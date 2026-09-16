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
from app.services.maintenance_boss_board import (
    BUSINESS_TYPE_FILTER_PATTERN,
    can_view_cost,
)

router = APIRouter(prefix="/maintenance/analytics", tags=["maintenance"])


@router.get("/pn-ranking")
def pn_ranking(
    response: Response,
    range_: str = Query("ytd", alias="range", pattern="^(ytd|12m|all|custom)$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None, max_length=128),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    project: str | None = Query(None, max_length=4000),
    customer: str | None = Query(None, max_length=128),
    sp: str | None = Query(None, max_length=128),
    order_no: str | None = Query(None, max_length=128),
    demand_type: str = Query(
        "all", pattern="^(all|(repair|stock)(,(repair|stock))*)$", max_length=128
    ),
    warehouse: str | None = Query(None, max_length=1024),
    cost_source: str = Query(
        "all",
        pattern="^(all|(linked|estimated|manual|missing)(,(linked|estimated|manual|missing))*)$",
        max_length=128,
    ),
    sort: str = Query(
        "cost_inc",
        pattern="^(cost_inc|cost_ex|qty|return_qty|effective_qty|occurrences|order_count|project_count|monthly_avg|bad_qty|bad_rate|missing_lines|cost_share|pn)$",
    ),
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
    全字段筛选（项目/客户/销售/单号/需求类型/仓库/取价来源）在服务层聚合前
    生效；非法码或超限返回 422。
    """
    response.headers["Cache-Control"] = "no-store"
    can_cost = can_view_cost(ctx)
    if sort in maintenance_analytics.COST_SORTS and not can_cost:
        record_access_log(
            ctx, "maintenance_analytics_sort_denied", "pn_ranking", {"sort": sort}
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "sort_requires_cost_permission",
                "message": "按成本排序需要成本查看权限（data_purchase_cost）",
            },
        )
    try:
        project_ids = maintenance_analytics.parse_csv_items(
            project, max_items=50, max_len=64, field="project"
        )
        warehouse_values = maintenance_analytics.parse_csv_items(
            warehouse, max_items=10, max_len=64, field="warehouse", trim=False
        )
        demand_codes = maintenance_analytics.parse_csv_items(
            None if demand_type == "all" else demand_type,
            max_items=2,
            max_len=16,
            field="demand_type",
        )
        cost_sources = maintenance_analytics.parse_csv_items(
            None if cost_source == "all" else cost_source,
            max_items=4,
            max_len=16,
            field="cost_source",
        )
    except maintenance_analytics.AnalyticsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    demand_types = (
        [maintenance_analytics.DEMAND_TYPE_LITERALS[code] for code in demand_codes]
        if demand_codes
        else None
    )
    # 2026-08-21 行键收敛：own_maintenance_projects_only 开 → 负责人∪销售范围
    from app.services import maintenance_project_assignments

    allowed = maintenance_project_assignments.maintenance_scope_project_ids(db, ctx)
    record_access_log(
        ctx,
        "maintenance_analytics_pn_ranking",
        "pn_ranking",
        {
            "q": bool(q and q.strip()),
            "sort": sort,
            "range": range_,
            "business_type": business_type,
            "scope": "full" if allowed is None else "scoped",
            # 只记标志位，不落客户/销售/单号等自由文本
            "project": bool(project_ids),
            "customer": bool(customer),
            "salesperson": bool(sp),
            "order_no": bool(order_no),
            "demand_type": bool(demand_codes),
            "warehouse": bool(warehouse_values),
            "cost_source": bool(cost_sources),
        },
    )
    try:
        return maintenance_analytics.pn_ranking(
            db,
            range_=range_,
            date_from=date_from,
            date_to=date_to,
            q=q,
            sort=sort,
            page=page,
            page_size=page_size,
            business_type=business_type,
            project_ids=project_ids,
            customer=customer,
            salesperson=sp,
            order_no=order_no,
            demand_types=demand_types,
            warehouses=warehouse_values,
            cost_sources=cost_sources,
            can_cost=can_cost,
            allowed_project_ids=allowed,
        )
    except maintenance_analytics.AnalyticsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/spend-trend")
def spend_trend(
    response: Response,
    range_: str = Query("ytd", alias="range", pattern="^(ytd|12m|all|custom)$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    granularity: str = Query("month", pattern="^(day|week|month|year)$"),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    project: str | None = Query(None, max_length=4000),
    customer: str | None = Query(None, max_length=128),
    sp: str | None = Query(None, max_length=128),
    order_no: str | None = Query(None, max_length=128),
    demand_type: str = Query(
        "all", pattern="^(all|(repair|stock)(,(repair|stock))*)$", max_length=128
    ),
    warehouse: str | None = Query(None, max_length=1024),
    cost_source: str = Query(
        "all",
        pattern="^(all|(linked|estimated|manual|missing)(,(linked|estimated|manual|missing))*)$",
        max_length=128,
    ),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """开支统计：按期（日/周/月/年）× 业务类型 × 销售拆分已知成本。

    筛选参数、解析与 422 语义与 pn-ranking 完全一致；成本列挂
    data_purchase_cost 权限（ready/restricted/not_imported 三态信封）。
    审计只记粒度与布尔标志，不落客户/销售/单号等自由文本。
    """
    response.headers["Cache-Control"] = "no-store"
    can_cost = can_view_cost(ctx)
    try:
        project_ids = maintenance_analytics.parse_csv_items(
            project, max_items=50, max_len=64, field="project"
        )
        warehouse_values = maintenance_analytics.parse_csv_items(
            warehouse, max_items=10, max_len=64, field="warehouse", trim=False
        )
        demand_codes = maintenance_analytics.parse_csv_items(
            None if demand_type == "all" else demand_type,
            max_items=2,
            max_len=16,
            field="demand_type",
        )
        cost_sources = maintenance_analytics.parse_csv_items(
            None if cost_source == "all" else cost_source,
            max_items=4,
            max_len=16,
            field="cost_source",
        )
    except maintenance_analytics.AnalyticsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    demand_types = (
        [maintenance_analytics.DEMAND_TYPE_LITERALS[code] for code in demand_codes]
        if demand_codes
        else None
    )
    # 2026-08-21 行键收敛：own_maintenance_projects_only 开 → 负责人∪销售范围
    from app.services import maintenance_project_assignments

    allowed = maintenance_project_assignments.maintenance_scope_project_ids(db, ctx)
    record_access_log(
        ctx,
        "maintenance_analytics_spend_trend",
        "spend_trend",
        {
            "granularity": granularity,
            "range": range_,
            "business_type": business_type,
            "scope": "full" if allowed is None else "scoped",
            # 只记标志位，不落客户/销售/单号等自由文本
            "project": bool(project_ids),
            "customer": bool(customer),
            "salesperson": bool(sp),
            "order_no": bool(order_no),
            "demand_type": bool(demand_codes),
            "warehouse": bool(warehouse_values),
            "cost_source": bool(cost_sources),
        },
    )
    try:
        return maintenance_analytics.spend_trend(
            db,
            range_=range_,
            date_from=date_from,
            date_to=date_to,
            granularity=granularity,
            business_type=business_type,
            project_ids=project_ids,
            customer=customer,
            salesperson=sp,
            order_no=order_no,
            demand_types=demand_types,
            warehouses=warehouse_values,
            cost_sources=cost_sources,
            can_cost=can_cost,
            allowed_project_ids=allowed,
        )
    except maintenance_analytics.AnalyticsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/filter-options")
def filter_options(
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """筛选器候选项（仓库）：随行键范围收敛，不泄露范围外仓库。"""
    response.headers["Cache-Control"] = "no-store"
    # 2026-08-21 行键收敛：与 pn-ranking 同一可见项目集
    from app.services import maintenance_project_assignments

    allowed = maintenance_project_assignments.maintenance_scope_project_ids(db, ctx)
    record_access_log(
        ctx,
        "maintenance_analytics_filter_options",
        "filter_options",
        {"scope": "full" if allowed is None else "scoped"},
    )
    return maintenance_analytics.filter_options(db, allowed_project_ids=allowed)
