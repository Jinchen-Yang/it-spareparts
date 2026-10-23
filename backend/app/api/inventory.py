"""库存 HTTP 入口：固定 read/adjust 动作；旧规则仅由未切换的领域适配器执行。"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import current_role
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
)
from app.services import inventory

router = APIRouter(
    prefix="/inventory",
    tags=["inventory"],
    dependencies=[Depends(current_role)],
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
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return inventory.read_inventory(db, identity, legacy_context=ctx, warehouse=warehouse, q=q, page=page, page_size=page_size)


@router.get("/dynamic")
def list_dynamic(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    """锚定动态库存（型号级）：期初=最近快照，之后跟单据流水；分仓快照行作参考。"""
    return inventory.read_dynamic(db, identity, legacy_context=ctx, q=q, page=page, page_size=page_size)


@router.get("/warehouses")
def warehouse_options(db: Session = Depends(get_db),
                      ctx: UserContext = Depends(get_current_user_context),
                      identity: VerifiedIdentity = Depends(current_verified_identity)) -> list[str]:
    return inventory.read_warehouses(db, identity, legacy_context=ctx)


@router.put("/{inv_id}")
def update(
    inv_id: int,
    body: InventoryUpdate,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    res = inventory.adjust_inventory(
        db, identity, legacy_context=ctx, inv_id=inv_id, manual_qty=body.manual_qty,
        safety_stock=body.safety_stock, clear_override=body.clear_override, reason=body.reason,
    )
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "库存记录不存在")
    return res
