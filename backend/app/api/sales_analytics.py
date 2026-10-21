"""销售分析页 API（D-41 §1，PR-3b；D-44 销售看板扩展，2026-10-22 轮复审修订）：
仅 admin/boss（页面键 page_sales_analysis）。

D-44 起所有端点共用 ``sales_filters`` 依赖（客户规范名 / 客户类型 / 销售人员多选、
金额区间与正负 / 空值开关），订单列表任意可见列升降序，详情带明细行，KPI 带毛利。

只读。不改动老板看板 /dashboard/sales 与 OrdersBlock（决策 2026-10-17 Q3）。
对账字段受 data_reconciliation 数据权限组控制（D-40 §6）：关闭时经
apply_field_visibility 统一置空（键名登记见 config.FIELD_GROUPS["sales_reconciliation"]），
且 orders/order_detail 的对账整块按「无快照」形态输出（has_data=False、history=[]、
不查快照表，#397 (2)）——连 has_data/reconciled_as_of 这类非金额字段也不泄漏。

准入附加规则（本页特有的硬界，本轮复审后）：
- **角色硬校验**：仅 admin/boss 可访问（RBAC 开启时在 API 层强制）。purchaser /
  readonly 即使被显式授予 ``page_sales_analysis`` 页面键也 403——页面键控制菜单，
  角色硬界控制数据面；PHASE1 旁路（ENABLE_RBAC=False）不受影响；
- 受限销售（own_customers_only）即使被逐账号授予页面键也 403——本页是全公司
  经营视角（大单/客户），按 security 的逐单成交不可见口径，不能对受限销售开洞；
- 客户字段不可见的账号不能按客户 / 客户类型筛选或分组（与 /dashboard/sales 同一
  先例，防用筛选结果数量探测客户名是否存在，D-44 §11）；
- **金额信息整体走成本权限（Q-A-03/A-AC03 修订）**：无 data_purchase_cost 时，
  三种口径的金额区间 / 正负 / 空值开关筛选与金额分布直方图一律 403、options 的
  金额上限全部 null——营收口径虽公开，区间结果的单数同样泄漏金额分布，无例外。

脱敏结构收敛（DL-006/DL-007 修订）：``revenue_costed`` / ``cost_coverage`` 不做全局
键登记（老板看板 /dashboard/sales 同名键对 boss 公开，全局登记会连带改变看板与
/api/profit 的既有契约），由本模块在 data_profit 不可见时**结构化置 null**（先例：
/api/profit 对 revenue_costed 的模块级收敛，test_agent_harden._ALLOW 已注明）。
gross_profit / gross_margin 已登记 profit 组，走通用 apply_field_visibility。
同理，客户派生聚合键（customers / new_customers / returning_customers /
undetermined）在 data_customer 不可见时结构化置 null——聚合计数本身是客户情报。
"""
import csv
import io
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import config
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
from app.services.sales_export_text import ExportTextTooLong, safe_formula_text

router = APIRouter(prefix="/sales/analytics", tags=["sales-analytics"])

