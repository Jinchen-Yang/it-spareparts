"""池分析 transport：分析与逐单事实由 pool_access 分别授权和投影。

仅资源尚未切换时，protected service 的 legacy adapter 保留旧权限语义。
"""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.services import pool_access
from app.security import UserContext, get_current_user_context, record_access_log
from app.services import pool_price_analysis

router = APIRouter(prefix="/pool-analysis", tags=["pool-analysis"])


@router.get("/orders/{side}/{order_id}")
def get_order_detail(
    side: Literal["purchase", "sales"],
    order_id: int,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, 'pool_analysis_order_detail', 'order', {'side': side, 'order_id': order_id})
    data = pool_access.read_pool_order_fact(db, identity, legacy_context=ctx,
        side=side, order_id=order_id)
    if data is None:
        raise HTTPException(status_code=404, detail="订单不存在或不是已生效订单")
    return data


@router.get("/pools")
def list_pools(
    range_: str | None = Query("90d", alias="range", pattern="^(30d|90d|365d|all|custom)$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None),
    pn: str | None = Query(None),
    purchase_type: str | None = Query(None, description="采购类型，如销售订单、补库、指定采购"),
    sort: str = Query("member_count",
                      pattern="^(member_count|purchase_total|purchase_average|sales_total|sales_average)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, 'pool_analysis_list', 'pools', {'q': q, 'pn': pn, 'purchase_type': purchase_type, 'sort': sort})
    try:
        return pool_access.read_pool_analysis_list(db, identity, legacy_context=ctx,
            range_=range_, date_from=date_from, date_to=date_to, q=q, pn=pn,
            purchase_type=purchase_type, sort=sort, page=page, page_size=page_size)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/pools/{group_id}")
def get_pool_detail(
    group_id: int,
    range_: str | None = Query("90d", alias="range", pattern="^(30d|90d|365d|all|custom)$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    purchase_type: str | None = Query(None, description="仅查看指定采购类型"),
    purchase_page: int = Query(1, ge=1),
    sales_page: int = Query(1, ge=1),
    orders_page_size: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, 'pool_analysis_detail', 'pools', {'group_id': group_id, 'purchase_type': purchase_type})
    try:
        data = pool_access.read_pool_analysis_detail(db, identity, group_id, legacy_context=ctx,
            range_=range_, date_from=date_from, date_to=date_to, purchase_type=purchase_type,
            purchase_page=purchase_page, sales_page=sales_page, page_size=orders_page_size)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="池不存在或已归档")
    return data


@router.get("/pools/{group_id}/price-map")
def get_pool_price_map(
    group_id: int,
    side: Literal["purchase", "sales"] = Query("purchase"),
    range_: str | None = Query("90d", alias="range", pattern="^(30d|90d|365d|all|custom)$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    purchase_type: str | None = Query(None),
    employee: str | None = Query(None),
    sort: Literal["pn", "weighted_avg", "constraint_delta", "latest_date"] = Query("pn"),
    order: Literal["asc", "desc"] = Query("asc"),
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, 'pool_analysis_price_map', 'pools', {'group_id': group_id, 'side': side, 'purchase_type': purchase_type, 'employee': employee, 'sort': sort, 'order': order})
    try:
        data = pool_access.read_pool_price_map(db, identity, group_id, legacy_context=ctx,
            side=side, range_=range_, date_from=date_from, date_to=date_to,
            purchase_type=purchase_type, employee=employee, sort=sort, order=order)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="池不存在或已归档")
    return data
