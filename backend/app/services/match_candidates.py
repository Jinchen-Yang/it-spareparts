"""疑似同款候选：生成 + 审核（整改 P2/P4，审核说明 §4.7/§7.6）。

生成两路（全部只建议、绝不自动合并）：
1. 重复组：pn_compact 完全一致的多个 active 型号（垃圾短 compact 已过滤），
   组内按业务量最大者为建议目标，其余成员各生成一条候选（score=1.0）；
2. 待审型号：needs_review=true 的型号跑 part_resolver 近似解析，
   取 score ≥ CANDIDATE_MIN_SCORE 的非待审 active 型号为建议目标（每源最多 TOP_N 条）。

幂等：部分唯一索引 (source, candidate) WHERE pending 防重；
已审核过（任何非 pending 终态）的同对不再重复生成。

审核动作：
- merge：执行 merge_parts（源并入候选目标）
- reject：仅否决本条建议
- independent：确认源是独立型号（清 needs_review、记 reviewed_at，并作废其全部待审候选）
注意：本模块只 import part_resolver、不在此修改它。整合后 resolver 已排除
merged 墓碑（与 search_parts 一致），本模块对其结果另按 status='active' 二次
过滤（见 generate 内 target_parts），故 resolver 的墓碑排除对候选发现是幂等的。
"""
import re
from datetime import datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import config
from app.models.dimensions import DimPart
from app.models.master_data import ProductMatchCandidate
from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine
from app.models.system import SysAuditLog
from app.services import master_data, merge, part_resolver

_NOW = lambda: datetime.now(timezone.utc)  # noqa: E731
_CJK = re.compile(r"[一-鿿]+")


class CandidateError(Exception):
    """候选审核动作非法。"""


def _volume_map(db: Session, part_ids: set[int]) -> dict[int, int]:
    """part → 业务行数（销售+采购），用于挑组内目标与队列排序。"""
    out: dict[int, int] = dict.fromkeys(part_ids, 0)
    for model in (FSalesLine, FPurchaseLine):
        for pid, cnt in db.execute(
            select(model.part_id, func.count()).where(model.part_id.in_(part_ids))
            .group_by(model.part_id)
        ).all():
            out[pid] = out.get(pid, 0) + cnt
    return out


def _reviewed_pairs(db: Session) -> set[tuple[int, int]]:
    return set(db.execute(
        select(ProductMatchCandidate.source_part_id, ProductMatchCandidate.candidate_part_id)
        .where(ProductMatchCandidate.status != "pending")
    ).all())


def _insert_pending(db: Session, rows: list[dict]) -> int:
    """新候选入队：与既有 pending 同对冲突时跳过（部分唯一索引仲裁）。"""
    inserted = 0
    for i in range(0, len(rows), 500):
        stmt = pg_insert(ProductMatchCandidate).values(rows[i:i + 500]).on_conflict_do_nothing(
            index_elements=["source_part_id", "candidate_part_id"],
            index_where=text("status = 'pending'"),
        )
        inserted += len(db.execute(stmt.returning(ProductMatchCandidate.id)).all())
    return inserted


def generate(db: Session) -> dict:
    """生成候选队列（幂等）。返回统计。"""
    reviewed = _reviewed_pairs(db)
    rows: list[dict] = []

    # ---- 1) 重复组 ----
    groups = master_data.dup_groups(db)
    member_ids = {pid for members in groups.values() for pid, _ in members}
    volume = _volume_map(db, member_ids) if member_ids else {}
    for compact, members in groups.items():
        target_id, target_pn = max(members, key=lambda m: (volume.get(m[0], 0), -m[0]))
        target_cjk = "".join(_CJK.findall(target_pn))
        for pid, pn in members:
            if pid == target_id or (pid, target_id) in reviewed:
                continue
            # compact 会剥掉中文：若中文残差不同（如"万兆单模"vs"万兆多模"），
            # 很可能是不同商品 —— 降分并明确警示，防止审核员被 1.0 误导
            src_cjk = "".join(_CJK.findall(pn))
            score, reason = 1.0, f"pn_compact 完全一致（{compact}），建议并入业务量最大的 {target_pn}"
            if src_cjk != target_cjk:
                score = 0.75
                reason += f"；⚠ 中文部分不同（{src_cjk or '无'} ≠ {target_cjk or '无'}），请人工核对是否同款"
            rows.append({
                "raw_text": pn, "normalized_text": compact,
                "source_part_id": pid, "candidate_part_id": target_id,
                "match_score": score, "match_reason": reason,
            })

    # ---- 2) 待审型号近似解析 ----
    pending_parts = db.execute(
        select(DimPart.id, DimPart.pn_std).where(
            DimPart.status == "active", DimPart.needs_review.is_(True))
    ).all()
    resolver_hits = 0
    for pid, pn in pending_parts:
        result = part_resolver.resolve(db, pn, limit=config.CANDIDATE_TOP_N + 3)
        targets = [it for it in result["items"]
                   if it["pn_std"] != pn and not it["needs_review"]
                   and it["score"] >= config.CANDIDATE_MIN_SCORE]
        if not targets:
            continue
        target_parts = dict(db.execute(select(DimPart.pn_std, DimPart.id).where(
            DimPart.pn_std.in_([t["pn_std"] for t in targets]),
            DimPart.status == "active")).all())
        n = 0
        for t in targets:
            tid = target_parts.get(t["pn_std"])
            if tid is None or (pid, tid) in reviewed or n >= config.CANDIDATE_TOP_N:
                continue
            rows.append({
                "raw_text": pn, "normalized_text": None,
                "source_part_id": pid, "candidate_part_id": tid,
                "match_score": min(t["score"], 1.0), "match_reason": t["match_reason"],
            })
            n += 1
        resolver_hits += 1 if n else 0

    inserted = _insert_pending(db, rows)
    db.commit()
    return {"dup_groups": len(groups), "needs_review_parts": len(pending_parts),
            "parts_with_suggestion": resolver_hits, "candidates_inserted": inserted,
            "candidates_proposed": len(rows)}