AMOUNT_BASIS_PATTERN = "^(amount_ex_tax|amount_inc_tax|revenue)$"
# 订单列表 / 导出排序键（D-44 §4 + 本轮复审：所有可见数据列，含订单号 / 业务类型 /
# 客户类型与对账三列）
ORDER_SORT_PATTERN = "^(" + "|".join(sales_analytics.ORDER_SORTS) + ")$"
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
        None, description="客户规范名多选精确匹配（D-44 §12，重复参数）；"
                          "传 __empty__ 筛「未填」桶（A-AC06）"),
    customer_type: list[str] | None = Query(
        None, description="客户类型多选（D-44 §2）；传 __empty__ 筛缺失"),
    salesperson_name: list[str] | None = Query(
        None, description="销售人员多选精确匹配；传 __empty__ 筛缺失"),
    amount_basis: str = Query("amount_ex_tax", pattern=AMOUNT_BASIS_PATTERN,
                              description="金额区间比较口径（D-44 §7）"),
    amount_min: Decimal | None = Query(None, ge=-AMOUNT_BOUND, le=AMOUNT_BOUND),
    amount_max: Decimal | None = Query(None, ge=-AMOUNT_BOUND, le=AMOUNT_BOUND),
    include_negative: bool = Query(True, description="含负金额单（D-44 §10，默认含；与有限区间同时生效=交集）"),
    include_empty: bool = Query(True, description="含无金额单（D-44 §10，默认含；有限区间下无金额单无法比较自然不入）"),
    category: list[str] | None = Query(
        None, description="商品大类多选（D-44 §8 行级口径；「未分类」匹配空值）"),
    brand: list[str] | None = Query(None, description="品牌多选（D-44 §8 行级口径）；传 __empty__ 筛缺失"),
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
    # 角色硬校验：页面键控制菜单，角色硬界控制数据面（本轮复审新增）。
    # ENABLE_RBAC=False（PHASE1 旁路）时放行，与全局开关语义一致。
    if config.ENABLE_RBAC and ctx.role not in ("admin", "boss"):
        raise HTTPException(status_code=403, detail="销售分析仅限管理员与老板访问")
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
    # 金额信息整体走成本权限（Q-A-03/A-AC03 修订）：无 data_purchase_cost 时三口径
    # 一视同仁——所选口径被遮蔽，或成本组键被遮蔽（basis=revenue 公开但成本不可见）
    # 都 403；筛选结果的单数本身就泄漏金额分布，fail-closed。
    if f.uses_amount and (
        is_field_hidden(ctx, f.amount_basis) or is_field_hidden(ctx, "amount_ex_tax")
    ):
        raise HTTPException(
            status_code=403, detail="当前账号无成本权限，不能按金额区间筛选"
        )


def _service_filters(f: SalesFilters) -> dict:
    return asdict(f)


def _cost_hidden(ctx: UserContext) -> bool:
    """金额信息可见性（Q-A-03）：与 _guards 的金额区间硬界同一判据。"""
    return is_field_hidden(ctx, "amount_ex_tax")


def _null_profit_keys(node) -> None:
    """递归把 revenue_costed / cost_coverage 置 null（data_profit 不可见时的结构收敛，
    见模块 docstring；不全局登记的原因：老板看板同名键公开）。"""
    if isinstance(node, dict):
        for k in ("revenue_costed", "cost_coverage"):
            if k in node:
                node[k] = None
        for v in node.values():
            _null_profit_keys(v)
    elif isinstance(node, list):
        for v in node:
            _null_profit_keys(v)


# 客户派生聚合键（预审线索 6 修复）：count(distinct customer) 及新老客拆分本身是
# 客户情报（基数 / 结构），data_customer 不可见时必须随逐行客户键一起收敛；不做
# 全局键登记的原因同 revenue_costed——通用 apply_field_visibility 按"逐键"遮蔽，
# 把聚合计数键登记进 customer_info 会连带改变其他模块同名键的既有契约。
_CUSTOMER_AGG_KEYS = ("customers", "new_customers", "returning_customers", "undetermined")


def _null_customer_keys(node) -> None:
    """递归把客户派生聚合**计数**置 null（summary KPI、lifecycle 拆分、分组行、
    趋势桶）。只动标量——options 的 ``customers`` 是候选**列表**（data_customer
    不可见时已被 customer_visible 门控成 []，防存在性探测的空表形态），不属收敛
    范围，不能覆盖成 null。"""
    if isinstance(node, dict):
        for k in _CUSTOMER_AGG_KEYS:
            v = node.get(k)
            if k in node and not isinstance(v, (dict, list)):
                node[k] = None
        for v in node.values():
            _null_customer_keys(v)
    elif isinstance(node, list):
        for v in node:
            _null_customer_keys(v)


