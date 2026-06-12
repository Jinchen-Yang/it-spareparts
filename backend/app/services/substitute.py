"""替代料服务（§5/§9）：查 + 增（a<b 排序去重、写审计）。

整改 P1：显式方向/类型/审核状态。行按 part_id_a < part_id_b 规范序存储，
direction 是相对规范序的枚举（both/a_to_b/b_to_a）。
人工创建（admin 已鉴权）即 status=active；未来自动发现的关系入 pending，
未审核不进入型号全景推荐（见 part_overview._substitutes）。
"""
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.inventory import PartSubstitute
from app.models.system import SysAuditLog
from app.services import part_overview

_TYPES = {"original", "compatible", "same_spec", "downgrade", "upgrade", "conditional"}


class SubstituteError(Exception):
    """型号不存在 / 自己关联自己等。"""


def list_substitutes(db: Session, pn_std: str) -> list[dict]:
    # 整改 P3：入参可能是已合并墓碑——merge 已把替代关系全 repoint 到目标 part_id，
    # 按墓碑 id 直查会得空。先 resolve_part 沿 merged 链取目标再查（与 list_sales 同源）。
    part, _ = part_overview.resolve_part(db, pn_std)
    if part is None:
        return []
    rows = db.execute(
        select(PartSubstitute).where(
            or_(PartSubstitute.part_id_a == part.id, PartSubstitute.part_id_b == part.id)
        )
    ).scalars().all()
    out = []
    for s in rows:
        is_a = s.part_id_a == part.id
        other_id = s.part_id_b if is_a else s.part_id_a
        other = db.get(DimPart, other_id)
        if other:
            if s.direction == "both":
                direction = "both"
            elif (is_a and s.direction == "a_to_b") or (not is_a and s.direction == "b_to_a"):
                direction = "incoming"   # 对方可替代本型号
            else:
                direction = "outgoing"   # 本型号可替代对方
            out.append({"pn_std": other.pn_std, "description": other.description,
                        "source": s.source, "note": s.note, "direction": direction,
                        "substitute_type": s.substitute_type, "status": s.status})
    return out


def add_substitute(db: Session, pn_a: str, pn_b: str, note: str | None,
                   operated_by: str | None, direction: str = "both",
                   substitute_type: str | None = None) -> dict:
    """direction 入参语义：both=互替；one_way=「pn_b 可替代 pn_a」（b 是替代者）。"""
    if pn_a == pn_b:
        raise SubstituteError("不能把型号设为自己的替代料")
    if direction not in ("both", "one_way"):
        raise SubstituteError(f"direction 非法: {direction}（both | one_way）")
    if substitute_type is not None and substitute_type not in _TYPES:
        raise SubstituteError(f"substitute_type 非法: {substitute_type}")
    pa = db.scalar(select(DimPart).where(DimPart.pn_std == pn_a))
    pb = db.scalar(select(DimPart).where(DimPart.pn_std == pn_b))
    missing = [pn for pn, p in [(pn_a, pa), (pn_b, pb)] if p is None]
    if missing:
        raise SubstituteError(f"型号不存在: {missing}")
    merged = [p.pn_std for p in (pa, pb) if p.status == "merged"]
    if merged:
        raise SubstituteError(f"型号已被合并，请对合并目标操作: {merged}")

    # CHECK(part_id_a < part_id_b)：写入前排序，方向按排序结果换算为相对枚举
    a_id, b_id = sorted([pa.id, pb.id])
    if direction == "both":
        dir_rel = "both"
    else:
        # one_way：b 可替代 a，即「pn_a 的需求可用 pn_b 满足」
        dir_rel = "a_to_b" if a_id == pa.id else "b_to_a"
    stmt = pg_insert(PartSubstitute).values(
        part_id_a=a_id, part_id_b=b_id, source="manual", note=note,
        direction=dir_rel, substitute_type=substitute_type,
        status="active", reviewed_at=datetime.now(timezone.utc),
    ).on_conflict_do_nothing(index_elements=["part_id_a", "part_id_b"]).returning(PartSubstitute.id)
    new_id = db.execute(stmt).scalar()
    created = new_id is not None
    if created:
        db.add(SysAuditLog(entity_type="substitute", entity_id=new_id, action="create",
                           before_json=None,
                           after_json={"pn_a": pn_a, "pn_b": pn_b, "note": note,
                                       "direction": direction, "substitute_type": substitute_type},
                           reason=note, operated_by=operated_by))
    db.commit()
    return {"created": created, "pn_a": pn_a, "pn_b": pn_b}
