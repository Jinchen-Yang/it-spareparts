"""库存服务（§7.4）：列表 + 人工修正（写审计、不碰 source_qty）。"""
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import security
from app.models.inventory import Inventory
from app.models.system import SysAuditLog


def _d(x):
    return float(x) if isinstance(x, Decimal) else x


def _jsonable(d: dict) -> dict:
    """审计 JSONB 安全化：date → isoformat 字符串。"""
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}


def _display_qty(inv: Inventory) -> Decimal:
    return inv.manual_qty if inv.is_qty_overridden and inv.manual_qty is not None else inv.source_qty


def _row(inv: Inventory) -> dict:
    return {
        "id": inv.id, "pn_std": inv.pn_std, "warehouse": inv.warehouse,
        "display_qty": _d(_display_qty(inv)),
        "source_qty": _d(inv.source_qty), "manual_qty": _d(inv.manual_qty),
        "is_qty_overridden": inv.is_qty_overridden, "safety_stock": _d(inv.safety_stock),
        "description": inv.description, "brand": inv.brand, "unit": inv.unit,
        "unit_cost": _d(inv.unit_cost), "inventory_value": _d(inv.inventory_value),
        "snapshot_date": inv.snapshot_date,
    }


def list_inventory(db: Session, warehouse: str | None, q: str | None,
                   page: int, page_size: int,
                   user_ctx: security.UserContext | None = None) -> dict:
    stmt = select(Inventory)
    if warehouse:
        stmt = stmt.where(Inventory.warehouse == warehouse)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Inventory.pn_std.ilike(like), Inventory.description.ilike(like)))
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(Inventory.pn_std, Inventory.warehouse)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_row(inv) for inv in rows]}


def warehouses(db: Session) -> list[str]:
    return [w for (w,) in db.execute(
        select(Inventory.warehouse).distinct().order_by(Inventory.warehouse)
    ).all()]


def update_inventory(db: Session, inv_id: int, manual_qty: Decimal | None,
                     safety_stock: Decimal | None, clear_override: bool,
                     reason: str | None, operated_by: str | None) -> dict | None:
    """人工修正：写 manual_qty/safety_stock，不动 source_qty；写审计；重算库存金额。"""
    inv = db.get(Inventory, inv_id)
    if inv is None:
        return None
    before = _row(inv)

    if clear_override:
        inv.manual_qty = None
        inv.is_qty_overridden = False
    elif manual_qty is not None:
        inv.manual_qty = manual_qty
        inv.is_qty_overridden = True
    if safety_stock is not None:
        inv.safety_stock = safety_stock

    # 重算库存金额（display_qty × unit_cost）
    if inv.unit_cost is not None:
        inv.inventory_value = (_display_qty(inv) * inv.unit_cost).quantize(Decimal("0.01"))

    db.flush()
    after = _row(inv)
    db.add(SysAuditLog(entity_type="inventory", entity_id=inv.id, action="update",
                       before_json=_jsonable(before), after_json=_jsonable(after), reason=reason,
                       operated_by=operated_by))
    db.commit()
    return after
