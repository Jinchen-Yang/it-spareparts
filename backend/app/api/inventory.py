"""库存 API（§9）：page_inventory 可读，人工修正仅管理员（写审计）。"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import current_role, require_admin
from app.db import get_db
from app.security import (
    UserContext,
    apply_field_visibility,
    get_current_user_context,
    require_page,
)
from app.services import inventory

router = APIRouter(
    prefix="/inventory",
    tags=["inventory"],
    dependencies=[Depends(current_role), Depends(require_page("page_inventory"))],
)


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
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return apply_field_visibility(
        inventory.list_inventory(db, warehouse, q, page, page_size, ctx), ctx)


@router.get("/dynamic")
def list_dynamic(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """锚定动态库存（型号级）：期初=最近快照，之后跟单据流水；分仓快照行作参考。"""
    return apply_field_visibility(inventory.list_dynamic(db, q, page, page_size, ctx), ctx)


@router.get("/warehouses")
def warehouse_options(db: Session = Depends(get_db)) -> list[str]:
    return inventory.warehouses(db)


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
