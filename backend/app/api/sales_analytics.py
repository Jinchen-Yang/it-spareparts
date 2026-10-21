"""销售分析页 API（D-41 §1，PR-3b；D-44 销售看板扩展）：仅 admin/boss（页面键 page_sales_analysis）。

D-44 起所有端点共用 ``sales_filters`` 依赖（客户规范名 / 客户类型 / 销售人员多选、
金额区间与正负 / 空值开关），订单列表任意列升降序，详情带明细行，KPI 带毛利。

只读。不改动老板看板 /dashboard/sales 与 OrdersBlock（决策 2026-10-17 Q3）。
对账字段受 data_reconciliation 数据权限组控制（D-40 §6）：关闭时经
apply_field_visibility 统一置空（键名登记见 config.FIELD_GROUPS["sales_reconciliation"]），
且 orders/order_detail 的对账整块按「无快照」形态输出（has_data=False、history=[]、
不查快照表，#397 (2)）——连 has_data/reconciled_as_of 这类非金额字段也不泄漏。

准入附加规则（本页特有的两条硬界）：
- 受限销售（own_customers_only）即使被逐账号授予页面键也 403——本页是全公司
  经营视角（大单/客户），按 security 的逐单成交不可见口径，不能对受限销售开洞；
- 客户字段不可见的账号不能按客户 / 客户类型筛选（与 /dashboard/sales 同一先例，
  防用筛选结果数量探测客户名是否存在，D-44 §11）；
- 金额区间所选口径被遮蔽的账号不能按金额区间 / 正负 / 空值开关筛选（D-44 §7）。
"""
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.api.query_bounds import ANALYTICS_PAGE_MAX, PG_INT_MAX
from app.auth import current_role
from app.db import get_db
from app.security import (
    UserContext,
    apply_field_visibility,
    get_current_user_context,
    is_field_hidden,
    is_scoped_sales,
    record_access_log,
    require_page,
)
from app.services import sales_analytics, sales_dashboard

router = APIRouter(prefix="/sales/analytics", tags=["sales-analytics"])

AMOUNT_BASIS_PATTERN = "^(amount_ex_tax|amount_inc_tax|revenue)$"
# 金额区间上限（Numeric(18,2) 量级内，防超大字面量拖慢比较）
AMOUNT_BOUND = Decimal("1e15")


@dataclass
class SalesFilters:
    """公共筛选参数（D-41 + D-44）：所有端点同一套解析，传给服务层 _order_query。"""
    date_from: date | None
    date_to: date | None
    business_types: list[str] | None
    customer: str | None
    salesperson: str | None
    customers: list[str] | None
    customer_types: list[str] | None
    salespeople: list[str] | None
    amount_basis: str
    amount_min: Decimal | None
    amount_max: Decimal | None
    include_negative: bool
    include_empty: bool
    categories: list[str] | None = None
    brands: list[str] | None = None

    @property
    def uses_customer(self) -> bool:
        return bool((self.customer and self.customer.strip())
                    or sales_analytics._clean_multi(self.customers)
                    or sales_analytics._clean_multi(self.customer_types))

    @property
    def uses_amount(self) -> bool:
        return (self.amount_min is not None or self.amount_max is not None
                or not self.include_negative or not self.include_empty)

    def log_echo(self) -> dict:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, (date, Decimal)) else v) for k, v in d.items()}


def sales_filters(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    business_type: list[str] | None = Query(None, description="业务类型多选（重复参数）"),
    customer: str | None = Query(None, description="客户名模糊匹配（旧参数，兼容保留）"),
    salesperson: str | None = Query(None, description="销售人员模糊匹配（旧参数，兼容保留）"),
    customer_name: list[str] | None = Query(
        None, description="客户规范名多选精确匹配（D-44 §12，重复参数）"),
    customer_type: list[str] | None = Query(None, description="客户类型多选（D-44 §2）"),
    salesperson_name: list[str] | None = Query(None, description="销售人员多选精确匹配"),
    amount_basis: str = Query("amount_ex_tax", pattern=AMOUNT_BASIS_PATTERN,
                              description="金额区间比较口径（D-44 §7）"),
    amount_min: Decimal | None = Query(None, ge=-AMOUNT_BOUND, le=AMOUNT_BOUND),
    amount_max: Decimal | None = Query(None, ge=-AMOUNT_BOUND, le=AMOUNT_BOUND),
    include_negative: bool = Query(True, description="含负金额单（D-44 §10，默认含）"),
    include_empty: bool = Query(True, description="含无金额单（D-44 §10，默认含）"),
    category: list[str] | None = Query(
        None, description="商品大类多选（D-44 §8 行级口径；「未分类」匹配空值）"),
    brand: list[str] | None = Query(None, description="品牌多选（D-44 §8 行级口径）"),
) -> SalesFilters:
    return SalesFilters(
        date_from=date_from, date_to=date_to, business_types=business_type,
        customer=customer, salesperson=salesperson, customers=customer_name,
        customer_types=customer_type, salespeople=salesperson_name,
        amount_basis=amount_basis, amount_min=amount_min, amount_max=amount_max,
        include_negative=include_negative, include_empty=include_empty,
        categories=category, brands=brand,
    )