def _finish(data, ctx: UserContext):
    """统一出口：通用键名脱敏 → 利润 / 客户聚合结构收敛。"""
    data = apply_field_visibility(data, ctx)
    if is_field_hidden(ctx, "gross_profit"):
        _null_profit_keys(data)
    if is_field_hidden(ctx, "customer_id"):
        _null_customer_keys(data)
    return data


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
    客户补全与当前筛选下的金额上限（拖动条右端）。无成本权限时金额上限全部 null。"""
    _guards(ctx, f)
    record_access_log(ctx, "sales_analytics_options", "sales_analytics", None)
    data = sales_analytics.business_type_options(db)
    data.update(sales_analytics.filter_options(
        db, customer_visible=not is_field_hidden(ctx, "customer"),
        amounts_visible=not _cost_hidden(ctx),
        customer_q=customer_q, **_service_filters(f),
    ))
    return _finish(data, ctx)


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
    return _finish(data, ctx)


def effective_sort(ctx: UserContext, sort: str) -> str:
    """行序防泄漏（dashboard ranking_restricted 同款先例）：排序键被遮蔽时，即便值已
    置 null，按真实值排序的行序本身仍泄漏排名——**单据表一律退回公开口径
    order_date + id**（Q-A-04 已批准口径：不退回 revenue，营收行序同样是排名信息；
    分组排行由 /groups 各自退回 orders 单数）。覆盖所有新增列（A-AC07：排序覆盖
    每个实际可见数据列，被遮蔽列的排序请求静默回退而非报错）。服务层 orders()
    有同判据双保险。"""
    if sort in ("amount_ex_tax", "amount_inc_tax", "gross_profit") and is_field_hidden(ctx, sort):
        return "order_date"
    if sort in ("customer", "customer_type") and is_field_hidden(ctx, "customer"):
        return "order_date"
    if sort in sales_analytics.RECON_SORTS and is_field_hidden(ctx, "reconciled_amount"):
        return "order_date"
    return sort


@router.get("/orders")
def orders(
    f: SalesFilters = Depends(sales_filters),
    sort: str = Query("amount_ex_tax", pattern=ORDER_SORT_PATTERN),
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
    """订单列表：任意可见列升降序 + 分页；每单挂最新对账快照块。"""
    _guards(ctx, f)
    sort = effective_sort(ctx, sort)
    record_access_log(ctx, "sales_analytics_orders", "sales_analytics", {
        **f.log_echo(), "sort": sort, "order": order, "include_returns": include_returns,
    })
    data = sales_analytics.orders(
        db, ctx, sort=sort, order=order, page=page, page_size=page_size,
        include_returns=include_returns, **_service_filters(f),
    )
    return _finish(data, ctx)


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
    以及行级的商品大类 / 品牌 / PN（D-44 §8）。缺失组 key=null + is_empty（A-AC06）。"""
    _guards(ctx, f)
    # 客户类属分组：无客户权限 403（D-44 §11，组键本身就是客户信息）
    if group_by in sales_dashboard.CUSTOMER_DIMS and is_field_hidden(ctx, "customer"):
        raise HTTPException(status_code=403, detail="当前账号无客户信息权限，不能按客户类属分组")
    # 组序防泄漏：被遮蔽的金额 / 毛利排序退回单数（D-39 组序保护同款）
    if sort in ("amount_ex_tax", "amount_inc_tax", "gross_profit") and is_field_hidden(ctx, sort):
        sort = "orders"
    # 客户数排序同款侧信道（反馈§4A / Q-A-04）：计数已在 _finish 置 null，但行序
    # 仍按真实客户数泄漏相对规模——退回公开单数并如实回显，不 403（与被遮金额
    # 排序同为静默回退）；服务层 groups(user_ctx=…) 同判据双保险
    if sort == "customers" and is_field_hidden(ctx, "customer"):
        sort = "orders"
    f = _public_basis(ctx, f)
    record_access_log(ctx, "sales_analytics_groups", "sales_analytics", {
        **f.log_echo(), "group_by": group_by, "sort": sort, "order": order,
        "top": top, "compare": compare,
    })
    try:
        data = sales_dashboard.groups(
            db, user_ctx=ctx, group_by=group_by, sort=sort, order=order, top=top,
            compare=compare, **_service_filters(f))
    except sales_dashboard.DashboardValidationError as exc:
        # 服务层组合校验（如 lines 排序只对行级维度有意义）→ 422，与 trend 同款
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _finish(data, ctx)


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
    """时间趋势（D-44 方案 §3.4）：日 / 周 / 月完整桶序列、上月对比序列、星期分布。

    下钻日期取请求的 date_from/date_to 交集（前端下钻只收窄区间，不扩大）。
    无日期订单以 no_date_orders 显式计数（A-AC05）。"""
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
    return _finish(data, ctx)


