"""销售分析页 API（D-41 §1，PR-3b）：仅 admin/boss（页面键 page_sales_analysis）。

只读。不改动老板看板 /dashboard/sales 与 OrdersBlock（决策 2026-10-17 Q3）。
对账字段受 data_reconciliation 数据权限组控制（D-40 §6）：关闭时经
apply_field_visibility 统一置空（键名登记见 config.FIELD_GROUPS["sales_reconciliation"]）。

准入附加规则（本页特有的两条硬界）：
- 受限销售（own_customers_only）即使被逐账号授予页面键也 403——本页是全公司
  经营视角（大单/客户），按 security 的逐单成交不可见口径，不能对受限销售开洞；
- 客户字段不可见的账号不能按客户筛选（与 /dashboard/sales 同一先例，防用筛选
  结果数量探测客户名是否存在）。
"""
from datetime import date

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
from app.services import sales_analytics

router = APIRouter(prefix="/sales/analytics", tags=["sales-analytics"])


def _guards(ctx: UserContext, date_from: date | None = None,
            date_to: date | None = None, customer: str | None = None) -> None:
    """三条前置硬界（在任何业务 SQL 前执行，防存在性侧信道）。四个端点统一调用。"""
    if is_scoped_sales(ctx):
        raise HTTPException(
            status_code=403,
            detail="销售分析为全公司经营视角，受限销售账号不可访问",
        )
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=422, detail="date_from 不能晚于 date_to（闭区间）"
        )
    if customer and is_field_hidden(ctx, "customer"):
        raise HTTPException(
            status_code=403, detail="当前账号无客户信息权限，不能按客户筛选"
        )


@router.get("/options")
def business_type_options(
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """业务类型多选候选取值与计数（全时间、已生效口径，验收对账用）。"""
    _guards(ctx)
    record_access_log(ctx, "sales_analytics_options", "sales_analytics", None)
    return sales_analytics.business_type_options(db)


@router.get("/summary")
def summary(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    business_type: list[str] | None = Query(None, description="业务类型多选（重复参数）"),
    customer: str | None = Query(None),
    salesperson: str | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_sales_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """区间 KPI：订单数 + 三金额合计（含 amount_inc_tax 缺失单数）。"""
    _guards(ctx, date_from, date_to, customer)
    record_access_log(ctx, "sales_analytics_summary", "sales_analytics", {
        "date_from": str(date_from), "date_to": str(date_to),
        "business_type": business_type, "customer": customer,
    })
    data = sales_analytics.summary(
        db, date_from=date_from, date_to=date_to, business_types=business_type,
        customer=customer, salesperson=salesperson,
    )
    return apply_field_visibility(data, ctx)


@router.get("/orders")
def orders(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    business_type: list[str] | None = Query(None, description="业务类型多选（重复参数）"),
    customer: str | None = Query(None),
    salesperson: str | None = Query(None),
    sort: str = Query("amount_ex_tax", pattern="^(amount_ex_tax|amount_inc_tax|revenue)$"),
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
    """大单列表：按所选金额键倒序 + 分页；每单挂最新对账快照块。"""
    _guards(ctx, date_from, date_to, customer)
    # 行序防泄漏（dashboard ranking_restricted 同款先例）：两个金额键登记在
    # purchase_cost 组，对 data_purchase_cost=False 的账号即便值已遮蔽为 null，
    # 按真实金额排序的行序本身仍泄漏逐单金额排名——退回公开口径 revenue。
    if sort != "revenue" and is_field_hidden(ctx, sort):
        sort = "revenue"
    record_access_log(ctx, "sales_analytics_orders", "sales_analytics", {
        "date_from": str(date_from), "date_to": str(date_to),
        "business_type": business_type, "customer": customer,
        "sort": sort, "include_returns": include_returns,
    })
    data = sales_analytics.orders(
        db, date_from=date_from, date_to=date_to, business_types=business_type,
        customer=customer, salesperson=salesperson,
        sort=sort, page=page, page_size=page_size, include_returns=include_returns,
    )
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
    data = sales_analytics.order_detail(db, order_id, include_returns=include_returns)
    if data is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return apply_field_visibility(data, ctx)
