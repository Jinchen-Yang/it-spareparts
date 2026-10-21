"""销售看板聚合（D-44 PR-2，2026-10-22 轮复审修订）：分组排行、时间趋势、对比上月、
新客 / 老客、金额分布、订单导出取数。

只读。全部建立在 ``sales_analytics._order_query`` 同一基查询之上（单头口径，一单一行），
在其子查询外再 GROUP BY——保证 分组合计（含「其他」）= 趋势各桶合计 = KPI 合计 =
订单列表合计（D-44 验收第 1 条），本模块不另起金额口径。行级分组（大类 / 品牌 / PN）
对**同一批叶子舍入值**求和（DL-018 修订），与订单列表行级口径逐分守恒。

口径要点（本轮修订处标 ⌘）：
- ⌘ 分组维度取归一值（``sales_analytics._norm_dim``：NULL / 空串 / 全空白 → NULL），
  缺失组键为 ``None`` + ``is_empty=True``，**不与真实名叫「未填」/「未分类」的对象
  碰撞**（A-AC06 / DL-020）；前端按维度给缺失组显示标签（未填 / 未分类）。
- 排序：任一指标升降序，NULL 值（含缺失组键）**升降都沉底**（A-AC08）；并列按分组
  键升序破并列；「其他」桶带 ``is_other=True``。
- 占比 / 单均 / 集中度按 ``amount_basis`` 计算；API 层在该口径被遮蔽时强制退回公开
  口径 revenue。⌘ 分母 ≤ 0（含负）不可比较 → None（Q-A-07）。
- 对比期 = 所选区间两端各前移一个自然月，不存在的日子取当月最后一天；区间长于一个月
  或缺任一端日期时不提供（D-44 §9）。⌘ 日期极值（如 9999-12）前移溢出按「对比期
  不可得」处理，不 500（A-AC10）；趋势序列触到日历尽头正常收尾，桶数超上限 422。
- ⌘ 新客 / 老客（Q-A-10）：首单 = 该规范名全历史已生效单最早 order_date。区间缺
  下端点（「全部」）时不拆新老客（``available=False``，基线缺失）；首单日期未知
  （该客户全部订单都无日期）计 ``undetermined``，不当新客。
- 趋势桶用序列生成完整序列，无成交的桶单数与金额为 0（确实没成交），有成交但该
  金额全缺失的桶为 null（D-44 方案 §3.4）。⌘ 无日期订单不静默消失：统一计入
  ``no_date_orders`` + ``no_date_amount_*``（不入任何时间桶——没有日期不可定位；
  A-AC05 / DL-019）；「全部」口径追加显式「无日期」桶（金额同口径），桶金额合计
  与 KPI 守恒，不只靠说明文字。
- ⌘ 金额分布（A-AC09）：段宽 1k 整数倍、段数 ≤ bins；0 / 8000 / 8001 / 8501 /
  纯负 / 空集全覆盖，``sum(bins) + negative_orders + empty_orders == total`` 逐单
  守恒；右端闭区间（max 恰落段界时归该段，不丢单）。
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Date, and_, cast, func, select
from sqlalchemy.orm import Session

from app import security, tax_policy
from app.models.dimensions import DimCustomer
from app.models.sales import FSalesLine, FSalesOrder
from app.services.query_filters import active_orders
from app.services.sales_analytics import (
    AMOUNT_KEYS,
    HEADER_BASIS_COLS,
    _category_match,
    _clean_multi,
    _f,
    _leaf_line_amount,
    _norm_dim,
    _order_query,
)
from app.services.sales_analytics import summary as _summary

EMPTY_LABEL = "未填"
OTHER_KEY = "__other__"

# 分组维度 → 基查询子查询列名（归一后 NULL = 缺失组）
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
# 行级分组维度（D-44 §8）：订单拆成明细行，一张单可计入多个组
LINE_DIMS = ("category", "brand", "pn")
ALL_GROUP_DIMS = tuple(GROUP_DIMS) + LINE_DIMS
# 可见数据列全量排序键（A-AC07 / DL-024，本轮返工补齐）：三金额 / 单数 / 毛利 /
# 客户数 / 组键之外，行级维度另有明细行 lines 与单均 avg_order；较上月同期为
# 真实同期变化率 basis_change（不与金额排序等价，反馈§3 数学反例）。
GROUP_SORTS = AMOUNT_KEYS + ("orders", "gross_profit", "customers", "key",
                             "lines", "avg_order", "basis_change")
GRANULARITIES = ("day", "week", "month")
MAX_TREND_BUCKETS = 1000


class DashboardValidationError(ValueError):
    """参数组合非法（API 层转 422）。"""


def shift_month(d: date, months: int) -> date:
    """自然月平移；目标月没有该日时取当月最后一天（3-31 前移 → 2-28/29）。

    越出 PG date 范围（年 1-9999，如 9999-12 后移）抛 ``DashboardValidationError``
    ——调用方按「不可得 / 序列到头」处理，绝不 500（A-AC10）。
    """
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    try:
        return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
    except (ValueError, OverflowError):
        raise DashboardValidationError(f"日期平移越界：{d} ± {months} 月")


def compare_window(date_from: date | None, date_to: date | None) -> tuple[date, date] | None:
    """对比期（D-44 §9）：两端前移一个自然月；区间长于一个月（与对比期重叠）或缺端点 → None。

    极值日期（9999-12 起 / 0001-01 止）前移 / 后移越界时按「对比期不可得」返回
    None——这类区间的上一月本就不存在或长度必不超一个月，不构成 500（A-AC10）。
    """
    if date_from is None or date_to is None or date_from > date_to:
        return None
    try:
        nxt = shift_month(date_from, 1)
    except DashboardValidationError:
        nxt = None  # 起点在最后一月：next 必然晚于任何合法 date_to
    if nxt is not None and nxt <= date_to:
        return None
    try:
        return shift_month(date_from, -1), shift_month(date_to, -1)
    except DashboardValidationError:
        return None  # 起点在第一月：上一月不存在


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
    """分母缺失或 ≤ 0（含负）不可比较 → None（Q-A-07 负分母不可比）。"""
    if num is None or den is None or den <= 0:
        return None
    return round(float(Decimal(num) / Decimal(den)), 4)


def _row_payload(r, basis: str) -> dict:
    gross = _gross(r.revenue_costed, r.cost_costed)
    basis_total = getattr(r, basis)
    extra = {"lines": r.lines} if hasattr(r, "lines") else {}
    return {
        **extra,
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


def _group_rows(db: Session, filters: dict, group_by: str):
    """按维度聚合的行：单头维度在订单子查询上分组；行级维度拆明细行分组。

    分组键一律归一（``_norm_dim``），缺失组键为 NULL + ``is_empty``（A-AC06），
    不用「未填」字面值与真实对象碰撞。行级：订单范围取 ``_order_query`` 同一筛选
    结果（含金额区间），再连明细行；有大类 / 品牌筛选时只计匹配行。金额同
    ``_order_query`` 行级口径的**叶子舍入**：每行先各自舍到分（含税 = 行金额，
    空时 数量 × 单价；不含税 = 含税 ÷ 1.13 名义）再求和（DL-018 / A-AC04）；
    单数 = 去重订单数，另给明细行数。
    """
    base, _ = _order_query(**filters)
    sub = base.subquery()
    if group_by not in LINE_DIMS:
        key = sub.c[GROUP_DIMS[group_by]].label("key")
        return db.execute(select(key, *_measures(sub)).group_by(key)).all()

    sl = FSalesLine
    # GROUP BY 用原始列（SELECT 的归一表达式是分组列上的表达式，PG 合法；避免
    # SELECT/GROUP BY 各自渲染 _norm_dim 产生不同绑定参数）。
    if group_by == "category":
        dim, group_cols = _norm_dim(sl.category_major), [sl.category_major]
    elif group_by == "brand":
        dim, group_cols = _norm_dim(sl.brand), [sl.brand]
    else:
        dim = _norm_dim(func.coalesce(sl.pn_std, sl.pn_raw))
        group_cols = [sl.pn_std, sl.pn_raw]
    key = dim.label("key")
    counts = sl.counts_revenue.is_(True)
    costed = and_(counts, sl.cost_moving_avg.is_not(None))
    leaf_inc = _leaf_line_amount(sl)
    stmt = select(
        key,
        func.count(func.distinct(sl.order_id)).label("orders"),
        func.count(sl.id).label("lines"),
        func.sum(func.round(leaf_inc / tax_policy.TAX_FACTOR, 2)).label("amount_ex_tax"),
        func.sum(leaf_inc).label("amount_inc_tax"),
        func.sum(sl.revenue_amount).filter(counts).label("revenue"),
        func.sum(sl.revenue_amount).filter(costed).label("revenue_costed"),
        func.sum(sl.cost_amount).filter(costed).label("cost_costed"),
        func.count(func.distinct(sub.c.customer)).label("customers"),
    ).select_from(sl).join(sub, sl.order_id == sub.c.id)
    cats = _clean_multi(filters.get("categories"))
    brs = _clean_multi(filters.get("brands"))
    if cats:
        stmt = stmt.where(_category_match(sl, cats))
    if brs:
        stmt = stmt.where(_norm_dim(sl.brand).in_(brs))
    return db.execute(stmt.group_by(*group_cols)).all()


def groups(
    db: Session,
    *,
    user_ctx=None,
    group_by: str,
    sort: str = "amount_ex_tax",
    order: str = "desc",
    top: int = 20,
    compare: bool = False,
    **filters,
) -> dict:
    """分组排行（D-44 方案 §3.3）：任一指标升降序，Top N + “其他”，占比 / 累计占比 / 集中度。

    ``filters`` 即 ``_order_query`` 全部筛选（含 amount_basis，占比按它计算）。
    排序值 NULL（含缺失组键）升降都沉底（A-AC08），并列按分组键升序破并列；
    「其他」恒排最后且合计 = 总计 − TopN，带 ``is_other=True``。``top=0`` 表示全部。
    ``compare=True`` 且对比期可用时，每组附上月同期同组的指标。
    ``sort="basis_change"``（较上月同期变化率）按真实同期变化排序，与当期金额
    排序不等价（反馈§3：当期 1000→上期 900 约 +11%，当期 500→上期 100 +400%，
    金额降序前者先、变化降序后者先）；与前端 changeRatio 同式 (cur−prev)/prev，
    基期未知 / ≤0 或本期未知 → 不可比较（Q-A-07），两向空值恒末尾。未开
    ``compare`` 或对比期不可用时该列不可见，排序退化为组键序（全部不可比较）。
    ``user_ctx``（可选）：带上下文直调时与 API 层同源生效排序——被遮蔽的金额 /
    毛利 / 客户数排序退回公开单数，被遮蔽口径退回 revenue（反馈§4A，服务层
    双保险）；不带 ctx 的内部取数路径行为不变，遮蔽交给调用方。
    """
    if group_by not in ALL_GROUP_DIMS:
        raise DashboardValidationError(f"不支持的分组维度：{group_by}")
    if sort not in GROUP_SORTS:
        raise DashboardValidationError(f"不支持的排序键：{sort}")
    if sort == "lines" and group_by not in LINE_DIMS:
        # 「明细行」列只在行级分组（大类 / 品牌 / PN）可见，单头维度没有该列可排
        raise DashboardValidationError(f"明细行排序仅适用于行级分组维度：{group_by}")
    basis = filters.get("amount_basis") or "amount_ex_tax"
    # 服务层双保险（反馈§4A「服务/API 同源有效排序语义」，orders() 同款先例）：
    # 直调带 user_ctx 也不按被遮键排序——金额 / 毛利 / 客户数排序退回公开单数
    # （客户数值遮蔽不拦行序，行序仍按真实客户数就泄漏相对规模）；口径被遮退回
    # 公开 revenue，avg_order / basis_change 恒按响应口径计算（排序值 = 展示值）。
    if user_ctx is not None:
        if ((sort in AMOUNT_KEYS or sort == "gross_profit")
                and security.is_field_hidden(user_ctx, sort)):
            sort = "orders"
        if sort == "customers" and security.is_field_hidden(user_ctx, "customer"):
            sort = "orders"
        if security.is_field_hidden(user_ctx, basis):
            filters["amount_basis"] = basis = "revenue"
    rows = _group_rows(db, filters, group_by)
    total_basis = sum((getattr(r, basis) or 0) for r in rows) if rows else None

    items = [{"key": r.key, "is_empty": r.key is None, **_row_payload(r, basis)}
             for r in rows]

    # 对比期行只取一次：basis_change 排序要给**全部**组算变化率（排序先于 TopN
    # 截断），后面的 compare 附值复用同一批行，不再多查一次。
    window = compare_window(filters.get("date_from"), filters.get("date_to"))
    crows = {}
    if compare and window is not None:
        crows = {r.key: r for r in _group_rows(db, _shift_filters(filters, window), group_by)}
    if sort == "basis_change":
        for i in items:
            prev = crows.get(i["key"])
            cur_b, prev_b = i["_basis"], (getattr(prev, basis, None) if prev else None)
            i["_change"] = (_ratio(cur_b - prev_b, prev_b)
                            if cur_b is not None and prev_b is not None else None)

    def sort_value(item):
        if sort == "key":
            return item["key"]
        if sort == "basis_change":
            return item["_change"]
        return item[sort]

    present = [i for i in items if sort_value(i) is not None]
    missing = [i for i in items if sort_value(i) is None]
    present.sort(key=lambda i: i["key"] or "")       # 并列按键升序（稳定排序；None 已在 missing）
    present.sort(key=sort_value, reverse=(order == "desc"))
    missing.sort(key=lambda i: i["key"] or "")
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
        rest_keys = {i["key"] for i in rest}
        rc = [r for r in rows if r.key in rest_keys]
        orders_n = sum(r.orders for r in rc)

        def _sum(attr):
            vals = [getattr(r, attr) for r in rc if getattr(r, attr) is not None]
            return sum(vals) if vals else None

        gross = _gross(_sum("revenue_costed"), _sum("cost_costed"))
        basis_sum = _sum(basis)
        other = {
            "key": OTHER_KEY,
            "is_other": True,
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

    # 集中度：按口径金额从大到小的头部占比（与当前排序无关），缺失组参与
    by_amount = sorted((Decimal(i["_basis"]) for i in items if i["_basis"] is not None),
                       reverse=True)
    concentration = {
        f"top{n}_share": _ratio(sum(by_amount[:n]), total_basis) if by_amount else None
        for n in (1, 5, 10)
    }

    compare_payload = None
    if compare and window is not None:
        cpayload = {k: _row_payload(r, basis) for k, r in crows.items()}
        for i in head:
            c = cpayload.get(i["key"])
            if c is not None:
                c.pop("_basis")
            i["compare"] = c
        compare_payload = {"date_from": window[0].isoformat(), "date_to": window[1].isoformat()}

    for i in items:
        i.pop("_basis", None)
        i.pop("_change", None)
    return {
        "group_by": group_by,
        # 行级分组：各组单数之和可大于总单数（一单多组），前端表头注明
        "line_level": group_by in LINE_DIMS,
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
    """完整桶序列（generate_series 语义）。桶数超上限 → 422（DashboardValidationError）。

    步进越过日历尽头（end = 9999-12-31 等）时在已含 end 的最后一个桶后正常收尾
    ——end 所在桶必然已入列（步进溢出只发生在 cur ≥ end 之后），不丢数据（A-AC10）。
    """
    out, cur = [], _bucket_start(start, granularity)
    while cur <= end:
        out.append(cur)
        if len(out) > MAX_TREND_BUCKETS:
            raise DashboardValidationError(
                f"趋势桶数超过 {MAX_TREND_BUCKETS}，请收窄区间或改用更粗的粒度")
        try:
            if granularity == "day":
                cur = cur + timedelta(days=1)
            elif granularity == "week":
                cur = cur + timedelta(days=7)
            else:
                cur = shift_month(cur, 1)
        except (DashboardValidationError, OverflowError):
            break
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

    无日期订单（A-AC05 / DL-019，修订）：日期过滤天然排除无日期单，计数因此在
    **去掉日期条件的同一筛选**上做——「全部」口径（两端都未给）时追加一个显式
    「无日期」桶（``bucket=null, no_date=true``，**单数与三金额同口径**），使
    桶合计（含无日期桶）= KPI 合计的订单数**与金额**都逐分守恒；有日期区间时
    无日期单不可判定归属，以 ``no_date_orders`` 计数 + ``no_date_amount_*``
    金额合计提示（不计入任何日期桶）。无任何有日期订单时日期桶为空，同形键返回。
    """
    if granularity not in GRANULARITIES:
        raise DashboardValidationError(f"不支持的粒度：{granularity}")
    date_from, date_to = filters.get("date_from"), filters.get("date_to")
    unbounded = date_from is None and date_to is None

    # 无日期单：在去掉日期条件的同一筛选上统计（日期过滤会排除它们）。
    nd_filters = {k: v for k, v in filters.items() if k not in ("date_from", "date_to")}
    nd_base, _ = _order_query(**nd_filters)
    nd_sub = nd_base.subquery()
    # 一次查询同时拿计数与聚合（无日期桶的金额同口径）
    nd_row = db.execute(select(
        func.count().label("orders"),
        func.sum(nd_sub.c.amount_ex_tax).label("amount_ex_tax"),
        func.sum(nd_sub.c.amount_inc_tax).label("amount_inc_tax"),
        func.sum(nd_sub.c.revenue).label("revenue"),
        func.sum(nd_sub.c.revenue_costed).label("revenue_costed"),
        func.sum(nd_sub.c.cost_costed).label("cost_costed"),
        func.count(func.distinct(nd_sub.c.customer)).label("customers"),
    ).select_from(nd_sub).where(nd_sub.c.order_date.is_(None))).one()
    nd = nd_row.orders or 0
    no_date_bucket = None
    if unbounded and nd:
        basis = filters.get("amount_basis") or "amount_ex_tax"
        no_date_bucket = {"bucket": None, "no_date": True,
                          **{k: v for k, v in _row_payload(nd_row, basis).items()
                             if k != "_basis"}}

    base, _ = _order_query(**filters)
    sub = base.subquery()
    lo, hi = db.execute(select(func.min(sub.c.order_date).label("lo"),
                               func.max(sub.c.order_date).label("hi"))
                        .select_from(sub)).one()
    if date_from is None or date_to is None:
        date_from = date_from or lo
        date_to = date_to or hi

    def _shape(buckets, cmp_buckets, cmp_window, cmp_available, weekday):
        return {
            "granularity": granularity,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "buckets": buckets,
            "compare": cmp_buckets,
            "compare_window": cmp_window,
            "compare_available": cmp_available,
            "weekday": weekday,
            "no_date_orders": nd,
            "has_no_date": bool(nd),
            # 无日期单金额合计（同口径）：区间口径下提示「另有多少金额未计入趋势」；
            # 「全部」口径下无日期桶已带同款金额，两处取值一致
            "no_date_amount_ex_tax": _f(nd_row.amount_ex_tax),
            "no_date_amount_inc_tax": _f(nd_row.amount_inc_tax),
            "no_date_revenue": _f(nd_row.revenue),
        }

    buckets: list[dict] = []
    if date_from is not None and date_to is not None:
        series = _bucket_series(date_from, date_to, granularity)
        buckets = _filled(_trend_rows(db, filters, granularity), series)
    if no_date_bucket is not None:
        buckets = buckets + [no_date_bucket]
    if date_from is None or date_to is None:
        return _shape(buckets, None, None, False, [])

    window = compare_window(filters.get("date_from"), filters.get("date_to"))
    cmp_buckets = None
    if compare and window is not None:
        cfilters = _shift_filters(filters, window)
        cmp_buckets = _filled(_trend_rows(db, cfilters, granularity),
                              _bucket_series(window[0], window[1], granularity))

    # 星期分布（ISO：1=周一 … 7=周日），只统计有日期的单（无日期单在 no_date_orders）
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
    return _shape(
        buckets, cmp_buckets,
        ({"date_from": window[0].isoformat(), "date_to": window[1].isoformat()}
         if cmp_buckets is not None else None),
        window is not None, weekday,
    )