@router.get("/amount-histogram")
def amount_histogram(
    f: SalesFilters = Depends(sales_filters),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """金额拖动条上方的订单数分布（D-44 §7）。无成本权限时 403：分布本身就是金额
    信息，三口径一视同仁（Q-A-03/A-AC03 修订）。"""
    _guards(ctx, f)
    if _cost_hidden(ctx) or is_field_hidden(ctx, f.amount_basis):
        raise HTTPException(status_code=403, detail="当前账号无成本权限，不能查看金额分布")
    record_access_log(ctx, "sales_analytics_amount_histogram", "sales_analytics", f.log_echo())
    return _finish(sales_dashboard.amount_histogram(db, **_service_filters(f)), ctx)


def _sales_csv_stream(header: list, rows) -> StreamingResponse:
    """订单 CSV 流（D-33 防公式走独立模块 sales_export_text——生产基线的
    api.maintenance 无 _safe_formula_only，不可从该巨大 API 模块 import）。

    先完整写内存再响应（与 maintenance._csv_stream 的「完整校验成功后才开始响应」
    同语义）：单次导出 ≤ EXPORT_MAX_ROWS（1 万行），内存有界；BOM 供 Excel 识别
    UTF-8；单元格超 32,767 字符或含超深括号的对抗串 → 413，绝不 500。
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    try:
        buf.write("﻿")  # BOM，Excel 正确识别 UTF-8
        writer.writerow(header)
        for row in rows:
            writer.writerow([safe_formula_text(v) for v in row])
    except ExportTextTooLong as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="sales-orders.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/orders/export")
def orders_export(
    f: SalesFilters = Depends(sales_filters),
    sort: str = Query("amount_ex_tax", pattern=ORDER_SORT_PATTERN),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
):
    """订单 CSV 导出（D-44 方案 §3.6）：与列表同筛选、同排序、同脱敏；≤10,000 行。

    被遮蔽字段整列不导出（而非导出空列），排序键同列表的防泄漏回退。单元格文本经
    sales_export_text.safe_formula_text（D-33：= / @ / +/- 公式形态转义，负数与负 PN
    不误伤；控制字符剔除；32,767 上限 413；超深括号按公式转义不 500）。
    """
    _guards(ctx, f)
    sort = effective_sort(ctx, sort)
    record_access_log(ctx, "sales_analytics_orders_export", "sales_analytics", {
        **f.log_echo(), "sort": sort, "order": order,
    })
    total, items = sales_dashboard.export_orders(
        db, sort=sort, order=order, **_service_filters(f))
    if total > sales_dashboard.EXPORT_MAX_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"共 {total} 单，超过导出上限 {sales_dashboard.EXPORT_MAX_ROWS}，请收窄区间或筛选",
        )
    columns = [(k, label) for k, label in sales_dashboard.EXPORT_COLUMNS
               if not is_field_hidden(ctx, k)]
    # None → csv.writer 写空串（缺失不冒充 0）
    rows = ([item.get(k) for k, _label in columns] for item in items)
    return _sales_csv_stream([label for _k, label in columns], rows)


@router.get("/orders/{order_id}")
def order_detail(
    order_id: int = Path(..., ge=1, le=PG_INT_MAX),
    include_returns: bool = Query(False, description="对账展示含退货已对账"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """订单下钻：三金额 + 明细行 + 最新对账块 + 对账历史（每个快照一条）。

    无 data_purchase_cost 时明细行金额组合（数量/单价/行金额/营收）服务层已置空
    （A-AC01：阻断 qty × 营收 重建成交单价）。"""
    _guards(ctx)
    record_access_log(ctx, "sales_analytics_order_detail", "sales_analytics", {
        "order_id": order_id, "include_returns": include_returns,
    })
    data = sales_analytics.order_detail(
        db, order_id, ctx, include_returns=include_returns)
    if data is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return _finish(data, ctx)
