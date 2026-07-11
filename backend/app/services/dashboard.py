"""老板经营看板聚合（P1 第二阶段第一刀）。

只读分析。一切金额走**未税口径**（config.PROFIT_VAT_RATE=13%，见 profit._ex_tax_*），
与利润引擎同源；成本/毛利读 recompute 落库的 f_sales_line 计算字段。

核心防误导口径（甲方 2026-07-11）：
- 正式利润只统计**有成本的销售行**；看板同时给 **成本覆盖率** 与 **未配成本营收**，否则毛利虚高。
- 经营 KPI **默认排除未来日期**（如 2026-12-02 的销售单），并单独计入"未来日期"异常。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app import config, security
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.query_filters import active_orders

# 取消/作废口径：非"已生效"里明确终止的两种状态（其余如进行中/草稿单独计）
_CANCELLED_STATUS = ("已取消", "作废")


def _f(x) -> float | None:
    return float(x) if x is not None else None


def _purchase_ex_tax_expr():
    """采购行未税额表达式：unit_price*qty，按头表 is_tax_inclusive 归一（含税/未知÷1.13、
    明确不含税取原值）；TAX_BASIS!=ex_tax 时不换算。与 profit._ex_tax_purchase 同口径。"""
    gross = FPurchaseLine.unit_price * FPurchaseLine.qty
    if config.TAX_BASIS != "ex_tax":
        return gross
    return case(
        (FPurchaseOrder.is_tax_inclusive.is_(False), gross),
        else_=gross / (Decimal(1) + config.PROFIT_VAT_RATE),
    )


def kpi(db: Session, date_from: date | None, date_to: date | None,
        as_of: date | None = None, user_ctx: security.UserContext | None = None) -> dict:
    """顶部经营指标。金额未税。默认排除未来日期（> as_of）。

    返回：销售额/采购额/已配成本销售额/毛利额/毛利率/成本覆盖率/未配成本营收/被排除营收
    + 订单健康（已生效/进行中/取消/异常行/未来日期单）。
    """
    today = as_of or date.today()
    upper = min(date_to, today) if date_to else today   # 未来日期一律排除出 KPI

    sl = FSalesLine
    counts = sl.counts_revenue.is_(True)
    costed = and_(counts, sl.cost_moving_avg.is_not(None))

    # ---- 销售侧（已生效 + 计营收 + [from, min(to,today)]）----
    sales_stmt = (
        select(
            func.sum(sl.revenue_amount).filter(counts).label("sales_ex_tax"),
            func.sum(sl.revenue_amount).filter(costed).label("sales_costed"),
            func.sum(sl.gross_profit).filter(costed).label("gross_profit"),
            func.sum(sl.revenue_amount).filter(sl.counts_revenue.is_(False)).label("excluded_rev"),
            func.count().filter(and_(counts, func.cardinality(sl.anomaly_flags) > 0)).label("anomaly_lines"),
        )
        .join(FSalesOrder, sl.order_id == FSalesOrder.id)
    )
    sales_stmt = active_orders(sales_stmt, FSalesOrder)
    if date_from:
        sales_stmt = sales_stmt.where(FSalesOrder.order_date >= date_from)
    sales_stmt = sales_stmt.where(FSalesOrder.order_date <= upper)
    if user_ctx is not None:
        sales_stmt = security.apply_data_scope(sales_stmt, user_ctx)
    s = db.execute(sales_stmt).one()

    sales_ex = s.sales_ex_tax or Decimal(0)
    sales_costed = s.sales_costed or Decimal(0)
    gross = s.gross_profit or Decimal(0)
    margin = (gross / sales_costed) if sales_costed else None            # 毛利率分母=已配成本营收
    coverage = (sales_costed / sales_ex) if sales_ex else None           # 成本覆盖率
    uncosted = sales_ex - sales_costed                                   # 未配成本营收（利润未计）

    # ---- 采购侧（已生效 + [from, min(to,today)]，未税额）----
    pur_stmt = (
        select(func.sum(_purchase_ex_tax_expr()).label("purchase_ex_tax"))
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
    )
    pur_stmt = active_orders(pur_stmt, FPurchaseOrder)
    if date_from:
        pur_stmt = pur_stmt.where(FPurchaseOrder.order_date >= date_from)
    pur_stmt = pur_stmt.where(FPurchaseOrder.order_date <= upper)
    purchase_ex = db.execute(pur_stmt).scalar() or Decimal(0)

    # ---- 订单健康（全状态，销售+采购，[from, to] 不裁未来——反而要数出未来单）----
    counts_by_status = _order_health(db, date_from, date_to, today)

    return {
        "window": {"date_from": date_from.isoformat() if date_from else None,
                   "date_to": date_to.isoformat() if date_to else None,
                   "as_of": today.isoformat(), "future_excluded": True},
        # 金额（未税）
        "sales_ex_tax": _f(sales_ex),
        "purchase_ex_tax": _f(purchase_ex),
        "sales_costed_ex_tax": _f(sales_costed),
        "gross_profit": _f(gross),
        "gross_margin": round(float(margin), 4) if margin is not None else None,
        "cost_coverage": round(float(coverage), 4) if coverage is not None else None,
        "sales_uncosted_ex_tax": _f(uncosted),
        "excluded_revenue": _f(s.excluded_rev),
        # 订单健康
        **counts_by_status,
        "anomaly_lines": s.anomaly_lines or 0,
    }


def _order_health(db: Session, date_from: date | None, date_to: date | None, today: date) -> dict:
    """销售+采购订单按状态计数 + 未来日期单数（数据异常）。跨两个头表求和。"""
    out = {"orders_active": 0, "orders_in_progress": 0, "orders_cancelled": 0, "orders_future": 0}
    for OM in (FSalesOrder, FPurchaseOrder):
        stmt = select(
            func.count().filter(OM.data_status == config.ACTIVE_STATUS).label("active"),
            func.count().filter(OM.data_status == "进行中").label("in_progress"),
            func.count().filter(OM.data_status.in_(_CANCELLED_STATUS)).label("cancelled"),
            func.count().filter(OM.order_date > today).label("future"),
        )
        if date_from:
            stmt = stmt.where(OM.order_date >= date_from)
        if date_to:
            stmt = stmt.where(OM.order_date <= date_to)
        r = db.execute(stmt).one()
        out["orders_active"] += r.active or 0
        out["orders_in_progress"] += r.in_progress or 0
        out["orders_cancelled"] += r.cancelled or 0
        out["orders_future"] += r.future or 0
    return out