def customer_lifecycle(db: Session, **filters) -> dict:
    """新客 / 老客（D-44 §13 / Q-A-10）：在当前筛选结果的客户中，首单落在区间内为新客。

    首单 = 该规范名全历史已生效单最早 order_date，不受业务类型等筛选影响；未填客户
    （规范名缺失）不参与。区间缺下端点（“全部”）时**不拆新老客**（``available=False``，
    各计数 null——没有基线，拆分无意义，前端隐藏拆分行）；首单日期未知（该客户全部
    订单都无日期）计 ``undetermined``，不当新客。
    """
    so = FSalesOrder
    base, _ = _order_query(**filters)
    sub = base.subquery()
    names = select(func.distinct(sub.c.customer).label("name")).where(
        sub.c.customer.is_not(None)).subquery()
    if filters.get("date_from") is None:
        total = db.execute(select(func.count()).select_from(names)).scalar() or 0
        return {"available": False, "customers": total, "new_customers": None,
                "returning_customers": None, "undetermined": None}
    norm = _norm_dim(DimCustomer.name_normalized)
    first = active_orders(
        select(norm.label("name"), func.min(so.order_date).label("first_date"))
        .select_from(so).join(DimCustomer, so.customer_id == DimCustomer.id)
        .where(norm.is_not(None)),
        so,
    ).group_by(DimCustomer.name_normalized).subquery()  # GROUP BY 原始列，表达式合法
    date_from = filters["date_from"]
    row = db.execute(
        select(func.count().filter(first.c.first_date >= date_from).label("new"),
               func.count().filter(first.c.first_date < date_from).label("returning"),
               func.count().filter(first.c.first_date.is_(None)).label("undetermined"))
        .select_from(names).join(first, first.c.name == names.c.name)
    ).one()
    new_n, ret_n, und_n = row.new or 0, row.returning or 0, row.undetermined or 0
    return {"available": True, "customers": new_n + ret_n + und_n,
            "new_customers": new_n, "returning_customers": ret_n, "undetermined": und_n}


