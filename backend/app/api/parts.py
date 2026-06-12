"""型号查询 API（§9）。用 query 传 pn_std，避开 PN 中的 / # 路由问题。需登录。"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import UserContext, apply_field_visibility, get_current_user_context, record_access_log
from app.services import part_overview

router = APIRouter(prefix="/parts", tags=["parts"])


@router.get("/search")
def search(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    part_type: str | None = Query(None, description="HDD | SSD | RAM"),
    interface: str | None = Query(None, description="SAS | SATA | NVME | FC | SCSI"),
    capacity_min: float | None = Query(None, description="容量下限(GB)"),
    capacity_max: float | None = Query(None, description="容量上限(GB)"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "search", "parts", {"q": q})
    data = part_overview.search_parts(db, q, page, page_size, ctx,
                                      part_type=part_type, interface=interface,
                                      capacity_min=capacity_min, capacity_max=capacity_max)
    return apply_field_visibility(data, ctx)


@router.get("/overview")
def overview(
    pn_std: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "overview", "part", {"pn_std": pn_std})
    data = part_overview.get_overview(db, pn_std, ctx)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"型号不存在: {pn_std}")
    return apply_field_visibility(data, ctx)


@router.get("/purchases")
def purchases(
    pn_std: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return apply_field_visibility(part_overview.list_purchases(db, pn_std, page, page_size, ctx), ctx)


@router.get("/sales")
def sales(
    pn_std: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return apply_field_visibility(part_overview.list_sales(db, pn_std, page, page_size, ctx), ctx)