def list_candidates(db: Session, status: str, page: int, page_size: int,
                    order: str = "volume") -> dict:
    src = ProductMatchCandidate
    stmt = select(src).where(src.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    if order == "score":
        stmt = stmt.order_by(src.match_score.desc().nullslast(), src.id)
    elif order == "age":
        stmt = stmt.order_by(src.created_at.asc(), src.id)
    else:
        stmt = stmt.order_by(src.id)
    cands = db.execute(stmt.offset((page - 1) * page_size).limit(page_size)).scalars().all()

    part_ids = {c.source_part_id for c in cands if c.source_part_id} | \
               {c.candidate_part_id for c in cands if c.candidate_part_id}
    parts = {p.id: p for p in db.execute(
        select(DimPart).where(DimPart.id.in_(part_ids))).scalars().all()} if part_ids else {}
    volume = _volume_map(db, part_ids) if part_ids else {}

    items = []
    for c in cands:
        s, t = parts.get(c.source_part_id), parts.get(c.candidate_part_id)
        items.append({
            "id": c.id, "raw_text": c.raw_text,
            "source_pn": s.pn_std if s else None,
            "source_desc": s.description if s else None,
            "candidate_pn": t.pn_std if t else None,
            "candidate_desc": t.description if t else None,
            "match_score": float(c.match_score) if c.match_score is not None else None,
            "match_reason": c.match_reason, "status": c.status,
            "review_action": c.review_action,
            "source_volume": volume.get(c.source_part_id, 0),
            "created_at": c.created_at,
        })
    if order == "volume":
        items.sort(key=lambda x: (-x["source_volume"], x["id"]))
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def review(db: Session, candidate_id: int, action: str, reason: str | None,
           operated_by: str | None) -> dict:
    cand = db.get(ProductMatchCandidate, candidate_id)
    if cand is None:
        raise CandidateError(f"候选不存在: {candidate_id}")
    if cand.status != "pending":
        raise CandidateError(f"候选已审核（{cand.status}），不可重复操作")
    if action not in ("merge", "reject", "independent"):
        raise CandidateError(f"action 非法: {action}（merge | reject | independent）")

    out: dict = {"candidate_id": candidate_id, "action": action}
    if action == "merge":
        source = db.get(DimPart, cand.source_part_id) if cand.source_part_id else None
        target = db.get(DimPart, cand.candidate_part_id) if cand.candidate_part_id else None
        if source is None or target is None:
            raise CandidateError("候选缺源/目标型号，无法合并")
        result = merge.merge_parts(db, source.pn_std, target.pn_std,
                                   reason or f"接受候选#{candidate_id}", operated_by)
        out["merge"] = result
        cand = db.get(ProductMatchCandidate, candidate_id)  # merge 已 commit，重取
        cand.status, cand.review_action = "accepted", "merge"
    elif action == "reject":
        cand.status, cand.review_action = "rejected", "reject"
    else:  # independent：源确认为独立型号
        cand.status, cand.review_action = "rejected", "independent"
        if cand.source_part_id:
            out["confirmed"] = confirm_independent(
                db, [cand.source_part_id], operated_by, _commit=False)
    cand.reviewed_at = _NOW()
    db.add(SysAuditLog(entity_type="match_candidate", entity_id=candidate_id,
                       action=f"review_{action}", before_json={"status": "pending"},
                       after_json={"status": cand.status, "review_action": cand.review_action},
                       reason=reason, operated_by=operated_by))
    db.commit()
    return out


def confirm_independent(db: Session, part_ids: list[int], operated_by: str | None,
                        _commit: bool = True) -> dict:
    """批量确认独立型号：清 needs_review、记 reviewed_at、作废其待审候选。

    针对实测 ~90% 待审型号没有可用合并候选的现实——必须有批量出口，
    否则 1078 个逐条点击不可运维。
    """
    now = _NOW()
    confirmed = db.execute(
        update(DimPart).where(DimPart.id.in_(part_ids), DimPart.status == "active")
        .values(needs_review=False, reviewed_at=now).returning(DimPart.id)
    ).scalars().all()
    obsoleted = db.execute(
        update(ProductMatchCandidate).where(
            ProductMatchCandidate.source_part_id.in_(part_ids),
            ProductMatchCandidate.status == "pending",
        ).values(status="obsolete", review_action="independent", reviewed_at=now)
    ).rowcount
    db.add(SysAuditLog(entity_type="part", entity_id=0, action="confirm_independent_batch",
                       after_json={"part_ids": list(confirmed), "obsoleted_candidates": obsoleted},
                       operated_by=operated_by))
    if _commit:
        db.commit()
    return {"confirmed": len(confirmed), "obsoleted_candidates": obsoleted}


def parts_without_candidates(db: Session) -> list[int]:
    """待审且没有任何 pending 候选的型号 id（批量确认的默认范围）。"""
    has_pending = select(ProductMatchCandidate.source_part_id).where(
        ProductMatchCandidate.status == "pending").scalar_subquery()
    return list(db.execute(
        select(DimPart.id).where(
            DimPart.status == "active", DimPart.needs_review.is_(True),
            DimPart.id.not_in(has_pending))
    ).scalars().all())
