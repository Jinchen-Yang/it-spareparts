"""替代料服务（§5/§9）：查 + 增（a<b 排序去重、写审计）。"""
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.inventory import PartSubstitute
from app.models.system import SysAuditLog


class SubstituteError(Exception):
    """型号不存在 / 自己关联自己等。"""


def list_substitutes(db: Session, pn_std: str) -> list[dict]:
    part = db.scalar(select(DimPart).where(DimPart.pn_std == pn_std))
    if part is None:
        return []
    rows = db.execute(
        select(PartSubstitute).where(
            or_(PartSubstitute.part_id_a == part.id, PartSubstitute.part_id_b == part.id)
        )
    ).scalars().all()
    out = []
    for s in rows:
        other_id = s.part_id_b if s.part_id_a == part.id else s.part_id_a
        other = db.get(DimPart, other_id)
        if other:
            out.append({"pn_std": other.pn_std, "description": other.description,
                        "source": s.source, "note": s.note})
    return out


def add_substitute(db: Session, pn_a: str, pn_b: str, note: str | None,
                   operated_by: str | None) -> dict:
    if pn_a == pn_b:
        raise SubstituteError("不能把型号设为自己的替代料")
    pa = db.scalar(select(DimPart).where(DimPart.pn_std == pn_a))
    pb = db.scalar(select(DimPart).where(DimPart.pn_std == pn_b))
    missing = [pn for pn, p in [(pn_a, pa), (pn_b, pb)] if p is None]
    if missing:
        raise SubstituteError(f"型号不存在: {missing}")

    # CHECK(part_id_a < part_id_b)：写入前排序
    a_id, b_id = sorted([pa.id, pb.id])
    stmt = pg_insert(PartSubstitute).values(
        part_id_a=a_id, part_id_b=b_id, source="manual", note=note
    ).on_conflict_do_nothing(index_elements=["part_id_a", "part_id_b"]).returning(PartSubstitute.id)
    new_id = db.execute(stmt).scalar()
    created = new_id is not None
    if created:
        db.add(SysAuditLog(entity_type="substitute", entity_id=new_id, action="create",
                           before_json=None,
                           after_json={"pn_a": pn_a, "pn_b": pn_b, "note": note},
                           reason=note, operated_by=operated_by))
    db.commit()
    return {"created": created, "pn_a": pn_a, "pn_b": pn_b}
