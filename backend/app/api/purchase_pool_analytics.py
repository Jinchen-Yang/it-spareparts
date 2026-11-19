"""采购分析的通用池只读面，复用经营看板的页面与维度权限。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.purchases import _analytics_common, _guard_customer_filter, _guard_scoped, _run
from app.api.query_bounds import ANALYTICS_PAGE_MAX, DbId
from app.auth import current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    is_field_hidden,
    record_access_log,
    require_page,
)
from app.services import purchase_analytics_cube as cube
from app.services import purchase_pool_analytics as service

router = APIRouter(
    prefix="/purchases/analytics/pool",
    tags=["purchases"],
    dependencies=[Depends(current_role), Depends(require_page("page_purchase_analytics"))],
)


def _scope(
    filters: dict = Depends(_analytics_common),
    scope: str | None = Query(None, max_length=8192),
    ctx: UserContext = Depends(get_current_user_context),
):
    _guard_scoped(ctx)
    _guard_customer_filter(filters, ctx)
    exact = _run(lambda: cube.parse_scope(scope))
    _run(lambda: cube.guard_dimensions(ctx, [], exact, "orders"))
    prices = not (
        is_field_hidden(ctx, "amount_paid") or is_field_hidden(ctx, "purchase_ceiling_ex_tax")
    )
    return filters, exact, prices, not is_field_hidden(ctx, "supplier"), ctx


@router.get("/parts")
def pool_parts(
    q: str = Query("", max_length=200),
    focus: Literal["all", "single", "unpooled", "above_pool", "above_cap"] = "all",
    pool_id: DbId | None = None,
    page: int = Query(1, ge=1, le=ANALYTICS_PAGE_MAX),
    page_size: int = Query(25, ge=1, le=100),
    request=Depends(_scope),
    db: Session = Depends(get_db),
):
    filters, scope, prices, suppliers, ctx = request
    if focus in ("above_pool", "above_cap") and not prices:
        raise HTTPException(403, "查看价格偏离需要采购成本及池价格权限")
    if focus == "single" and not suppliers:
        raise HTTPException(403, "无供应商权限")
    record_access_log(
        ctx, "analytics_pool_parts", "purchases/analytics", {"focus": focus, "page": page}
    )
    return _run(
        lambda: service.parts(
            db,
            filters,
            scope,
            q=q.strip(),
            focus=focus,
            pool_id=pool_id,
            page=page,
            page_size=page_size,
            price_visible=prices,
            supplier_visible=suppliers,
        )
    )


@router.get("/sources")
def pool_sources(
    part_id: DbId,
    unit: str | None = Query(None, max_length=16),
    page: int = Query(1, ge=1, le=ANALYTICS_PAGE_MAX),
    page_size: int = Query(25, ge=1, le=100),
    request=Depends(_scope),
    db: Session = Depends(get_db),
):
    filters, scope, prices, suppliers, ctx = request
    if not suppliers:
        raise HTTPException(403, "无供应商权限")
    record_access_log(ctx, "analytics_pool_sources", "purchases/analytics", {"part_id": part_id})
    return _run(
        lambda: service.sources(
            db,
            filters,
            scope,
            part_id=part_id,
            unit=(unit or "").strip() or None,
            page=page,
            page_size=page_size,
            price_visible=prices,
        )
    )


@router.get("/members")
def pool_members(
    pool_id: DbId,
    unit: str | None = Query(None, max_length=16),
    page: int = Query(1, ge=1, le=ANALYTICS_PAGE_MAX),
    page_size: int = Query(25, ge=1, le=100),
    request=Depends(_scope),
    db: Session = Depends(get_db),
):
    filters, scope, prices, _, ctx = request
    record_access_log(ctx, "analytics_pool_members", "purchases/analytics", {"pool_id": pool_id})
    return _run(
        lambda: service.peers(
            db,
            filters,
            scope,
            pool_id=pool_id,
            unit=(unit or "").strip() or None,
            page=page,
            page_size=page_size,
            price_visible=prices,
        )
    )
