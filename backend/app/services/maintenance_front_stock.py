"""维保前置库账本服务（B1）。

口径（2026-08-15 已确认）：
- 入账 = 发货单维保供货（shipment_in）、采购直发（purchase_in）；
- 出账 = 项目结束收回（return_out）、变卖（salvage_out）；
- 现场领用不写本账本；无收货环节；不记 SN。
- 幂等键 = (kind, source_type, source_ref, part_id)：同一来源行重复入账自动收敛。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_front_stock import (
    SOURCE_TYPES,
    MaintenanceFrontStock,
    MaintenanceFrontStockLedger,
)

KIND_SIGNS = {
    "shipment_in": Decimal(1),
    "purchase_in": Decimal(1),
    "return_out": Decimal(-1),
    "salvage_out": Decimal(-1),
}


class FrontStockError(RuntimeError):
    """前置库账本业务错误基类。"""


class FrontStockNegativeBalance(FrontStockError):
    """出账后结存为负：失败关闭，不写账。"""


class FrontStockInvalidMovement(FrontStockError):
    """非法流水类型/来源/数量。"""


def _signed(kind: str, qty: Decimal) -> Decimal:
    sign = KIND_SIGNS.get(kind)
    if sign is None:
        raise FrontStockInvalidMovement(f"未知流水类型：{kind}")
    if qty <= 0:
        raise FrontStockInvalidMovement("数量必须为正数")
    return qty * sign


def _get_or_create_stock(
    db: Session,
    *,
    project_id: str,
    part_id: int,
    warehouse_name: str,
) -> MaintenanceFrontStock:
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == project_id,
            MaintenanceFrontStock.part_id == part_id,
            MaintenanceFrontStock.warehouse_name == warehouse_name,
        )
    ).scalar_one_or_none()
    if stock is None:
        stock = MaintenanceFrontStock(
            stock_id=str(uuid4()),
            project_id=project_id,
            part_id=part_id,
            warehouse_name=warehouse_name,
            qty=Decimal("0"),
            version=1,
        )
        db.add(stock)
        db.flush()
    return stock


def apply_movement(
    db: Session,
    *,
    project_id: str,
    part_id: int,
    kind: str,
    source_type: str,
    source_ref: str,
    qty: Decimal,
    warehouse_name: str = "",
    unit_cost_ex_tax: Decimal | None = None,
    unit_cost_inc_tax: Decimal | None = None,
    reason: str | None = None,
    operated_by: str,
) -> MaintenanceFrontStockLedger:
    """入/出账一笔记账。幂等：同 (kind, source_type, source_ref, part_id) 直接返回已记账。"""
    if source_type not in SOURCE_TYPES:
        raise FrontStockInvalidMovement(f"未知来源类型：{source_type}")
    if not source_ref or not source_ref.strip():
        raise FrontStockInvalidMovement("来源引用不可为空")
    if not operated_by or not operated_by.strip():
        raise FrontStockInvalidMovement("操作人不可为空")
    qty = Decimal(qty)
    signed = _signed(kind, qty)

    existing = db.execute(
        select(MaintenanceFrontStockLedger).where(
            MaintenanceFrontStockLedger.kind == kind,
            MaintenanceFrontStockLedger.source_type == source_type,
            MaintenanceFrontStockLedger.source_ref == source_ref[:128],
            MaintenanceFrontStockLedger.part_id == part_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    stock = _get_or_create_stock(
        db, project_id=project_id, part_id=part_id, warehouse_name=warehouse_name
    )
    new_qty = stock.qty + signed
    if new_qty < 0:
        raise FrontStockNegativeBalance(
            f"前置库结存不足：{stock.qty} < 出账 {qty}（{kind}）"
        )
    stock.qty = new_qty
    if signed > 0:
        stock.last_inbound_at = datetime.now(timezone.utc)
        if unit_cost_ex_tax is not None:
            stock.unit_cost_ex_tax = unit_cost_ex_tax
        if unit_cost_inc_tax is not None:
            stock.unit_cost_inc_tax = unit_cost_inc_tax
    stock.version += 1

    ledger = MaintenanceFrontStockLedger(
        ledger_id=str(uuid4()),
        stock_id=stock.stock_id,
        project_id=project_id,
        part_id=part_id,
        kind=kind,
        source_type=source_type,
        source_ref=source_ref[:128],
        qty_change=signed,
        qty_after=new_qty,
        unit_cost_ex_tax=unit_cost_ex_tax,
        unit_cost_inc_tax=unit_cost_inc_tax,
        reason=reason,
        operated_by=operated_by[:64],
    )
    db.add(ledger)
    db.flush()
    return ledger


def balance_rows(db: Session, project_id: str) -> list[dict]:
    """项目前置库结存（含库龄天数与金额估值）。"""
    from app.models.dimensions import DimPart

    rows = db.execute(
        select(
            MaintenanceFrontStock,
            DimPart.pn_std,
            DimPart.description,
        )
        .join(DimPart, DimPart.id == MaintenanceFrontStock.part_id)
        .where(MaintenanceFrontStock.project_id == project_id)
        .order_by(MaintenanceFrontStock.warehouse_name, DimPart.pn_std)
    ).all()
    now = datetime.now(timezone.utc)
    result = []
    for stock, pn, description in rows:
        age_days = None
        if stock.last_inbound_at is not None:
            age_days = max(0, (now - stock.last_inbound_at).days)
        result.append(
            {
                "stock_id": stock.stock_id,
                "part_id": stock.part_id,
                "pn": pn,
                "description": description,
                "warehouse_name": stock.warehouse_name,
                "qty": float(stock.qty),
                "unit_cost_ex_tax": (
                    float(stock.unit_cost_ex_tax)
                    if stock.unit_cost_ex_tax is not None
                    else None
                ),
                "unit_cost_inc_tax": (
                    float(stock.unit_cost_inc_tax)
                    if stock.unit_cost_inc_tax is not None
                    else None
                ),
                "value_ex_tax": (
                    float(stock.qty * stock.unit_cost_ex_tax)
                    if stock.unit_cost_ex_tax is not None
                    else None
                ),
                "last_inbound_at": (
                    stock.last_inbound_at.isoformat()
                    if stock.last_inbound_at is not None
                    else None
                ),
                "age_days": age_days,
            }
        )
    return result


def ledger_entries(db: Session, project_id: str, *, limit: int = 200) -> list[dict]:
    rows = db.execute(
        select(MaintenanceFrontStockLedger)
        .where(MaintenanceFrontStockLedger.project_id == project_id)
        .order_by(MaintenanceFrontStockLedger.created_at.desc())
        .limit(min(max(limit, 1), 1000))
    ).scalars().all()
    return [
        {
            "ledger_id": row.ledger_id,
            "part_id": row.part_id,
            "kind": row.kind,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "qty_change": float(row.qty_change),
            "qty_after": float(row.qty_after),
            "reason": row.reason,
            "operated_by": row.operated_by,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