def _guards(ctx: UserContext, f: SalesFilters | None = None) -> None:
    """前置硬界（在任何业务 SQL 前执行，防存在性侧信道）。所有端点统一调用。"""
    if is_scoped_sales(ctx):
        raise HTTPException(
            status_code=403,
            detail="销售分析为全公司经营视角，受限销售账号不可访问",
        )
    if f is None:
        return
    if f.date_from is not None and f.date_to is not None and f.date_from > f.date_to:
        raise HTTPException(
            status_code=422, detail="date_from 不能晚于 date_to（闭区间）"
        )
    if f.amount_min is not None and f.amount_max is not None and f.amount_min > f.amount_max:
        raise HTTPException(
            status_code=422, detail="amount_min 不能大于 amount_max（闭区间）"
        )
    # 客户类属筛选（客户名 / 规范名 / 客户类型）：与 /dashboard/sales 同一先例，
    # 防用筛选结果数量探测客户是否存在（D-44 §11）
    if f.uses_customer and is_field_hidden(ctx, "customer"):
        raise HTTPException(
            status_code=403, detail="当前账号无客户信息权限，不能按客户筛选"
        )
    # 金额区间与正负/空值开关：所选口径被遮蔽时，筛选结果的单数本身就泄漏金额
    # 分布（D-44 §7），fail-closed 403
    if f.uses_amount and is_field_hidden(ctx, f.amount_basis):
        raise HTTPException(
            status_code=403, detail="当前账号无该金额口径权限，不能按金额区间筛选"
        )


def _service_filters(f: SalesFilters) -> dict:
    return asdict(f)


