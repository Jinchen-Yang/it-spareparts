"""销售分析页聚合服务（D-41 §1，PR-3b）。

只读分析，挂在销售单模块下的独立「销售分析」页（仅 admin/boss，页面键
``page_sales_analysis``）：看所选区间有多少销售单、多少钱，并按金额倒序追踪大单。
不改动老板看板 ``/dashboard/sales`` 与 OrdersBlock（决策 2026-10-17 Q3）。

口径（单一真值源，本模块不另起炉灶）：
- 订单范围 = 已生效（``query_filters.active_orders``，与老板看板一致）。日期为闭区间
  ``[date_from, date_to]``，不传 = 全部；**不做**看板 KPI 的"未来日期排除"——
  区间由用户显式选择，"全部"就是全部（见 orders/summary 的 as_of 仅作展示）。
- 三金额并存（D-41 §2）：不含税 ``amount_ex_tax`` / 含税订单金额 ``amount_inc_tax``
  （可空：没有值返回 null，绝不当 0、不用推算值冒充）/ 营收 ``revenue`` = 订单内
  计营收行（``f_sales_line.counts_revenue``，由导入/利润链路按
  ``config.REVENUE_BUSINESS_TYPES`` 落库）的 ``revenue_amount`` 之和——与老板看板
  ``/dashboard/sales`` 同口径，本模块只消费不重算。
- 对账（D-40 §4-§5）：每单挂最新快照（``sales_reconciliation.latest_snapshots_by_
  raw_order_id``：source_modified_at DESC NULLS LAST → import_batch_id DESC → id DESC）；
  ``include_returns`` 只影响 ``reconciled_display``（含 = 已对账 + 退货已对账，
  默认不含 = 只看已对账），不写死扣减。无快照 → has_data=False、金额全 null。
- 脱敏：对账字段键名（reconciled_amount / pending_amount / return_reconciled_amount /
  order_amount_at_snapshot / reconciled_as_of / reconciled_display）逐字登记在
  ``config.FIELD_GROUPS["sales_reconciliation"]``（data_reconciliation 组），API 层经
  ``apply_field_visibility`` 统一置空；``amount_ex_tax``/``amount_inc_tax``/``customer``
  沿用既有 purchase_cost / customer_info 组的保守遮蔽，营收口径键保持公开。
- 无 data_reconciliation 权限的**整块契约**（#397 (2)）：orders 每单的 reconciliation
  与 order_detail 的 reconciliation/reconciliation_history 输出统一「无快照」形态——
  ``has_data=False``、块内全部键 null、history=[]——且**不查快照表**（连非金额字段
  has_data / reconciled_as_of / import_batch_id / source 也不得泄漏「该单有对账数据」）。
  该形态与真实无快照单完全一致，前端 isReconBlockEmpty 无需区分成因。
- D-44 销售看板扩展：筛选加客户规范名 / 客户类型 / 销售人员多选精确匹配与金额区间
  （口径可选三金额之一、闭区间、负金额 / 无金额两开关，默认都含）；订单列表任意列
  升降序（NULLS LAST）；单头带 customer_type（customer_info 组）与毛利
  gross_profit / gross_margin（profit 组，与 dashboard.kpi 同口径）；详情带明细行。
- 时区：timestamptz 输出（reconciled_as_of / created_at）统一规范成 UTC 带偏移
  （``astimezone(timezone.utc).isoformat()``）——PG 会话时区会改变 aware datetime 的
  原生 isoformat 偏移，规范后输出不随会话漂移（#397 carry-over）。
"""
from datetime import date, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app import security, tax_policy
from app.business_time import business_today
from app.models.dimensions import DimCustomer
from app.models.sales import FSalesLine, FSalesOrder, FSalesReconciliationSnapshot
from app.services.query_filters import active_orders
from app.services.sales_reconciliation import latest_snapshots_by_raw_order_id

# 大单列表排序键（D-41 §2 三金额；D-44 §4/§5 扩到毛利与日期/客户/销售，任意列升降序）。
# 固定 NULLS LAST（升降都沉底）+ id 同向破并列。
AMOUNT_KEYS = ("amount_ex_tax", "amount_inc_tax", "revenue")
ORDER_SORTS = AMOUNT_KEYS + ("gross_profit", "order_date", "customer", "salesperson")
SORT_ORDERS = ("asc", "desc")


