"""采购记录 API（合同重点：销售和采购都能直接看最近采购）。

页面级准入 require_page("page_purchases")：销售/采购/老板/只读/管理员均 page_purchases=True
可达（甲方 2026-06-15 确认销售也要能查采购记录）。字段级仍过 apply_field_visibility：
未授权角色的供应商等仍被遮（销售 data_supplier=False → 供应商列为空，但进价可见）。

采购分析（D-39，客户需求 Round1）挂在独立页面键 page_purchase_analytics（模板仅 boss，
admin 由 page_permission_allowed 恒放行），与上面的采购记录页共用本路由前缀。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.api.query_bounds import ANALYTICS_PAGE_MAX, ANALYTICS_PAGE_SIZE_MAX, PG_INT_MAX, DbId
from app import config
from app.auth import current_role
from app.db import get_db
from app.security import (UserContext, apply_field_visibility, get_current_user_context,
                          is_field_hidden, is_scoped_sales, record_access_log, require_page)
from app.services import purchase_analysis, purchase_analytics, purchase_query

router = APIRouter(prefix="/purchases", tags=["purchases"])


@router.get("/recent")
def recent(
    q: str | None = Query(None, description="型号/描述/品牌关键词"),
    days: int = Query(30, ge=1, le=3660),
    supplier: str | None = Query(None),
    status: str | None = Query(None, description="流程状态过滤：留空=仅已生效；'全部'=不限；或具体状态(已取消/进行中/草稿/已生效)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchases")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "recent", "purchases",
                      {"q": q, "days": days, "supplier": supplier, "status": status})
    data = purchase_query.recent_purchases(db, ctx, q=q, days=days,
                                           supplier=supplier, page=page,
                                           page_size=page_size, status=status)
    return apply_field_visibility(data, ctx)


@router.get("/cancellation-stats")
def cancellation_stats(
    granularity: str = Query("month", description="统计粒度：month/quarter/year"),
    days: int | None = Query(None, ge=1, le=3660, description="仅统计近 N 天；留空=全部历史"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchases")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """采购单按期间×状态统计：多少单被取消/作废（采购未成功）。供宋总按月/季/年看趋势。"""
    record_access_log(ctx, "cancellation_stats", "purchases",
                      {"granularity": granularity, "days": days})
    data = purchase_analysis.cancellation_stats(db, ctx, granularity=granularity, days=days)
    return apply_field_visibility(data, ctx)


@router.get("/analysis")
def analysis(
    days: int = Query(7, ge=1, le=366, description="时间窗（天）"),
    exclude_designated: bool = Query(True, description="排除指定采购（大额批量补库）"),
    freq_threshold: int = Query(3, ge=1, le=100, description="频发判定次数阈值"),
    q: str | None = Query(None, description="型号/描述/品牌关键词"),
    supplier: str | None = Query(None),
    purchaser: str | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchases")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """采购分析面板：逐型号频次/总量/逐日/价格/来源拆分 + KPI 概览 + 来源构成。"""
    record_access_log(ctx, "analysis", "purchases", {"days": days, "q": q})
    data = purchase_analysis.analysis(
        db, ctx, days=days, exclude_designated=exclude_designated,
        freq_threshold=freq_threshold, q=q, supplier=supplier, purchaser=purchaser)
    return apply_field_visibility(data, ctx)


@router.get("/analysis/part")
def analysis_part(
    part_id: int = Query(..., ge=1, description="型号 part_id（主排行行展开）"),
    days: int = Query(7, ge=1, le=366),
    exclude_designated: bool = Query(True, description="与主排行一致，默认排除指定采购"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchases")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """单型号逐笔采购（横向比价：谁采的/哪家/含税未税/多少钱）。"""
    record_access_log(ctx, "analysis_part", "purchases", {"part_id": part_id, "days": days})
    data = purchase_analysis.part_purchases(
        db, ctx, part_id=part_id, days=days, exclude_designated=exclude_designated)
    return apply_field_visibility(data, ctx)


# ------------------------------------------------------------------ 采购分析（D-39）

_GROUP_BY_PATTERN = "^(source_type|purchaser|salesperson|customer|category|month|day)$"


def _analytics_common(
    date_from: date | None = Query(None, description="起始日期（闭区间，含当日）；不传=全部"),
    date_to: date | None = Query(None, description="结束日期（闭区间，含当日）；不传=全部"),
    source_type: list[str] = Query([], description="采购类型多选（source_type_raw 原值精确匹配）"),
    purchaser: list[str] = Query([], description="采购员多选（精确匹配）"),
    salesperson: list[str] = Query([], description="关联销售单销售人员多选"),
    customer_id: list[DbId] = Query([], description="关联销售单客户 id 多选（1..2^31-1）"),
    category: list[str] = Query([], description="分类多选（dim_part.category_major；「未分类」为哨兵值）"),
) -> dict:
    """采购分析共用筛选（日期预设由前端折算，后端只收 date_from/date_to）。"""
    return {
        "date_from": date_from, "date_to": date_to,
        "source_type": source_type, "purchaser": purchaser,
        "salesperson": salesperson, "customer_id": customer_id,
        "category": category,
    }


def _guard_customer_filter(filters: dict, ctx: UserContext,
                           group_by: str | None = None) -> None:
    """data_customer 不可见时不允许按客户筛选/分组/下钻（dashboard 同款 fail-closed；
    本页面向老板/管理员的全程口径，不像 dashboard 给 scoped sales 留口）。"""
    hidden = is_field_hidden(ctx, "customer_id")
    if hidden and filters["customer_id"]:
        raise HTTPException(status_code=403, detail="当前账号无客户信息权限，不能按客户筛选")
    if hidden and group_by == "customer":
        raise HTTPException(status_code=403, detail="当前账号无客户信息权限，不能按客户分组")


def _guard_scoped(ctx: UserContext) -> None:
    """own_customers_only 收紧账号不进本页：分析查询是全公司聚合，没有按客户收紧的
    数据面，fail-closed（比 dashboard 更严——那边明细本身按 scope 裁剪过）。

    角色硬校验（2026-10-22 轮复审新增）：仅 admin/boss 可访问——页面键控制菜单，
    角色硬界控制数据面；purchaser / readonly 即使被显式授予 ``page_purchase_analytics``
    也 403。ENABLE_RBAC=False（PHASE1 旁路）不受影响。"""
    if config.ENABLE_RBAC and ctx.role not in ("admin", "boss"):
        raise HTTPException(status_code=403, detail="采购分析仅限管理员与老板访问")
    if is_scoped_sales(ctx):
        raise HTTPException(status_code=403, detail="采购分析为全公司口径页面，收紧账号不可访问")


def _run(fn):
    """服务层 FilterValidationError（越过了 Query pattern 兜底的非法参数）→ 422。"""
    try:
        return fn()
    except purchase_analytics.FilterValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/analytics/options")
def analytics_options(
    selected_customer_id: list[DbId] = Query([], max_length=ANALYTICS_PAGE_SIZE_MAX,
                                                description="补齐已选客户名称，最多200个合法id"),
    customer_q: str = Query("", max_length=200, description="客户名称包含搜索（字面值）"),
    customer_page: int = Query(1, ge=1, le=ANALYTICS_PAGE_MAX),
    customer_page_size: int = Query(200, ge=1, le=ANALYTICS_PAGE_SIZE_MAX),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchase_analytics")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """采购分析筛选取值；客户按名称/id稳定排序，可搜索/分页，每页最多200项。

    客户候选沿用 summary 的身份规则与 data_customer 权限，不汇总金额。
    """
    _guard_scoped(ctx)
    record_access_log(ctx, "analytics_options", "purchases/analytics", {})
    data = purchase_analytics.options(
        db, ctx, customer_q=customer_q, customer_page=customer_page,
        customer_page_size=customer_page_size, selected_customer_ids=selected_customer_id)
    return apply_field_visibility(data, ctx)


@router.get("/analytics/summary")
def analytics_summary(
    filters: dict = Depends(_analytics_common),
    group_by: str = Query("source_type", pattern=_GROUP_BY_PATTERN,
                          description="分组维度；category 为行级口径"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchase_analytics")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """KPI + 分组汇总（三口径一次全返回；分组合计=KPI，供前端核对）。"""
    _guard_scoped(ctx)
    _guard_customer_filter(filters, ctx, group_by=group_by)
    record_access_log(ctx, "analytics_summary", "purchases/analytics", dict(filters))
    data = _run(lambda: purchase_analytics.summary(db, ctx, group_by=group_by, **filters))
    return apply_field_visibility(data, ctx)


@router.get("/analytics/orders")
def analytics_orders(
    filters: dict = Depends(_analytics_common),
    group_by: str | None = Query(None, pattern=_GROUP_BY_PATTERN,
                                 description="与上层分组维度同传，配合 group_key 下钻"),
    group_key: str | None = Query(None, description="分组值（哨兵值/月份 YYYY-MM/日 YYYY-MM-DD）"),
    sort: str = Query("order_date", pattern="^(order_date|amount_inc|amount_ex|amount_paid)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1, le=ANALYTICS_PAGE_MAX),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchase_analytics")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """分组下钻订单列表（分页+排序，同额/同日按 id 倒序稳定翻页）。"""
    _guard_scoped(ctx)
    _guard_customer_filter(filters, ctx, group_by=group_by)
    record_access_log(ctx, "analytics_orders", "purchases/analytics",
                      {**filters, "group_by": group_by, "group_key": group_key,
                       "sort": sort, "order": order, "page": page, "page_size": page_size})
    data = _run(lambda: purchase_analytics.orders(
        db, ctx, group_by=group_by, group_key=group_key, sort=sort, order=order,
        page=page, page_size=page_size, **filters))
    return apply_field_visibility(data, ctx)


@router.get("/analytics/orders/{order_id}")
def analytics_order_detail(
    order_id: int = Path(..., ge=1, le=PG_INT_MAX),
    category: list[str] = Query([], description="传入时行按分类过滤并附 category_matched 汇总"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchase_analytics")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """订单明细（自有实现，不依赖 page_boss_board）：单头三口径 + 单头批次行。"""
    _guard_scoped(ctx)
    record_access_log(ctx, "analytics_order_detail", "purchases/analytics",
                      {"order_id": order_id, "category": category})
    data = purchase_analytics.order_detail(db, order_id, ctx, category=category)
    if data is None:
        raise HTTPException(status_code=404, detail="订单不存在或不是已生效采购单")
    return apply_field_visibility(data, ctx)
