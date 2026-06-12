"""商品合并服务（整改 P4，审核说明 §4.8/原则5）。

合并 = 把源型号的全部业务事实归到目标型号，源行变墓碑（status=merged）。
本系统最危险操作，设计要点（评审定稿）：
- 按 id 升序 SELECT FOR UPDATE 锁源/目标两行，持锁后复核双方均 active
  （防 A→B 与 B→A 并发互合成环）；
- 别名用 UPSERT 而非 INSERT（恒等别名 94% 已存在，纯 INSERT 必撞唯一键）；
- 路径压缩：指向源的 merged_into_id 全部改指目标，链长恒 ≤1；
- 事实行 pn_std/pn_raw 不改写（追溯与回滚归属的前提），只 repoint part_id；
- merge_log 存源行全镜像 + 目标被补字段 before/after + 各表受影响行 id，
  支持人工 LIFO 回滚（不提供任意历史自动回滚）；
- 合并提交后自动重算利润与库存成本（两者各自管理事务，绝不塞进合并事务）。
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart, PartAlias
from app.models.inquiry import FPartInquiry
from app.models.inventory import Inventory, PartSubstitute
from app.models.master_data import (
    ProductDataQualityIssue,
    ProductMatchCandidate,
    ProductMergeLog,
    ProductSpec,
)
from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine
from app.models.system import SysAuditLog

_log = logging.getLogger("merge")

# 合并时从源补到目标的 fill-if-empty 字段
_FILL_FIELDS = ("description", "brand", "brand_id", "category_major", "category_minor",
                "category_id", "machine_or_part", "unit", "pn_raw_sample")


class MergeError(Exception):
    """合并前置条件不满足。"""


def _part_snapshot(p: DimPart) -> dict:
    return {
        "id": p.id, "pn_std": p.pn_std, "pn_raw_sample": p.pn_raw_sample,
        "description": p.description, "brand": p.brand, "brand_id": p.brand_id,
        "category_major": p.category_major, "category_minor": p.category_minor,
        "category_id": p.category_id, "machine_or_part": p.machine_or_part,
        "unit": p.unit, "needs_review": p.needs_review,
        "is_excluded": p.is_excluded, "exclude_reason": p.exclude_reason,
        "status": p.status, "merged_into_id": p.merged_into_id,
    }


def _repoint(db: Session, model, source_id: int, target_id: int) -> list[int]:
    return list(db.execute(
        update(model).where(model.part_id == source_id)
        .values(part_id=target_id).returning(model.id)
    ).scalars().all())


def _merge_substitutes(db: Session, source: DimPart, target: DimPart) -> dict:
    """替代关系迁移：删自配对，repoint 时按规范序重排+方向换算，与既有目标对去重。"""
    rows = db.execute(select(PartSubstitute).where(
        or_(PartSubstitute.part_id_a == source.id, PartSubstitute.part_id_b == source.id)
    )).scalars().all()
    moved, dropped = [], []
    existing = {(s.part_id_a, s.part_id_b) for s in db.execute(
        select(PartSubstitute).where(
            or_(PartSubstitute.part_id_a == target.id, PartSubstitute.part_id_b == target.id))
    ).scalars().all()}
    for s in rows:
        other = s.part_id_b if s.part_id_a == source.id else s.part_id_a
        before = {"id": s.id, "a": s.part_id_a, "b": s.part_id_b,
                  "direction": s.direction, "status": s.status, "note": s.note}
        if other == target.id:                      # 源↔目标互为替代 → 合并后自配对，删
            db.delete(s)
            dropped.append(before)
            continue
        new_a, new_b = sorted([target.id, other])
        if (new_a, new_b) in existing:              # 目标已有同对 → 删源对，保目标对
            db.delete(s)
            dropped.append(before)
            continue
        # 方向相对规范序：源在原对中的位次与目标在新对中的位次不同则翻转
        src_was_a = s.part_id_a == source.id
        tgt_is_a = new_a == target.id
        direction = s.direction
        if direction != "both" and src_was_a != tgt_is_a:
            direction = "a_to_b" if direction == "b_to_a" else "b_to_a"
        s.part_id_a, s.part_id_b, s.direction = new_a, new_b, direction
        existing.add((new_a, new_b))
        moved.append(before)
    return {"moved": moved, "dropped": dropped}


def merge_parts(db: Session, source_pn: str, target_pn: str, reason: str | None,
                operated_by: str | None) -> dict:
    """执行合并（单事务）。返回 merge_log 摘要；调用方负责随后触发重算。"""
    if source_pn == target_pn:
        raise MergeError("源与目标是同一型号")
    pre = db.execute(select(DimPart.id, DimPart.pn_std).where(
        DimPart.pn_std.in_([source_pn, target_pn]))).all()
    by_pn = {pn: pid for pid, pn in pre}
    missing = [pn for pn in (source_pn, target_pn) if pn not in by_pn]
    if missing:
        raise MergeError(f"型号不存在: {missing}")

    # 按 id 升序锁行，防并发互合死锁/成环
    locked = {}
    for pid in sorted(by_pn.values()):
        locked[pid] = db.execute(
            select(DimPart).where(DimPart.id == pid).with_for_update()
        ).scalar_one()
    source, target = locked[by_pn[source_pn]], locked[by_pn[target_pn]]
    if source.status != "active":
        raise MergeError(f"源型号 {source_pn} 状态为 {source.status}，不可合并")
    if target.status != "active":
        raise MergeError(f"目标型号 {target_pn} 状态为 {target.status}，不可作为合并目标")

    source_snapshot = _part_snapshot(source)

    # 1) 目标 fill-if-empty 补资料
    merged_fields = {}
    for f in _FILL_FIELDS:
        if getattr(target, f) is None and getattr(source, f) is not None:
            merged_fields[f] = {"before": None, "after": getattr(source, f)}
            setattr(target, f, getattr(source, f))

    # 2) 事实表 repoint（pn_std/pn_raw 原文不动）
    affected = {
        "f_purchase_line_ids": _repoint(db, FPurchaseLine, source.id, target.id),
        "f_sales_line_ids": _repoint(db, FSalesLine, source.id, target.id),
        "inventory_ids": _repoint(db, Inventory, source.id, target.id),
        "f_part_inquiry_ids": _repoint(db, FPartInquiry, source.id, target.id),
    }

    # 3) 别名：源名下全部别名 repoint 到目标（复合外键要求 part_id 与 pn_std 同改）
    affected["part_alias_ids"] = list(db.execute(
        update(PartAlias).where(PartAlias.part_id == source.id)
        .values(part_id=target.id, pn_std=target.pn_std).returning(PartAlias.id)
    ).scalars().all())
    # 源 pn_std 本身必须有一条 active 别名指向目标（恒等别名 94% 已存在 → UPSERT）
    db.execute(pg_insert(PartAlias).values(
        pn_raw=source.pn_std, pn_std=target.pn_std, part_id=target.id,
        source="merge", needs_review=False, status="active",
    ).on_conflict_do_update(
        index_elements=[PartAlias.pn_raw],
        set_={"pn_std": target.pn_std, "part_id": target.id,
              "status": "active", "source": "merge", "needs_review": False},
    ))

    # 4) 规格：目标缺的键迁过去，冲突的键保留目标值、删源行
    spec_moved = db.execute(update(ProductSpec).where(
        ProductSpec.part_id == source.id,
        ~ProductSpec.spec_key.in_(select(ProductSpec.spec_key).where(
            ProductSpec.part_id == target.id).scalar_subquery()),
    ).values(part_id=target.id).returning(ProductSpec.id)).scalars().all()
    spec_dropped = db.execute(
        select(ProductSpec).where(ProductSpec.part_id == source.id)).scalars().all()
    for s in spec_dropped:
        db.delete(s)
    affected["spec_moved_ids"] = list(spec_moved)
    affected["spec_dropped"] = [{"key": s.spec_key, "value": s.spec_value} for s in spec_dropped]

    # 5) 替代关系
    affected["substitutes"] = _merge_substitutes(db, source, target)

    # 6) 涉及源的其它待审候选作废；源的 open 质量问题关闭
    db.execute(update(ProductMatchCandidate).where(
        ProductMatchCandidate.status == "pending",
        or_(ProductMatchCandidate.source_part_id == source.id,
            ProductMatchCandidate.candidate_part_id == source.id),
    ).values(status="obsolete", reviewed_at=datetime.now(timezone.utc)))
    db.execute(update(ProductDataQualityIssue).where(
        ProductDataQualityIssue.part_id == source.id,
        ProductDataQualityIssue.status == "open",
    ).values(status="resolved", resolved_by=operated_by or "merge",
             resolved_at=datetime.now(timezone.utc),
             resolution_note=f"已合并入 {target.pn_std}"))

    # 7) 路径压缩 + 源变墓碑
    db.execute(update(DimPart).where(DimPart.merged_into_id == source.id)
               .values(merged_into_id=target.id))
    source.status = "merged"
    source.merged_into_id = target.id
    target.reviewed_at = datetime.now(timezone.utc)

    counts = {k: (len(v) if isinstance(v, list) else None) for k, v in affected.items()}
    log_row = ProductMergeLog(
        source_part_id=source.id, target_part_id=target.id, merge_reason=reason,
        source_snapshot=source_snapshot, merged_fields=merged_fields,
        affected_records=affected, created_by=operated_by,
    )
    db.add(log_row)
    db.add(SysAuditLog(
        entity_type="part", entity_id=source.id, action="merge",
        before_json=source_snapshot,
        after_json={"merged_into": target.pn_std, "target_part_id": target.id, **counts},
        reason=reason, operated_by=operated_by))
    db.commit()
    _log.info("merged part %s -> %s (%s)", source_pn, target_pn, counts)
    return {"source_pn": source_pn, "target_pn": target_pn,
            "merge_log_id": log_row.id, "affected_counts": counts}


def cost_source_distribution(db: Session, part_id: int) -> dict:
    """某 part 名下销售行 cost_source 分布（合并前后对比用）。"""
    rows = db.execute(
        select(FSalesLine.cost_source, func.count()).where(FSalesLine.part_id == part_id)
        .group_by(FSalesLine.cost_source)
    ).all()
    return {(k or "null"): v for k, v in rows}
