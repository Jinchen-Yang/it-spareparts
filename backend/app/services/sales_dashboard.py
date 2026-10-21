"""销售看板聚合（D-44 PR-2）：分组排行、时间趋势、对比上月、新客 / 老客。

只读。全部建立在 ``sales_analytics._order_query`` 同一基查询之上（单头口径，一单一行），
在其子查询外再 GROUP BY——保证 分组合计（含“其他”）= 趋势各桶合计 = KPI 合计 =
订单列表合计（D-44 验收第 1 条），本模块不另起金额口径。

口径要点：
- 分组维度取原值，空值统一标「未填」作为一组参与排行，不丢弃（D-44 方案 §4.3）；
  客户按规范名合并（D-44 §12）。
- 占比 / 单均 / 集中度按 ``amount_basis`` 计算；API 层在该口径被遮蔽时强制退回公开
  口径 revenue，因此这些派生键不必另行登记脱敏。
- 对比期 = 所选区间两端各前移一个自然月，不存在的日子取当月最后一天；区间长于一个月
  或缺任一端日期时不提供（D-44 §9）。
- 新客 = 首单日期落在所选区间内的客户；首单按规范名取全历史已生效单最早日期，
  不受其他筛选影响；未填客户不参与（D-44 §13）。
- 趋势桶用 generate_series 生成完整序列，无成交的桶单数与金额为 0（确实没成交），
  有成交但该金额全缺失的桶为 null（D-44 方案 §3.4）。
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Date, cast, func, literal, select
from sqlalchemy.orm import Session

from app import tax_policy
from app.models.dimensions import DimCustomer
from app.models.sales import FSalesOrder
from app.services.query_filters import active_orders
from app.services.sales_analytics import AMOUNT_KEYS, _f, _order_query
from app.services.sales_analytics import summary as _summary

EMPTY_LABEL = "未填"
OTHER_KEY = "__other__"

# 分组维度 → 基查询子查询列名
GROUP_DIMS = {
    "customer": "customer",
    "customer_type": "customer_type",
    "customer_city": "customer_city",
    "customer_source": "customer_source",
    "salesperson": "salesperson",
    "business_type": "business_type",
    "warehouse": "warehouse",
}
CUSTOMER_DIMS = ("customer", "customer_type", "customer_city", "customer_source")
GROUP_SORTS = AMOUNT_KEYS + ("orders", "gross_profit", "customers", "key")
GRANULARITIES = ("day", "week", "month")
MAX_TREND_BUCKETS = 1000


class DashboardValidationError(ValueError):
    """参数组合非法（API 层转 422）。"""


def shift_month(d: date, months: int) -> date:
    """自然月平移；目标月没有该日时取当月最后一天（3-31 前移 → 2-28/29）。"""
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def compare_window(date_from: date | None, date_to: date | None) -> tuple[date, date] | None:
    """对比期（D-44 §9）：两端前移一个自然月；区间长于一个月（与对比期重叠）或缺端点 → None。"""
    if date_from is None or date_to is None:
        return None
    if shift_month(date_from, 1) <= date_to:
        return None
    return shift_month(date_from, -1), shift_month(date_to, -1)


def _measures(sub):
    return [
        func.count().label("orders"),
        func.sum(sub.c.amount_ex_tax).label("amount_ex_tax"),
        func.sum(sub.c.amount_inc_tax).label("amount_inc_tax"),
        func.sum(sub.c.revenue).label("revenue"),
        func.sum(sub.c.revenue_costed).label("revenue_costed"),
        func.sum(sub.c.cost_costed).label("cost_costed"),
        func.count(func.distinct(sub.c.customer)).label("customers"),
    ]


def _gross(rc, cc) -> Decimal | None:
    if rc is None or cc is None:
        return None
    return tax_policy.round_money(rc) - tax_policy.round_money(cc)


def _ratio(num, den) -> float | None:
    if num is None or not den:
        return None
    return round(float(Decimal(num) / Decimal(den)), 4)


def _row_payload(r, basis: str) -> dict:
    gross = _gross(r.revenue_costed, r.cost_costed)
    basis_total = getattr(r, basis)
    return {
        "orders": r.orders,
        "customers": r.customers,
        "amount_ex_tax": _f(r.amount_ex_tax),
        "amount_inc_tax": _f(r.amount_inc_tax),
        "revenue": _f(r.revenue),
        "gross_profit": _f(gross),
        "gross_margin": _ratio(gross, r.revenue_costed),
        "avg_order": _f(Decimal(basis_total) / r.orders) if basis_total is not None and r.orders else None,
        "_basis": basis_total,
    }


def _shift_filters(filters: dict, window: tuple[date, date]) -> dict:
    return {**filters, "date_from": window[0], "date_to": window[1]}


def groups(
    db: Session,
    *,
    group_by: str,
    sort: str = "amount_ex_tax",
    order: str = "desc",
    top: int = 20,
    compare: bool = False,
    **filters,
) -> dict:
    """分组排行（D-44 方案 §3.3）：任一指标升降序，Top N + “其他”，占比 / 累计占比 / 集中度。

    ``filters`` 即 ``_order_query`` 全部筛选（含 amount_basis，占比按它计算）。
    排序 NULLS LAST，并列按分组键升序破并列；“其他”恒排最后且合计 = 总计 − TopN。
    ``top=0`` 表示全部。``compare=True`` 且对比期可用时，每组附上月同期同组的指标。
    """
    if group_by not in GROUP_DIMS:
        raise DashboardValidationError(f"不支持的分组维度：{group_by}")
    if sort not in GROUP_SORTS:
        raise DashboardValidationError(f"不支持的排序键：{sort}")
    basis = filters.get("amount_basis") or "amount_ex_tax"
    base, _ = _order_query(**filters)
    sub = base.subquery()
    dim = sub.c[GROUP_DIMS[group_by]]
    key = func.coalesce(dim, literal(EMPTY_LABEL)).label("key")
    rows = db.execute(select(key, *_measures(sub)).group_by(key)).all()
    total_basis = sum((getattr(r, basis) or 0) for r in rows) if rows else None

    items = [{"key": r.key, "is_empty": r.key == EMPTY_LABEL, **_row_payload(r, basis)}
             for r in rows]

    def sort_value(item):
        if sort == "key":
            return item["key"]
        return item[sort]

    present = [i for i in items if sort_value(i) is not None]
    missing = [i for i in items if sort_value(i) is None]
    present.sort(key=lambda i: i["key"])                    # 并列按键升序（稳定排序）
    present.sort(key=sort_value, reverse=(order == "desc"))
    missing.sort(key=lambda i: i["key"])
    ordered = present + missing

    head = ordered if top == 0 else ordered[:top]
    rest = [] if top == 0 else ordered[top:]
    cumulative = Decimal(0)
    for i in head:
        b = i["_basis"]
        i["share"] = _ratio(b, total_basis)
        if b is not None:
            cumulative += Decimal(b)
        i["cumulative_share"] = _ratio(cumulative, total_basis)

    other = None
    if rest:
        rc = [r for r in rows if r.key in {i["key"] for i in rest}]
        orders_n = sum(r.orders for r in rc)

        def _sum(attr):
            vals = [getattr(r, attr) for r in rc if getattr(r, attr) is not None]
            return sum(vals) if vals else None

        gross = _gross(_sum("revenue_costed"), _sum("cost_costed"))
        basis_sum = _sum(basis)
        other = {
            "key": OTHER_KEY,
            "groups": len(rest),
            "orders": orders_n,
            "amount_ex_tax": _f(_sum("amount_ex_tax")),
            "amount_inc_tax": _f(_sum("amount_inc_tax")),
            "revenue": _f(_sum("revenue")),
            "gross_profit": _f(gross),
            "gross_margin": _ratio(gross, _sum("revenue_costed")),
            "share": _ratio(basis_sum, total_basis),
            "avg_order": _f(Decimal(basis_sum) / orders_n) if basis_sum is not None and orders_n else None,
        }

    # 集中度：按口径金额从大到小的头部占比（与当前排序无关），未填组参与
    by_amount = sorted((Decimal(i["_basis"]) for i in items if i["_basis"] is not None),
                       reverse=True)
    concentration = {
        f"top{n}_share": _ratio(sum(by_amount[:n]), total_basis) if by_amount else None
        for n in (1, 5, 10)
    }

    window = compare_window(filters.get("date_from"), filters.get("date_to"))
    compare_payload = None
    if compare and window is not None:
        cbase, _ = _order_query(**_shift_filters(filters, window))
        csub = cbase.subquery()
        ckey = func.coalesce(csub.c[GROUP_DIMS[group_by]], literal(EMPTY_LABEL)).label("key")
        crows = {r.key: _row_payload(r, basis)
                 for r in db.execute(select(ckey, *_measures(csub)).group_by(ckey)).all()}
        for i in head:
            c = crows.get(i["key"])
            if c is not None:
                c.pop("_basis")
            i["compare"] = c
        compare_payload = {"date_from": window[0].isoformat(), "date_to": window[1].isoformat()}

    for i in items:
        i.pop("_basis", None)
    return {
        "group_by": group_by,
        "sort": sort,
        "order": order,
        "amount_basis": basis,
        "top": top,
        "total_groups": len(items),
        "groups": head,
        "other": other,
        "concentration": concentration,
        "compare_window": compare_payload,
        "compare_available": window is not None,
    }


def _bucket_start(d: date, granularity: str) -> date:
    if granularity == "day":
        return d
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def _bucket_series(start: date, end: date, granularity: str) -> list[date]:
    out, cur = [], _bucket_start(start, granularity)
    while cur <= end:
        out.append(cur)
        if len(out) > MAX_TREND_BUCKETS:
            raise DashboardValidationError(
                f"趋势桶数超过 {MAX_TREND_BUCKETS}，请收窄区间或改用更粗的粒度")
        if granularity == "day":
            cur += timedelta(days=1)
        elif granularity == "week":
            cur += timedelta(days=7)
        else:
            cur = shift_month(cur, 1)
    return out


def _trend_rows(db: Session, filters: dict, granularity: str) -> dict[date, dict]:
    base, _ = _order_query(**filters)
    sub = base.subquery()
    trunc = {"day": "day", "week": "week", "month": "month"}[granularity]
    bucket = cast(func.date_trunc(trunc, sub.c.order_date), Date).label("bucket")
    stmt = (select(bucket, *_measures(sub))
            .where(sub.c.order_date.is_not(None)).group_by(bucket))
    return {r.bucket: _row_payload(r, filters.get("amount_basis") or "amount_ex_tax")
            for r in db.execute(stmt).all()}


def _filled(rows: dict[date, dict], series: list[date]) -> list[dict]:
    out = []
    for b in series:
        r = rows.get(b)
        if r is None:
            r = {"orders": 0, "customers": 0, "amount_ex_tax": 0.0, "amount_inc_tax": 0.0,
                 "revenue": 0.0, "gross_profit": 0.0, "gross_margin": None, "avg_order": None}
        else:
            r = {k: v for k, v in r.items() if k != "_basis"}
        out.append({"bucket": b.isoformat(), **r})
    return out


def trend(
    db: Session,
    *,
    granularity: str = "day",
    compare: bool = False,
    **filters,
) -> dict:
    """时间趋势（D-44 方案 §3.4）：完整桶序列 + 可选上月对比序列 + 星期分布。

    区间缺端点（“全部”等）时取筛选结果里的最早 / 最晚订单日。周以周一为起点。
    对比序列按位置对齐（第 1 桶对第 1 桶），桶数与主序列可能差一（月长不同），
    前端按位置配对，多出的桶不连线。
    """
    if granularity not in GRANULARITIES:
        raise DashboardValidationError(f"不支持的粒度：{granularity}")
    date_from, date_to = filters.get("date_from"), filters.get("date_to")
    if date_from is None or date_to is None:
        base, _ = _order_query(**filters)
        sub = base.subquery()
        lo, hi = db.execute(select(func.min(sub.c.order_date),
                                   func.max(sub.c.order_date))).one()
        date_from = date_from or lo
        date_to = date_to or hi
    if date_from is None or date_to is None:
        return {"granularity": granularity, "buckets": [], "compare": None,
                "compare_window": None, "compare_available": False, "weekday": []}
    series = _bucket_series(date_from, date_to, granularity)
    buckets = _filled(_trend_rows(db, filters, granularity), series)

    window = compare_window(filters.get("date_from"), filters.get("date_to"))
    cmp_buckets = None
    if compare and window is not None:
        cfilters = _shift_filters(filters, window)
        cmp_buckets = _filled(_trend_rows(db, cfilters, granularity),
                              _bucket_series(window[0], window[1], granularity))

    # 星期分布（ISO：1=周一 … 7=周日），只统计有日期的单
    base, _ = _order_query(**filters)
    sub = base.subquery()
    dow = func.extract("isodow", sub.c.order_date).label("dow")
    wd = {int(r.dow): _row_payload(r, filters.get("amount_basis") or "amount_ex_tax")
          for r in db.execute(select(dow, *_measures(sub))
                              .where(sub.c.order_date.is_not(None)).group_by(dow)).all()}
    weekday = []
    for n in range(1, 8):
        r = wd.get(n)
        weekday.append({"isodow": n, "orders": r["orders"] if r else 0,
                        "amount_ex_tax": r["amount_ex_tax"] if r else 0.0,
                        "amount_inc_tax": r["amount_inc_tax"] if r else 0.0,
                        "revenue": r["revenue"] if r else 0.0})
    return {
        "granularity": granularity,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "buckets": buckets,
        "compare": cmp_buckets,
        "compare_window": (
            {"date_from": window[0].isoformat(), "date_to": window[1].isoformat()}
            if cmp_buckets is not None else None),
        "compare_available": window is not None,
        "weekday": weekday,
    }


def customer_lifecycle(db: Session, **filters) -> dict:
    """新客 / 老客（D-44 §13）：在当前筛选结果的客户中，首单落在区间内为新客。

    首单 = 该规范名全历史已生效单最早 order_date，不受业务类型等筛选影响；未填客户
    （规范名为空）不参与。区间缺下端点（“全部”）时全部客户都是新客。
    """
    so = FSalesOrder
    first = active_orders(
        select(DimCustomer.name_normalized.label("name"),
               func.min(so.order_date).label("first_date"))
        .select_from(so).join(DimCustomer, so.customer_id == DimCustomer.id)
        .where(DimCustomer.name_normalized.is_not(None)),
        so,
    ).group_by(DimCustomer.name_normalized).subquery()
    base, _ = _order_query(**filters)
    sub = base.subquery()
    date_from = filters.get("date_from")
    names = select(func.distinct(sub.c.customer).label("name")).where(
        sub.c.customer.is_not(None)).subquery()
    is_new = (first.c.first_date >= date_from) if date_from is not None else literal(True)
    row = db.execute(
        select(func.count().filter(is_new).label("new"),
               func.count().filter(~is_new).label("returning"))
        .select_from(names).join(first, first.c.name == names.c.name)
    ).one()
    return {"new_customers": row.new or 0, "returning_customers": row.returning or 0}


def summary_compare(db: Session, **filters) -> dict | None:
    """上月对比 KPI：对比期可用时返回 {window, kpi}，否则 None（D-44 §9）。"""
    window = compare_window(filters.get("date_from"), filters.get("date_to"))
    if window is None:
        return None
    data = _summary(db, **_shift_filters(filters, window))
    return {"date_from": window[0].isoformat(), "date_to": window[1].isoformat(),
            "kpi": data["kpi"]}