def _f(x) -> float | None:
    return float(tax_policy.round_money(x)) if x is not None else None


def _clean_multi(values: list[str] | None) -> list[str]:
    """多选筛选值清洗：去空白、去空串、保序去重（重复值对 IN 无意义，静默收敛）。"""
    return list(dict.fromkeys(v.strip() for v in (values or []) if v and v.strip()))


def _escape_like(term: str) -> str:
    """ILIKE 通配符转义（#397 (5)）：%/_/\\ 按字面匹配，escape 定界符用反斜杠。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


UNCATEGORIZED_LABEL = "未分类"


def _category_match(sl, cats: list[str]):
    """商品大类匹配：「未分类」匹配空值（与 D-39 采购分类同一显示约定）。"""
    named = [c for c in cats if c != UNCATEGORIZED_LABEL]
    conds = []
    if named:
        conds.append(sl.category_major.in_(named))
    if UNCATEGORIZED_LABEL in cats:
        conds.append(sl.category_major.is_(None))
    return or_(*conds)


def _order_query(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    business_types: list[str] | None = None,
    customer: str | None = None,
    salesperson: str | None = None,
    customers: list[str] | None = None,
    customer_types: list[str] | None = None,
    salespeople: list[str] | None = None,
    amount_basis: str = "amount_ex_tax",
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    include_negative: bool = True,
    include_empty: bool = True,
    categories: list[str] | None = None,
    brands: list[str] | None = None,
):
    """销售分析订单基查询：一张销售订单一行（LEFT JOIN 明细聚营收/毛利，无明细行也保留）。

    内层按单分组聚合，外层包成子查询再施加金额区间条件——营收/毛利是聚合值，
    子查询外过滤省去 HAVING 分支，三金额口径同一写法（D-44 §7/§10）。
    返回 (stmt, sort_columns)：sort_columns 供 orders() 排序；summary() 把 stmt 包成
    子查询做合计，保证 KPI = 列表合计（同一口径同一 SQL）。

    毛利（D-44 §5，与 dashboard.kpi 同口径）：已配成本 = 计营收且 cost_moving_avg
    非空的行；revenue_costed = 这些行营收之和，cost_costed = 其成本之和，
    gross_profit = 两者之差；单内无已配成本行 → 三者皆 NULL（不当 0）。

    行级口径（D-44 §8）：传了商品大类 / 品牌筛选时，订单拆成明细行、只计匹配行——
    明细内连接（无匹配行的单不出现），单头两金额改为匹配行合计：含税 = 行金额
    （为空时 数量 × 含税单价），不含税 = 含税 ÷ 1.13（名义，按单四舍五入到分）；
    营收 / 毛利同样只计匹配行。下游 summary / orders / groups / trend 自动沿用。
    """
    so, sl = FSalesOrder, FSalesLine
    cats = _clean_multi(categories)
    brs = _clean_multi(brands)
    line_mode = bool(cats or brs)
    counts = sl.counts_revenue.is_(True)
    costed = and_(counts, sl.cost_moving_avg.is_not(None))
    revenue_costed = func.sum(sl.revenue_amount).filter(costed)
    cost_costed = func.sum(sl.cost_amount).filter(costed)
    if line_mode:
        line_inc = func.sum(func.coalesce(sl.line_amount, sl.qty * sl.unit_price))
        amount_cols = (
            func.round(line_inc / tax_policy.TAX_FACTOR, 2).label("amount_ex_tax"),
            line_inc.label("amount_inc_tax"),
        )
    else:
        amount_cols = (so.amount_ex_tax, so.amount_inc_tax)
    inner = (
        select(
            so.id, so.order_no, so.order_date, so.salesperson, so.business_type,
            so.data_status, so.raw_order_id, *amount_cols,
            DimCustomer.name_normalized.label("customer"),
            DimCustomer.customer_type.label("customer_type"),
            DimCustomer.city.label("customer_city"),
            DimCustomer.customer_source.label("customer_source"),
            so.warehouse,
            func.sum(sl.revenue_amount).filter(counts).label("revenue"),
            revenue_costed.label("revenue_costed"),
            cost_costed.label("cost_costed"),
            (revenue_costed - cost_costed).label("gross_profit"),
        )
        .join(DimCustomer, so.customer_id == DimCustomer.id, isouter=True)
    )
    if line_mode:
        match = [sl.order_id == so.id]
        if cats:
            match.append(_category_match(sl, cats))
        if brs:
            match.append(sl.brand.in_(brs))
        inner = inner.join(sl, and_(*match))
    else:
        inner = inner.join(sl, sl.order_id == so.id, isouter=True)
    inner = active_orders(inner, so)
    if date_from is not None:
        inner = inner.where(so.order_date >= date_from)
    if date_to is not None:
        inner = inner.where(so.order_date <= date_to)
    types = _clean_multi(business_types)
    if types:
        inner = inner.where(so.business_type.in_(types))
    if customer and customer.strip():
        inner = inner.where(DimCustomer.name_normalized.ilike(
            f"%{_escape_like(customer.strip())}%", escape="\\"))
    if salesperson and salesperson.strip():
        inner = inner.where(so.salesperson.ilike(
            f"%{_escape_like(salesperson.strip())}%", escape="\\"))
    # 多选精确匹配（D-44 §2/§12）：客户按规范名合并，同名多 customer_id 一并命中
    names = _clean_multi(customers)
    if names:
        inner = inner.where(DimCustomer.name_normalized.in_(names))
    ctypes = _clean_multi(customer_types)
    if ctypes:
        inner = inner.where(DimCustomer.customer_type.in_(ctypes))
    people = _clean_multi(salespeople)
    if people:
        inner = inner.where(so.salesperson.in_(people))
    inner = inner.group_by(
        so.id, DimCustomer.name_normalized, DimCustomer.customer_type,
        DimCustomer.city, DimCustomer.customer_source,
    )

    sub = inner.subquery("sales_orders")
    stmt = select(sub)
    if amount_basis not in AMOUNT_KEYS:
        amount_basis = "amount_ex_tax"
    basis = sub.c[amount_basis]
    # 金额区间闭区间（D-44 §7）；有区间条件时 NULL 金额自然不落在区间内
    if amount_min is not None:
        stmt = stmt.where(basis >= amount_min)
    if amount_max is not None:
        stmt = stmt.where(basis <= amount_max)
    # 两个开关（D-44 §10）：默认都含；关闭即排除对应单（NULL 比较不为真，需显式保留）
    if not include_negative:
        stmt = stmt.where(or_(basis.is_(None), basis >= 0))
    if not include_empty:
        stmt = stmt.where(basis.is_not(None))
    return stmt, {
        "amount_ex_tax": sub.c.amount_ex_tax,
        "amount_inc_tax": sub.c.amount_inc_tax,
        "revenue": sub.c.revenue,
        "gross_profit": sub.c.gross_profit,
        "order_date": sub.c.order_date,
        "customer": sub.c.customer,
        "salesperson": sub.c.salesperson,
        "id": sub.c.id,
    }


def business_type_options(db: Session) -> dict:
    """业务类型筛选候选取值与计数（D-41 §4：原列「业务类型#」的 14 个原值，不映射）。

    只数已生效单；计数为全时间口径（与生产分布对账用），NULL 归入 value=None 一组
    不丢弃。排序：计数降序、同计数按值升序——确定序，分页/快照测试可复现。
    """
    so = FSalesOrder
    stmt = active_orders(select(so.business_type, func.count().label("orders")), so)
    stmt = stmt.group_by(so.business_type).order_by(
        func.count().desc(), so.business_type.asc().nulls_last()
    )
    return {
        "business_types": [
            {"value": r.business_type, "orders": r.orders} for r in db.execute(stmt)
        ]
    }


OPTION_LIST_LIMIT = 50


def filter_options(
    db: Session,
    *,
    customer_visible: bool = True,
    customer_q: str = "",
    **filters,
) -> dict:
    """D-44 新筛选的候选取值：客户类型、销售人员、客户（按关键字补全）、金额上限。

    - 客户类型 / 销售人员：全时间已生效单去重值与单数，空值 value=None 不丢；
      排序同 business_type_options（计数降序、值升序）。
    - 客户：按规范名去重（D-44 §12），``customer_q`` 子串匹配，最多 50 条；
      客户字段不可见时客户与客户类型都返回空表（D-44 §11，防存在性探测）。
    - 金额上限：在 ``filters``（当前其他筛选，不含金额区间本身）下三金额各自的
      最大值，供拖动条右端（D-44 §7，前端向上取整到 1k）；键名
      amount_ex_tax_max / amount_inc_tax_max 随 purchase_cost 组遮蔽，revenue_max 公开。
    """
    so = FSalesOrder

    def _counts(col, *joins):
        stmt = select(col.label("value"), func.count().label("orders")).select_from(so)
        for target, cond in joins:
            stmt = stmt.join(target, cond, isouter=True)
        stmt = active_orders(stmt, so).group_by(col).order_by(
            func.count().desc(), col.asc().nulls_last())
        return [{"value": r.value, "orders": r.orders} for r in db.execute(stmt)]

    salespeople = _counts(so.salesperson)
    customer_types: list[dict] = []
    customers: list[dict] = []
    if customer_visible:
        customer_types = _counts(
            DimCustomer.customer_type, (DimCustomer, so.customer_id == DimCustomer.id))
        stmt = (
            select(DimCustomer.name_normalized.label("value"),
                   func.count(so.id).label("orders"))
            .select_from(so)
            .join(DimCustomer, so.customer_id == DimCustomer.id)
            .where(DimCustomer.name_normalized.is_not(None))
        )
        q = (customer_q or "").strip()
        if q:
            stmt = stmt.where(DimCustomer.name_normalized.ilike(
                f"%{_escape_like(q)}%", escape="\\"))
        stmt = active_orders(stmt, so).group_by(DimCustomer.name_normalized).order_by(
            func.count(so.id).desc(), DimCustomer.name_normalized.asc()
        ).limit(OPTION_LIST_LIMIT)
        customers = [{"value": r.value, "orders": r.orders} for r in db.execute(stmt)]

    sl = FSalesLine

    def _line_counts(col, empty_label=None):
        stmt = active_orders(
            select(col.label("value"), func.count(func.distinct(so.id)).label("orders"))
            .select_from(sl).join(so, sl.order_id == so.id), so,
        ).group_by(col).order_by(func.count(func.distinct(so.id)).desc(),
                                 col.asc().nulls_last())
        return [{"value": (r.value if r.value is not None else empty_label),
                 "orders": r.orders} for r in db.execute(stmt)]

    categories = _line_counts(sl.category_major, UNCATEGORIZED_LABEL)
    brands = [b for b in _line_counts(sl.brand) if b["value"] is not None]

    for k in ("amount_min", "amount_max", "include_negative", "include_empty", "amount_basis"):
        filters.pop(k, None)
    base, _ = _order_query(**filters)
    sub = base.subquery()
    mx = db.execute(select(
        func.max(sub.c.amount_ex_tax).label("ex"),
        func.max(sub.c.amount_inc_tax).label("inc"),
        func.max(sub.c.revenue).label("rev"),
    ).select_from(sub)).one()
    return {
        "salespeople": salespeople,
        "categories": categories,
        "brands": brands,
        "customer_types": customer_types,
        "customers": customers,
        "amount_range": {
            "amount_ex_tax_max": _f(mx.ex),
            "amount_inc_tax_max": _f(mx.inc),
            "revenue_max": _f(mx.rev),
        },
    }


def summary(
    db: Session,
    *,
    as_of: date | None = None,
    **filters,
) -> dict:
    """所选区间 KPI：订单数 + 三金额合计 + 客户数/销售人数 + 毛利（D-41 验收 / D-44 §5）。

    ``filters`` 即 ``_order_query`` 的全部筛选参数。金额合计跳过 NULL 并单独报告
    amount_inc_tax_missing（缺失单数）——NULL 不当 0、也不用推算值冒充（D-41 §3）。
    无订单时合计为 None 而非 0（区分"查无数据"与"合计为零"）。客户数按规范名去重
    （D-44 §12），未填客户不计。毛利 = 已配成本营收 − 已配成本；毛利率分母 =
    已配成本营收；成本覆盖率 = 已配成本营收 ÷ 营收（与 dashboard.kpi 同口径）。
    """
    today = as_of or business_today()
    base, _ = _order_query(**filters)
    sub = base.subquery()
    agg = db.execute(
        select(
            func.count().label("orders"),
            func.sum(sub.c.amount_ex_tax).label("amount_ex_tax_total"),
            func.sum(sub.c.amount_inc_tax).label("amount_inc_tax_total"),
            func.count()
            .filter(sub.c.amount_inc_tax.is_(None))
            .label("amount_inc_tax_missing"),
            func.sum(sub.c.revenue).label("revenue_total"),
            func.sum(sub.c.revenue_costed).label("revenue_costed"),
            func.sum(sub.c.cost_costed).label("cost_costed"),
            func.count(func.distinct(sub.c.customer)).label("customers"),
            func.count(func.distinct(sub.c.salesperson)).label("salespeople"),
        ).select_from(sub)
    ).one()
    gross = (
        tax_policy.round_money(agg.revenue_costed) - tax_policy.round_money(agg.cost_costed)
        if agg.revenue_costed is not None and agg.cost_costed is not None else None
    )
    margin = (
        round(float(gross / agg.revenue_costed), 4)
        if gross is not None and agg.revenue_costed else None
    )
    coverage = (
        round(float(agg.revenue_costed / agg.revenue_total), 4)
        if agg.revenue_costed is not None and agg.revenue_total else None
    )
    return {
        "window": {
            "date_from": filters["date_from"].isoformat() if filters.get("date_from") else None,
            "date_to": filters["date_to"].isoformat() if filters.get("date_to") else None,
        },
        "filters_echo": {
            "business_type": _clean_multi(filters.get("business_types")),
            "customer": filters.get("customer"),
            "salesperson": filters.get("salesperson"),
            "customers": _clean_multi(filters.get("customers")),
            "customer_types": _clean_multi(filters.get("customer_types")),
            "salespeople": _clean_multi(filters.get("salespeople")),
            "amount_basis": filters.get("amount_basis", "amount_ex_tax"),
            "include_negative": filters.get("include_negative", True),
            "include_empty": filters.get("include_empty", True),
            "categories": _clean_multi(filters.get("categories")),
            "brands": _clean_multi(filters.get("brands")),
        },
        # 行级口径（D-44 §8）：不含税为按 13% 推算的名义值，前端据此标注
        "line_mode": bool(_clean_multi(filters.get("categories"))
                          or _clean_multi(filters.get("brands"))),
        "as_of": today.isoformat(),
        "kpi": {
            "orders": agg.orders,
            "amount_ex_tax_total": _f(agg.amount_ex_tax_total),
            "amount_inc_tax_total": _f(agg.amount_inc_tax_total),
            "amount_inc_tax_missing": agg.amount_inc_tax_missing,
            "revenue_total": _f(agg.revenue_total),
            "customers": agg.customers,
            "salespeople": agg.salespeople,
            "revenue_costed": _f(agg.revenue_costed),
            "gross_profit": _f(gross),
            "gross_margin": margin,
            "cost_coverage": coverage,
        },
    }


def _utc_iso(dt) -> str | None:
    """timestamptz → UTC 带偏移 isoformat（#397 carry-over）。

    PG 把 timestamptz 按会话时区物化成 aware datetime，原生 isoformat 的偏移随
    ``SET TIME ZONE`` 漂移；规范成 UTC 后输出与会话时区无关。naive（无 tzinfo）
    无法换算，原样输出（库内快照列均为 TZDateTime，正常不会走到）。
    """
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else dt.isoformat()


def _reconciliation_block(
    snap: FSalesReconciliationSnapshot | None, *, include_returns: bool
) -> dict:
    """单条最新快照 → 展示块（D-40 §4-§5）。

    reconciled_display：不含退货 = 已对账；含 = 已对账 + 退货已对账（不写死扣减）。
    参与求和的分量按"缺失即不存在"处理（任一有值就给该侧合计，全缺失 → None），
    绝不把空单元格补成 0。snap=None 同时服务「无快照」与「无权限整块」两种成因，
    输出形态一致（#397 (2)）。
    """
    if snap is None:
        return {
            "has_data": False,
            "reconciled_as_of": None,
            "order_amount_at_snapshot": None,
            "reconciled_amount": None,
            "pending_amount": None,
            "return_reconciled_amount": None,
            "reconciled_display": None,
        }
    parts = [snap.reconciled_amount]
    if include_returns:
        parts.append(snap.return_reconciled_amount)
    present = [p for p in parts if p is not None]
    return {
        "has_data": True,
        "reconciled_as_of": _utc_iso(snap.source_modified_at),
        "order_amount_at_snapshot": _f(snap.order_amount),
        "reconciled_amount": _f(snap.reconciled_amount),
        "pending_amount": _f(snap.pending_amount),
        "return_reconciled_amount": _f(snap.return_reconciled_amount),
        "reconciled_display": _f(sum(present)) if present else None,
    }


def _reconciliation_hidden(user_ctx) -> bool:
    """无 data_reconciliation 权限 → 对账整块按「无快照」形态输出且不查快照表。"""
    return user_ctx is not None and security.is_field_hidden(
        user_ctx, "reconciled_amount")


def orders(
    db: Session,
    user_ctx=None,
    *,
    sort: str = "amount_ex_tax",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
    include_returns: bool = False,
    as_of: date | None = None,
    **filters,
) -> dict:
    """订单列表：按所选列升序或降序 + 分页（D-41 §2 → D-44 §4 放开为任意列升降序）。

    排序固定 ``<key> <dir> NULLS LAST, id <dir>``（空值升降都沉底；PG 默认 DESC
    NULLS FIRST，必须显式），id 同向破并列，翻页不丢行/重行。字段遮蔽导致的
    排序回退在 API 层完成（行序防泄漏），本函数只按给定键排。
    user_ctx 无 data_reconciliation 权限时每单 reconciliation 统一「无快照」形态，
    不查快照表（#397 (2)，契约见模块 docstring）。
    """
    today = as_of or business_today()
    if sort not in ORDER_SORTS:
        sort = "amount_ex_tax"
    if order not in SORT_ORDERS:
        order = "desc"
    recon_hidden = _reconciliation_hidden(user_ctx)
    base, sort_cols = _order_query(**filters)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    if order == "asc":
        keys = (sort_cols[sort].asc().nulls_last(), sort_cols["id"].asc())
    else:
        keys = (sort_cols[sort].desc().nulls_last(), sort_cols["id"].desc())
    stmt = base.order_by(*keys).limit(page_size).offset((page - 1) * page_size)
    rows = db.execute(stmt).all()
    snaps = {} if recon_hidden else latest_snapshots_by_raw_order_id(
        db, [r.raw_order_id for r in rows])
    items = []
    for r in rows:
        items.append({
            **_order_fields(r, today),
            "reconciliation": _reconciliation_block(
                snaps.get(r.raw_order_id), include_returns=include_returns
            ),
        })
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of": today.isoformat(),
        "sort": sort,
        "order": order,
        "include_returns": include_returns,
        "items": items,
    }


def _order_fields(r, today: date) -> dict:
    """列表与详情共用的单头字段（同一行形态，键名登记见模块 docstring）。"""
    return {
        "order_id": r.id,
        "order_no": r.order_no,
        "order_date": r.order_date.isoformat() if r.order_date else None,
        "is_future": bool(r.order_date and r.order_date > today),
        "salesperson": r.salesperson,
        "customer": r.customer,
        "customer_type": r.customer_type,
        "business_type": r.business_type,
        "data_status": r.data_status,
        "amount_ex_tax": _f(r.amount_ex_tax),
        "amount_inc_tax": _f(r.amount_inc_tax),
        "revenue": _f(r.revenue),
        "gross_profit": _f(r.gross_profit),
        "gross_margin": (
            round(float(r.gross_profit / r.revenue_costed), 4)
            if r.gross_profit is not None and r.revenue_costed else None
        ),
    }


def _order_lines(db: Session, order_id: int) -> list[dict]:
    """订单明细行（D-44 方案 §3.6）：PN、品类、数量、含税单价、行金额、营收、毛利。

    unit_price / line_amount 随 purchase_cost 组遮蔽（销售行同名键，config 注释
    已说明）；gross_profit / gross_margin 随 data_profit。未配成本或不计营收的行
    毛利为 NULL（与 dashboard 已配成本口径一致，不当 0）。
    """
    sl = FSalesLine
    rows = db.execute(
        select(
            sl.id, sl.line_no, sl.pn_std, sl.pn_raw, sl.description, sl.category_major,
            sl.brand, sl.qty, sl.unit_price, sl.line_amount, sl.revenue_amount,
            sl.counts_revenue, sl.cost_moving_avg, sl.cost_amount,
        )
        .where(sl.order_id == order_id)
        .order_by(sl.line_no.asc().nulls_last(), sl.id.asc())
    ).all()
    out = []
    for r in rows:
        costed = bool(r.counts_revenue) and r.cost_moving_avg is not None
        rev = r.revenue_amount if r.counts_revenue else None
        gp = (
            tax_policy.round_money(r.revenue_amount) - tax_policy.round_money(r.cost_amount)
            if costed and r.revenue_amount is not None and r.cost_amount is not None
            else None
        )
        out.append({
            "line_id": r.id,
            "line_no": r.line_no,
            "pn": r.pn_std or r.pn_raw,
            "description": r.description,
            "category_major": r.category_major,
            "brand": r.brand,
            "qty": float(r.qty) if r.qty is not None else None,
            "unit_price": _f(r.unit_price),
            "line_amount": _f(r.line_amount),
            "revenue": _f(rev),
            "counts_revenue": bool(r.counts_revenue),
            "gross_profit": _f(gp),
            "gross_margin": (
                round(float(gp / r.revenue_amount), 4)
                if gp is not None and r.revenue_amount else None
            ),
        })
    return out


def order_detail(
    db: Session,
    order_id: int,
    user_ctx=None,
    *,
    include_returns: bool = False,
    as_of: date | None = None,
) -> dict | None:
    """订单下钻：三金额 + 最新对账块 + 对账历史（每个快照一条，D-40 §3 只追加）。

    与列表同口径（已生效；非已生效单 404，由 API 层转换）。历史按最新值排序键的
    反序（旧→新：source_modified_at ASC NULLS FIRST → 批次 → id），末条与
    reconciliation 块一致。user_ctx 无 data_reconciliation 权限时 reconciliation
    为「无快照」形态、reconciliation_history=[]，不查快照表（#397 (2)）。"""

    today = as_of or business_today()
    base, cols = _order_query()
    row = db.execute(base.where(cols["id"] == order_id)).one_or_none()
    if row is None:
        return None
    if _reconciliation_hidden(user_ctx):
        history_rows = []
    else:
        history_rows = db.scalars(
            select(FSalesReconciliationSnapshot)
            .where(FSalesReconciliationSnapshot.raw_order_id == row.raw_order_id)
            .order_by(
                FSalesReconciliationSnapshot.source_modified_at.asc().nulls_first(),
                FSalesReconciliationSnapshot.import_batch_id.asc(),
                FSalesReconciliationSnapshot.id.asc(),
            )
        ).all()
    history = [
        {
            "import_batch_id": snap.import_batch_id,
            "source": snap.source,
            "created_at": _utc_iso(snap.created_at),
            "reconciled_as_of": _utc_iso(snap.source_modified_at),
            "order_amount_at_snapshot": _f(snap.order_amount),
            "reconciled_amount": _f(snap.reconciled_amount),
            "pending_amount": _f(snap.pending_amount),
            "return_reconciled_amount": _f(snap.return_reconciled_amount),
        }
        for snap in history_rows
    ]
    latest = history_rows[-1] if history_rows else None
    return {
        **_order_fields(row, today),
        "lines": _order_lines(db, row.id),
        "as_of": today.isoformat(),
        "reconciliation": _reconciliation_block(
            latest, include_returns=include_returns
        ),
        "reconciliation_history": history,
    }
