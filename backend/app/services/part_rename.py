"""PN 标准化 = 受控改名（设计方案 §5）。

改名与合并是两件事：
- 合并：两个 part_id 是同一商品 → 事实归一到目标，源变墓碑（改身份）。见 merge.py。
- 改名：part_id 不变，只把 dim_part.pn_std 文本改成厂商规范写法（改文本不改身份）。本服务。

要点（评审定稿口径）：
1. pn_std 全局唯一。新名被其它 **active** 型号占用 → **不改名**，自动转为合并候选
   （同款两表达本质是合并问题，绝不自动合并）；被墓碑占用 → 拒绝（防复活占位）。
2. part_alias 与 dim_part 有复合外键 (part_id, pn_std)。改名须把名下别名的 pn_std 同事务一起改；
   该外键设为 DEFERRABLE，本服务 `SET CONSTRAINTS ... DEFERRED`，改完 commit 时统一校验。
3. pn_compact / search_doc 是库 STORED 生成列，改 pn_std 后自动重算，无需手写。
4. 旧 pn_std UPSERT 为 active 别名(source='rename')→ 老单据/旧习惯查询经别名召回照常命中
   （与 merge 留恒等别名同一招；绝不劫持已被第三方占用的 pn_raw）。
5. 事实表(采购/销售/库存) pn_std/pn_raw 原文不动（与 merge 同铁律：导入原文是追溯之根），
   业务身份全按 part_id 聚集，故改名不触发成本/利润重算。
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductMatchCandidate
from app.models.system import SysAuditLog

_log = logging.getLogger("rename")


class RenameError(Exception):
    """改名前置条件不满足。"""


def rename_part(db: Session, part_id: int, new_pn_std: str, reason: str | None,
                operated_by: str | None) -> dict:
    """把 part_id 的 pn_std 改成 new_pn_std。

    返回 {"renamed": True, ...} 成功；{"renamed": False, "merge_candidate_id": ...}
    表示目标名被 active 型号占用，已转合并候选（未改名）。
    """
    new_pn = (new_pn_std or "").strip()
    if not new_pn:
        raise RenameError("新 pn_std 不能为空")
    if len(new_pn) > 128:
        raise RenameError(f"新 pn_std 超长({len(new_pn)}>128)")

    part = db.execute(
        select(DimPart).where(DimPart.id == part_id).with_for_update()
    ).scalar_one_or_none()
    if part is None:
        raise RenameError(f"型号不存在: id={part_id}")
    if part.status != "active":
        raise RenameError(f"型号 {part.pn_std} 状态为 {part.status}，不可改名")
    old_pn = part.pn_std
    if new_pn == old_pn:
        raise RenameError("新旧 pn_std 相同，无需改名")

    # 目标名占用检查（pn_std 全局唯一）
    occ = db.execute(select(DimPart.id, DimPart.status)
                     .where(DimPart.pn_std == new_pn)).first()
    if occ is not None:
        occ_id, occ_status = occ
        if occ_status != "active":
            raise RenameError(f"目标名 {new_pn} 已被墓碑占用(防复活)，不可改名")
        # 同款两表达 → 转合并候选（与既有 pending 同对则跳过）
        cand_id = db.execute(
            pg_insert(ProductMatchCandidate).values(
                raw_text=old_pn, normalized_text=new_pn,
                source_part_id=part.id, candidate_part_id=occ_id,
                match_reason=f"改名目标 {new_pn} 已被占用，疑同款待合并审核",
                status="pending",
            ).on_conflict_do_nothing(
                index_elements=["source_part_id", "candidate_part_id"],
                index_where=text("status = 'pending'"),
            ).returning(ProductMatchCandidate.id)
        ).scalar()
        db.add(SysAuditLog(
            entity_type="part", entity_id=part.id, action="rename_to_candidate",
            before_json={"pn_std": old_pn},
            after_json={"target": new_pn, "occupied_by_part_id": occ_id},
            reason=reason, operated_by=operated_by))
        db.commit()
        return {"renamed": False, "part_id": part.id, "old_pn": old_pn,
                "target_pn": new_pn, "occupied_by_part_id": occ_id,
                "merge_candidate_id": cand_id,
                "message": "目标名被 active 型号占用，已转合并候选"}

    # —— 执行改名 —— 延迟复合外键，名下别名 pn_std 与 dim_part.pn_std 同事务改 ——
    db.execute(text("SET CONSTRAINTS fk_alias_part_pn DEFERRED"))
    alias_ids = list(db.execute(
        update(PartAlias).where(PartAlias.part_id == part.id)
        .values(pn_std=new_pn).returning(PartAlias.id)
    ).scalars().all())
    part.pn_std = new_pn
    part.reviewed_at = datetime.now(timezone.utc)
    db.flush()
    # 旧名→新名 active 别名：上面 bulk 多已把恒等别名(pn_raw=旧名)改成新名；
    # 仅当旧名没有恒等别名时补一条。DO NOTHING 防劫持归属第三方 part 的同 pn_raw 别名。
    inserted = db.execute(
        pg_insert(PartAlias).values(
            pn_raw=old_pn, pn_std=new_pn, part_id=part.id,
            source="rename", needs_review=False, status="active",
        ).on_conflict_do_nothing(index_elements=[PartAlias.pn_raw])
        .returning(PartAlias.id)
    ).scalar()
    db.add(SysAuditLog(
        entity_type="part", entity_id=part.id, action="rename",
        before_json={"pn_std": old_pn}, after_json={"pn_std": new_pn},
        reason=reason, operated_by=operated_by))
    db.commit()
    _log.info("renamed part %s: %r -> %r (aliases=%d)", part.id, old_pn, new_pn, len(alias_ids))
    return {"renamed": True, "part_id": part.id, "old_pn": old_pn, "new_pn": new_pn,
            "aliases_updated": len(alias_ids), "rename_alias_inserted": inserted is not None}
