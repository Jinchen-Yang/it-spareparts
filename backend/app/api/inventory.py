"""库存 API（§9）：列表（登录可看）、人工修正（管理员，写审计）。"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import current_role, require_admin
from app.db import get_db
from app.security import UserContext, apply_field_visibility, get_current_user_context
from app.services import inventory

router = APIRouter(prefix="/inventory", tags=["inventory"])


class InventoryUpdate(BaseModel):
    manual_qty: Decimal | None = None
    safety_stock: Decimal | None = None
    clear_override: bool = False     # true 则撤销人工修正，恢复用 source_qty
    reason: str | None = None


@router.get("")
def list_(
    warehouse: str | None = Query(None),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return apply_field_visibility(
        inventory.list_inventory(db, warehouse, q, page, page_size, ctx), ctx)


@router.get("/warehouses")
def warehouse_options(db: Session = Depends(get_db), _: str = Depends(current_role)) -> list[str]:
    return inventory.warehouses(db)


@router.get("/movements")
def list_movements(
    warehouse: str | None = Query(None),
    q: str | None = Query(None),
    doc_type: str | None = Query(None),
    ledger_kind: str | None = Query(None),   # part | machine
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """全局出入流水分页（§7.6）。"""
    return apply_field_visibility(
        inventory.list_movements(db, warehouse, q, doc_type, page, page_size, ledger_kind), ctx)


@router.get("/parts/{part_id}/ledger")
def part_ledger(
    part_id: int,
    warehouse: str | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """单个型号的完整出入流水 + 逐行动态结存——库存页「点开看每条出入」。"""
    rows = inventory.part_ledger(db, part_id, warehouse)
    return apply_field_visibility({"part_id": part_id, "items": rows}, ctx)


@router.get("/reconciliation")
def reconciliation(
    warehouse: str | None = Query(None),
    only_diff: bool = Query(True),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
) -> dict:
    """对账：流水回放在库 vs 快照 source_qty 的差异（§7.6）。"""
    return inventory.reconcile(db, warehouse, only_diff)


@router.put("/{inv_id}")
def update(
    inv_id: int,
    body: InventoryUpdate,
    db: Session = Depends(get_db),
    role: str = Depends(require_admin),
) -> dict:
    res = inventory.update_inventory(
        db, inv_id, body.manual_qty, body.safety_stock,
        body.clear_override, body.reason, operated_by=role,
    )
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "库存记录不存在")
    return res