@router.get("/options")
def business_type_options(
    customer_q: str = Query("", max_length=100, description="客户名关键字补全"),
    f: SalesFilters = Depends(sales_filters),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """筛选候选：业务类型（全时间计数，验收对账用）+ D-44 新增的客户类型、销售人员、
    客户补全与当前筛选下的金额上限（拖动条右端）。"""
    _guards(ctx, f)
    record_access_log(ctx, "sales_analytics_options", "sales_analytics", None)
    data = sales_analytics.business_type_options(db)
    data.update(sales_analytics.filter_options(
        db, customer_visible=not is_field_hidden(ctx, "customer"),
        customer_q=customer_q, **_service_filters(f),
    ))
    return apply_field_visibility(data, ctx)


@router.get("/summary")
def summary(
    f: SalesFilters = Depends(sales_filters),
    compare: bool = Query(False, description="附上月同期 KPI（D-44 §9）"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """区间 KPI：订单数 + 三金额合计（含 amount_inc_tax 缺失单数）+ 客户数 / 销售人数 + 毛利
    + 新客 / 老客；compare=true 时附上月同期 KPI（区间长于一个月不提供）。"""
    _guards(ctx, f)
    record_access_log(ctx, "sales_analytics_summary", "sales_analytics", {
        **f.log_echo(), "compare": compare})
    filters = _service_filters(f)
    data = sales_analytics.summary(db, **filters)
    data["kpi"].update(sales_dashboard.customer_lifecycle(db, **filters))
    data["compare"] = sales_dashboard.summary_compare(db, **filters) if compare else None
    data["compare_available"] = sales_dashboard.compare_window(f.date_from, f.date_to) is not None
    return apply_field_visibility(data, ctx)


def effective_sort(ctx: UserContext, sort: str) -> str:
    """行序防泄漏（dashboard ranking_restricted 同款先例）：排序键被遮蔽时，即便值已
    置 null，按真实值排序的行序本身仍泄漏排名——金额 / 毛利退回公开口径 revenue，
    客户退回订单日期。"""
    if sort in ("amount_ex_tax", "amount_inc_tax", "gross_profit") and is_field_hidden(ctx, sort):
        return "revenue"
    if sort == "customer" and is_field_hidden(ctx, "customer"):
        return "order_date"
    return sort


@router.get("/orders")
def orders(
    f: SalesFilters = Depends(sales_filters),
    sort: str = Query(
        "amount_ex_tax",
        pattern="^(amount_ex_tax|amount_inc_tax|revenue|gross_profit|order_date|customer|salesperson)$",
    ),
    order: str = Query("desc", pattern="^(asc|desc)$", description="升序 / 降序（D-44 §4）"),
    page: int = Query(1, ge=1, le=ANALYTICS_PAGE_MAX),
    page_size: int = Query(50, ge=1, le=200),
    include_returns: bool = Query(
        False, description="对账展示含退货已对账（D-40 §5，默认不含）"
    ),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """订单列表：任意列升降序 + 分页；每单挂最新对账快照块。"""
    _guards(ctx, f)
    sort = effective_sort(ctx, sort)
    record_access_log(ctx, "sales_analytics_orders", "sales_analytics", {
        **f.log_echo(), "sort": sort, "order": order, "include_returns": include_returns,
    })
    data = sales_analytics.orders(
        db, ctx, sort=sort, order=order, page=page, page_size=page_size,
        include_returns=include_returns, **_service_filters(f),
    )
    return apply_field_visibility(data, ctx)


def _public_basis(ctx: UserContext, f: SalesFilters) -> SalesFilters:
    """占比 / 单均 / 集中度按 amount_basis 计算：该口径被遮蔽时强制退回公开口径
    revenue（金额区间筛选在 _guards 已 403，强制改口径不改变筛选结果）。"""
    if is_field_hidden(ctx, f.amount_basis):
        f.amount_basis = "revenue"
    return f


@router.get("/groups")
def groups(
    group_by: str = Query(..., pattern="^(" + "|".join(sales_dashboard.ALL_GROUP_DIMS) + ")$"),
    sort: str = Query("amount_ex_tax", pattern="^(" + "|".join(sales_dashboard.GROUP_SORTS) + ")$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    top: int = Query(20, ge=0, le=500, description="Top N，0 = 全部"),
    compare: bool = Query(False, description="每组附上月同期指标（D-44 §9）"),
    f: SalesFilters = Depends(sales_filters),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """分组排行（D-44 方案 §3.3）：客户 / 客户类型 / 城市 / 来源 / 销售 / 业务类型 / 仓库，
    以及行级的商品大类 / 品牌 / PN（D-44 §8）。"""
    _guards(ctx, f)
    # 客户类属分组：无客户权限 403（D-44 §11，组键本身就是客户信息）
    if group_by in sales_dashboard.CUSTOMER_DIMS and is_field_hidden(ctx, "customer"):
        raise HTTPException(status_code=403, detail="当前账号无客户信息权限，不能按客户类属分组")
    # 组序防泄漏：被遮蔽的金额 / 毛利排序退回单数（D-39 组序保护同款）
    if sort in ("amount_ex_tax", "amount_inc_tax", "gross_profit") and is_field_hidden(ctx, sort):
        sort = "orders"
    f = _public_basis(ctx, f)
    record_access_log(ctx, "sales_analytics_groups", "sales_analytics", {
        **f.log_echo(), "group_by": group_by, "sort": sort, "order": order,
        "top": top, "compare": compare,
    })
    data = sales_dashboard.groups(
        db, group_by=group_by, sort=sort, order=order, top=top, compare=compare,
        **_service_filters(f))
    return apply_field_visibility(data, ctx)


@router.get("/trend")
def trend(
    granularity: str = Query("day", pattern="^(day|week|month)$"),
    compare: bool = Query(False, description="附上月同期序列（D-44 §9）"),
    f: SalesFilters = Depends(sales_filters),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """时间趋势（D-44 方案 §3.4）：日 / 周 / 月完整桶序列、上月对比序列、星期分布。"""
    _guards(ctx, f)
    f = _public_basis(ctx, f)
    record_access_log(ctx, "sales_analytics_trend", "sales_analytics", {
        **f.log_echo(), "granularity": granularity, "compare": compare,
    })
    try:
        data = sales_dashboard.trend(
            db, granularity=granularity, compare=compare, **_service_filters(f))
    except sales_dashboard.DashboardValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return apply_field_visibility(data, ctx)


@router.get("/orders/{order_id}")
def order_detail(
    order_id: int = Path(..., ge=1, le=PG_INT_MAX),
    include_returns: bool = Query(False, description="对账展示含退货已对账"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """订单下钻：三金额 + 最新对账块 + 对账历史（每个快照一条）。"""
    _guards(ctx)
    record_access_log(ctx, "sales_analytics_order_detail", "sales_analytics", {
        "order_id": order_id, "include_returns": include_returns,
    })
    data = sales_analytics.order_detail(
        db, order_id, ctx, include_returns=include_returns)
    if data is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return apply_field_visibility(data, ctx)