def summary_compare(db: Session, **filters) -> dict | None:
    """上月对比 KPI：对比期可用时返回 {window, kpi}，否则 None（D-44 §9）。"""
    window = compare_window(filters.get("date_from"), filters.get("date_to"))
    if window is None:
        return None
    data = _summary(db, **_shift_filters(filters, window))
    return {"date_from": window[0].isoformat(), "date_to": window[1].isoformat(),
            "kpi": data["kpi"]}


HISTOGRAM_BINS = 20
HISTOGRAM_STEP = Decimal(1000)   # 与拖动条 1k 步长对齐（D-44 §7）


def amount_histogram(db: Session, *, bins: int = HISTOGRAM_BINS, **filters) -> dict:
    """拖动条上方的订单数分布（D-44 方案 §3.5，参考用，不是固定档位；A-AC09 修订）。

    范围 = 当前其他筛选下（不含金额区间本身）**整单口径**金额（HEADER_BASIS_COLS，
    与拖动条比较域一致）的 0 至最大值；段宽取 1k 的整数倍且段数 ≤ bins。右端**闭**：
    max 恰落段界（8000 / 8501）归入该段所在桶，不丢单。负金额单与无金额单单独
    计数，不并入任何段——``sum(bins) + negative_orders + empty_orders == total``
    逐单守恒；最大值 ≤ 0（含全 0 / 纯负）时给 1 个 [0, width] 段或不给段，空集
    给空段表。
    """
    basis = filters.get("amount_basis") or "amount_ex_tax"
    for k in ("amount_min", "amount_max", "include_negative", "include_empty"):
        filters.pop(k, None)
    base, _ = _order_query(**filters)
    sub = base.subquery()
    col = sub.c[dict(zip(AMOUNT_KEYS, HEADER_BASIS_COLS))[basis]]
    agg = db.execute(select(
        func.count().label("total"),
        func.max(col).label("mx"),
        func.count().filter(col < 0).label("neg"),
        func.count().filter(col.is_(None)).label("empty"),
    ).select_from(sub)).one()
    out_bins: list[dict] = []
    if agg.mx is not None and agg.mx >= 0:
        step = HISTOGRAM_STEP
        # 正数向上取整（勿用 -(-x/bins) 浮点）：width 是 1k 整数倍、使段数 ≤ bins
        width = max(step, -(-Decimal(agg.mx) // bins // step) * step)
        # 右端闭：max 恰为段界时也要有它的桶（floor(mx/width)+1 ≥ 1，全 0 也 1 桶）
        n = int(Decimal(agg.mx) // width) + 1
        idx = func.least(func.floor(col / width), n - 1).label("idx")
        counts = {int(r.idx): r.orders for r in db.execute(
            select(idx, func.count().label("orders")).select_from(sub)
            .where(col >= 0).group_by(idx)).all()}
        out_bins = [
            {"lo": _f(width * i), "hi": _f(width * (i + 1)), "orders": counts.get(i, 0)}
            for i in range(n)
        ]
    return {
        "amount_basis": basis,
        "max": _f(agg.mx),
        "total": agg.total,
        "bins": out_bins,
        "negative_orders": agg.neg,
        "empty_orders": agg.empty,
    }


EXPORT_MAX_ROWS = 10000
# (键, 表头)：导出列与订单列表同口径；被遮蔽的列整列不导出
EXPORT_COLUMNS = (
    ("order_no", "订单号"), ("order_date", "订单日期"), ("customer", "客户"),
    ("customer_type", "客户类型"), ("salesperson", "销售人员"), ("business_type", "业务类型"),
    ("amount_ex_tax", "不含税金额"), ("amount_inc_tax", "含税订单金额"), ("revenue", "营收"),
    ("gross_profit", "毛利"), ("gross_margin", "毛利率"),
)


def export_orders(db: Session, *, sort: str, order: str, **filters) -> tuple[int, list[dict]]:
    """订单导出取数：与 /orders 同筛选同排序（复用 orders()），返回 (总数, 行)。

    总数超过 EXPORT_MAX_ROWS 时只返回总数，由 API 层拒绝（提示收窄区间）。
    导出不含对账块：``with_recon_blocks=False`` 不查快照表也不拼块（对账列排序
    仍可用——排序走内联快照子查询 join，与取块无关）。
    """
    from app.services.sales_analytics import orders as _orders

    probe = _orders(db, None, sort=sort, order=order, page=1, page_size=1,
                    with_recon_blocks=False, **filters)
    total = probe["total"]
    if total > EXPORT_MAX_ROWS or total == 0:
        return total, []
    data = _orders(db, None, sort=sort, order=order, page=1, page_size=total,
                   with_recon_blocks=False, **filters)
    for item in data["items"]:
        item.pop("reconciliation", None)
    return total, data["items"]
