"""维保数据分析看板 API：PN 成本排名与损坏频率（2026-08-21）。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import maintenance_analytics
from app.services.maintenance_boss_board import can_view_cost

router = APIRouter(prefix="/maintenance/analytics", tags=["maintenance"])


@router.get("/pn-ranking")
def pn_ranking(
    response: Response,
    range_: str = Query("ytd", alias="range", pattern="^(ytd|12m|all|custom)$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None, max_length=128),
    sort: str = Query("cost_inc",
                      pattern="^(cost_inc|cost_ex|qty|occurrences|bad_qty)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """全项目 PN 维度：备件消耗成本排名 + 损坏频率（含 RKD 坏件佐证）。

    成本列挂 data_purchase_cost 权限（restricted 信封）；无权限按成本排序
    返回 422（不静默降级——boss-board 同款）。
    """
    response.headers["Cache-Control"] = "no-store"
    can_cost = can_view_cost(ctx)
    if sort in maintenance_analytics.COST_SORTS and not can_cost:
        record_access_log(ctx, "maintenance_analytics_sort_denied",
                          "pn_ranking", {"sort": sort})
        raise HTTPException(
            status_code=422,
            detail={"code": "sort_requires_cost_permission",
                    "message": "按成本排序需要成本查看权限（data_purchase_cost）"},
        )
    record_access_log(ctx, "maintenance_analytics_pn_ranking", "pn_ranking",
                      {"q": bool(q and q.strip()), "sort": sort, "range": range_,
                       "scope": "full"})
    try:
        return maintenance_analytics.pn_ranking(
            db, range_=range_, date_from=date_from, date_to=date_to,
            q=q, sort=sort, page=page, page_size=page_size,
            can_cost=can_cost)
    except maintenance_analytics.AnalyticsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
