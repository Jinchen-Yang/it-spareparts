"""全员互通池价格分析读接口（DEV-03/04）。

与老板经营看板分离：这里只认 ``page_pool_analysis``，不放开任何
``/dashboard`` 接口。所有响应继续经过字段权限结构化脱敏。
"""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    is_field_hidden,
    record_access_log,
    require_page,
)
from app.services import pool_price_analysis

router = APIRouter(prefix="/pool-analysis", tags=["pool-analysis"])


@router.get("/orders/{side}/{order_id}")
def get_order_detail(
    side: Literal["purchase", "sales"],
    order_id: int,
    db: Session = Depends(get_db),
    _role: str = Depends(current_role),
    _page: None = Depends(require_page("page_pool_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "pool_analysis_order_detail", "order", {
        "side": side, "order_id": order_id})
    data = pool_price_analysis.order_detail(db, side, order_id)
    if data is None:
        raise HTTPException(status_code=404, detail="订单不存在或不是已生效订单")
    return pool_price_analysis.apply_visibility(data, ctx)


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
    _role: str = Depends(current_role),
    _page: None = Depends(require_page("page_pool_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    price_restricted = is_field_hidden(ctx, "purchase_ceiling_ex_tax")
    ranking_restricted = price_restricted and sort != "member_count"
    effective_sort = "member_count" if ranking_restricted else sort
    record_access_log(ctx, "pool_analysis_list", "pools",
                      {"q": q, "pn": pn, "purchase_type": purchase_type, "sort": sort,
                       "effective_sort": effective_sort})
    try:
        data = pool_price_analysis.list_pools(
            db, range_=range_, date_from=date_from, date_to=date_to,
            q=q, pn=pn, purchase_type=purchase_type, page=page, page_size=page_size,
            requested_sort=sort, effective_sort=effective_sort,
            ranking_restricted=ranking_restricted)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return pool_price_analysis.apply_visibility(data, ctx)


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
    _role: str = Depends(current_role),
    _page: None = Depends(require_page("page_pool_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "pool_analysis_detail", "pools", {
        "group_id": group_id, "purchase_type": purchase_type})
    try:
        data = pool_price_analysis.pool_detail(
            db, group_id, date_from, date_to, range_=range_, purchase_type=purchase_type,
            purchase_page=purchase_page,
            sales_page=sales_page, page_size=orders_page_size)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="池不存在或已归档")
    return pool_price_analysis.apply_visibility(data, ctx)


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
    _role: str = Depends(current_role),
    _page: None = Depends(require_page("page_pool_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    price_restricted = is_field_hidden(ctx, "purchase_ceiling_ex_tax")
    # 无价格权限时，必须在调用服务前把真实数组顺序也锁定为 PN 升序；
    # 不能仅在 apply_visibility 阶段改响应元数据，否则 pn/desc 仍会泄露旧排序语义。
    ranking_restricted = price_restricted and (sort != "pn" or order != "asc")
    effective_sort = "pn" if price_restricted else sort
    effective_order = "asc" if price_restricted else order
    record_access_log(ctx, "pool_analysis_price_map", "pools", {
        "group_id": group_id, "side": side, "purchase_type": purchase_type,
        "employee": employee, "sort": sort, "effective_sort": effective_sort,
        "order": order, "effective_order": effective_order,
    })
    try:
        data = pool_price_analysis.price_map(
            db, group_id, side=side, range_=range_, date_from=date_from, date_to=date_to,
            purchase_type=purchase_type, employee=employee,
            requested_sort=sort, requested_order=order,
            effective_sort=effective_sort, effective_order=effective_order)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="池不存在或已归档")
    return pool_price_analysis.apply_visibility(data, ctx)
