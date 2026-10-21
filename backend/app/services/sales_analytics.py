"""销售分析页聚合服务（D-41 §1，PR-3b；D-44 销售看板扩展，2026-10-22 轮复审修订）。

只读分析，挂在销售单模块下的独立「销售分析」页（仅 admin/boss，页面键
``page_sales_analysis``）：看所选区间有多少销售单、多少钱，并按金额倒序追踪大单。
不改动老板看板 ``/dashboard/sales`` 与 OrdersBlock（决策 2026-10-17 Q3）。

口径（单一真值源，本模块不另起炉灶）：
- 订单范围 = 已生效（``query_filters.active_orders``，与老板看板一致）。日期为闭区间
  ``[date_from, date_to]``，不传 = 全部；**不做**看板 KPI 的"未来日期排除"——
  区间由用户显式选择，"全部"就是全部；区间内的未来单保留并以 ``is_future`` 标识
  （Q-A-08）。营收 = 计营收行（``f_sales_line.counts_revenue``）的 ``revenue_amount``
  之和——与老板看板同口径，本模块只消费不重算。
- 三金额并存（D-41 §2）：不含税 ``amount_ex_tax`` / 含税订单金额 ``amount_inc_tax``
  （可空：没有值返回 null，绝不当 0、不用推算值冒充）/ 营收 ``revenue``。
- 金额区间（Q-A-02/Q-A-06 修订）：比较对象恒为**整单口径金额**——不含税 / 含税
  为原单头事实列，营收无单头列、用整单营收（计营收行合计，与行级筛选无关的相关
  子查询）。行级筛选（大类/品牌）只决定入选订单的**展示金额** = 匹配行合计
  （「整单入选、匹配行计额」，响应 ``line_mode`` 标注）。许可开关与有限区间
  **同时满足（交集）**：``include_negative=false`` 时负区间内的负单同样排除；
  无金额单无法与有限区间比较，自然不入（NULL 比较不为真）。
- 行级金额的叶子舍入（DL-018 修订）：行级口径下每条明细行先各自舍入到分（含税
  = 行金额或 数量×单价；不含税 = 含税÷1.13 名义），订单、分组、趋势、KPI 一律对
  **同一批叶子值**求和——四处逐分守恒（A-AC04）。单头口径沿单头事实，与行级口径
  是两种事实层（``line_mode`` 区分），不伪称无条件一致。
- 缺失语义统一（DL-020 修订）：所有分组/筛选维度的 NULL、空串、全空白统一归缺失
  （SQL 层 ``nullif(btrim(...), '')``），分组键为 NULL + ``is_empty=True``，与真实
  名叫「未填」的对象不碰撞（A-AC06）；筛选缺失用哨兵值 ``EMPTY_FILTER``。
- 对账（D-40 §4-§5）：每单挂最新快照（``sales_reconciliation.latest_snapshots_by_
  raw_order_id``：source_modified_at DESC NULLS LAST → import_batch_id DESC → id DESC）；
  ``include_returns`` 只影响 ``reconciled_display``（含 = 已对账 + 退货已对账，
  默认不含 = 只看已对账），不写死扣减。无快照 → has_data=False、金额全 null。
- 脱敏：对账字段键名逐字登记在 ``config.FIELD_GROUPS["sales_reconciliation"]``
  （data_reconciliation 组）；``amount_*`` 沿 purchase_cost 组，客户字段沿
  customer_info 组。行级**营收组合**（qty × 营收 → 成交单价可被精确重建，DL-006，
  A-AC01 反例 2000×1.13÷2=1130）在无 data_purchase_cost 时于服务层随成交价一起
  置空；revenue_costed / cost_coverage 不做全局键登记（老板看板同名键公开），
  由本模块 API 层结构收敛（见 api/sales_analytics.py）。
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

# 大单列表排序键（D-41 §2 三金额；D-44 §4/§5 + 本轮复审：所有可见数据列——含
# 订单号 / 业务类型 / 客户类型与对账块四列 已对账 reconciled_display、待对账
# pending_amount、更新时间 reconciled_as_of、退货已对账 return_reconciled_amount
# ——双向排序）。固定 NULLS LAST（升降都沉底，A-AC08）+ id 同向破并列。
AMOUNT_KEYS = ("amount_ex_tax", "amount_inc_tax", "revenue")
RECON_SORTS = ("reconciled_display", "pending_amount", "reconciled_as_of",
               "return_reconciled_amount")
ORDER_SORTS = AMOUNT_KEYS + (
    "gross_profit", "order_date", "order_no", "customer", "customer_type",
    "salesperson", "business_type",
) + RECON_SORTS
SORT_ORDERS = ("asc", "desc")

# 缺失筛选哨兵（DL-020）：筛「未填」桶传此值。以 ``__`` 包裹与业务命名习惯隔离，
# 与任何真实值不相等；真实值恰好等于哨兵串时按字面精确匹配。
EMPTY_FILTER = "__empty__"
# btrim 第二参：空串 / 纯空白（含全角空格）统一归缺失（A-AC06）。
_WS = " \t\r\n　"


def _f(x) -> float | None:
    return float(tax_policy.round_money(x)) if x is not None else None


def _clean_multi(values: list[str] | None) -> list[str]:
    """多选筛选值清洗：去空白、去空串、保序去重（重复值对 IN 无意义，静默收敛）。"""
    return list(dict.fromkeys(v.strip() for v in (values or []) if v and v.strip()))


def _escape_like(term: str) -> str:
    """ILIKE 通配符转义（#397 (5)）：%/_/\\ 按字面匹配，escape 定界符用反斜杠。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


UNCATEGORIZED_LABEL = "未分类"


def _norm_dim(col):
    """维度统一缺失语义（DL-020 / A-AC06）：NULL / 空串 / 全空白 → NULL，其余去两端空白。

    归一成结构化 NULL 后，缺失组以 NULL 键 + ``is_empty`` 标记呈现，与真实名叫
    「未填」的对象天然不碰撞；候选项与匹配两侧都用同一归一，存储侧尾随空白不再
    造成"选项选了却筛不出来"。
    """
    return func.nullif(func.btrim(col, _WS), "")


def _multi_match(col, values: list[str]):
    """多选精确匹配 + 缺失哨兵：``EMPTY_FILTER`` 匹配归一后的 NULL，其余值精确匹配。"""
    names = [v for v in values if v != EMPTY_FILTER]
    conds = []
    if names:
        conds.append(_norm_dim(col).in_(names))
    if EMPTY_FILTER in values:
        conds.append(_norm_dim(col).is_(None))
    return or_(*conds)


def _category_match(sl, cats: list[str]):
    """商品大类匹配：「未分类」匹配空值（与 D-39 采购分类同一显示约定），其余归一匹配。"""
    named = [c for c in cats if c != UNCATEGORIZED_LABEL]
    conds = []
    if named:
        conds.append(_norm_dim(sl.category_major).in_(named))
    if UNCATEGORIZED_LABEL in cats:
        conds.append(_norm_dim(sl.category_major).is_(None))
    return or_(*conds)


def _leaf_line_amount(sl):
    """行级叶子含税额：行金额（空时 数量 × 含税单价），**先各自舍入到分**（叶子舍入）。"""
    return func.round(func.coalesce(sl.line_amount, sl.qty * sl.unit_price), 2)


# 金额区间 / 分布恒比的「整单口径」列（Q-A-02 修订）：不含税 / 含税为单头事实，
# 营收无单头事实列、由 _header_revenue 相关子查询给整单营收——**与行级筛选无关**
# （行级 join 会使聚合 revenue 只剩匹配行贡献，不能充当区间比较基准）。
HEADER_BASIS_COLS = ("header_amount_ex_tax", "header_amount_inc_tax", "header_revenue")


def _header_revenue(so):
    """整单营收（计营收行合计）的相关标量子查询：按 order_id 独立聚合，不受外层
    行级 join / 筛选影响（Q-A-02「先按整单营收入选，再算匹配行贡献」）。"""
    return (
        select(func.sum(FSalesLine.revenue_amount))
        .where(FSalesLine.order_id == so.id,
               FSalesLine.counts_revenue.is_(True))
        .correlate(so)
        .scalar_subquery()
    )


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
    with_recon_sort: bool = False,
    recon_returns: bool = False,
):
    """销售分析订单基查询：一张销售订单一行（LEFT JOIN 明细聚营收/毛利，无明细行也保留）。

    内层按单分组聚合，外层包成子查询再施加金额区间条件——营收/毛利是聚合值，
    子查询外过滤省去 HAVING 分支（D-44 §7/§10）。返回 (stmt, sort_columns)。

    子查询同时带两组金额列：
    - ``header_amount_ex_tax`` / ``header_amount_inc_tax`` / ``header_revenue``：
      整单口径事实（revenue 无单头列，用整单营收相关子查询）。**金额区间与
      正负/空值开关恒作用于这组**（Q-A-02「整单入选」，行级筛选只改展示金额
      不改入选判定；行级 join 后聚合 revenue 只剩匹配行贡献，不能当比较基准）。
    - ``amount_ex_tax`` / ``amount_inc_tax`` / ``revenue``：展示金额。无行级筛选
      时 = 单头事实 / 营收聚合；有大类/品牌筛选时 = 匹配行合计（明细内连接，无
      匹配行的单不出现；行级叶子舍入后求和，A-AC04 守恒）。下游 summary /
      orders / groups / trend 自动沿用同一批叶子值。

    毛利（D-44 §5，与 dashboard.kpi 同口径）：已配成本 = 计营收且 cost_moving_avg
    非空的行；revenue_costed = 这些行营收之和，cost_costed = 其成本之和，
    gross_profit = 两者之差；单内无已配成本行 → 三者皆 NULL（不当 0）。

    ``with_recon_sort``：内联最新快照子查询（outerjoin raw_order_id），把对账三列
    挂进 sort_columns（reconciled_display 随 ``recon_returns`` 与列表同一定义）。
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
        leaf_inc = _leaf_line_amount(sl)
        amount_cols = (
            func.sum(func.round(leaf_inc / tax_policy.TAX_FACTOR, 2))
            .label("amount_ex_tax"),
            func.sum(leaf_inc).label("amount_inc_tax"),
        )
    else:
        amount_cols = (so.amount_ex_tax, so.amount_inc_tax)
    inner = (
        select(
            so.id, so.order_no, so.order_date, so.salesperson, so.business_type,
            so.data_status, so.raw_order_id, *amount_cols,
            so.amount_ex_tax.label("header_amount_ex_tax"),
            so.amount_inc_tax.label("header_amount_inc_tax"),
            _header_revenue(so).label("header_revenue"),
            _norm_dim(DimCustomer.name_normalized).label("customer"),
            _norm_dim(DimCustomer.customer_type).label("customer_type"),
            _norm_dim(DimCustomer.city).label("customer_city"),
            _norm_dim(DimCustomer.customer_source).label("customer_source"),
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
            match.append(_norm_dim(sl.brand).in_(brs))
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
        inner = inner.where(_multi_match(so.business_type, types))
    if customer and customer.strip():
        inner = inner.where(DimCustomer.name_normalized.ilike(
            f"%{_escape_like(customer.strip())}%", escape="\\"))
    if salesperson and salesperson.strip():
        inner = inner.where(so.salesperson.ilike(
            f"%{_escape_like(salesperson.strip())}%", escape="\\"))
    # 多选精确匹配（D-44 §2/§12）：客户按规范名合并，同名多 customer_id 一并命中；
    # 哨兵 EMPTY_FILTER 匹配「无客户」（无 customer_id 或规范名缺失——归一后同 NULL）。
    names = _clean_multi(customers)
    if names:
        inner = inner.where(_multi_match(DimCustomer.name_normalized, names))
    ctypes = _clean_multi(customer_types)
    if ctypes:
        inner = inner.where(_multi_match(DimCustomer.customer_type, ctypes))
    people = _clean_multi(salespeople)
    if people:
        inner = inner.where(_multi_match(so.salesperson, people))
    # GROUP BY 用原始列（SELECT 里的 nullif(btrim(...)) 是分组列上的表达式，
    # PG 允许；避免 SELECT/GROUP BY 各自渲染 _norm_dim 产生不同绑定参数导致
    # "must appear in the GROUP BY clause"）。
    inner = inner.group_by(
        so.id, DimCustomer.name_normalized, DimCustomer.customer_type,
        DimCustomer.city, DimCustomer.customer_source,
    )

    sub = inner.subquery("sales_orders")
    stmt = select(sub)
    if amount_basis not in AMOUNT_KEYS:
        amount_basis = "amount_ex_tax"
    # 金额区间恒比整单口径（Q-A-02：revenue 用 header_revenue——行级 join 后的聚合
    # 营收只剩匹配行贡献，不是整单事实）。
    hbasis = sub.c[dict(zip(AMOUNT_KEYS, HEADER_BASIS_COLS))[amount_basis]]
    # 许可开关与有限区间**同时满足（交集）**（Q-A-06 修订）：区间不再绕过开关——
    # include_negative=false 时负区间内的负单同样排除；无金额单无法与有限区间比较，
    # 自然不入（NULL 比较不为真）。开关单独（不限区间）语义不变：默认都含，关闭即
    # 排除对应单（NULL 比较不为真，负开关需显式保留 NULL）。
    if not include_negative:
        stmt = stmt.where(or_(hbasis.is_(None), hbasis >= 0))
    if not include_empty:
        stmt = stmt.where(hbasis.is_not(None))
    if amount_min is not None:
        stmt = stmt.where(hbasis >= amount_min)
    if amount_max is not None:
        stmt = stmt.where(hbasis <= amount_max)
    sort_cols = {
        "amount_ex_tax": sub.c.amount_ex_tax,
        "amount_inc_tax": sub.c.amount_inc_tax,
        "revenue": sub.c.revenue,
        "gross_profit": sub.c.gross_profit,
        "order_date": sub.c.order_date,
        "order_no": sub.c.order_no,
        "customer": sub.c.customer,
        "customer_type": sub.c.customer_type,
        "salesperson": sub.c.salesperson,
        "business_type": sub.c.business_type,
        "id": sub.c.id,
    }
    if with_recon_sort:
        latest = _latest_snapshot_subquery()
        stmt = stmt.outerjoin(latest, latest.c.raw_order_id == sub.c.raw_order_id)
        display = latest.c.snap_reconciled
        if recon_returns:
            display = display + func.coalesce(latest.c.snap_return, 0)
        sort_cols["reconciled_display"] = display
        sort_cols["pending_amount"] = latest.c.snap_pending
        sort_cols["reconciled_as_of"] = latest.c.snap_modified
        # 第四个对账可见列（本轮返工 A-AC07）：直接按最新快照的退货已对账排序，
        # 不随 include_returns（那只改 reconciled_display 的展示定义）。
        sort_cols["return_reconciled_amount"] = latest.c.snap_return
    return stmt, sort_cols


def _latest_snapshot_subquery():
    """每单最新对账快影子查询（与 latest_snapshots_by_raw_order_id 同一序：
    source_modified_at DESC NULLS LAST → import_batch_id DESC → id DESC），供对账列排序。

    DISTINCT ON 用 ``Select.distinct(*expr)``（SQLAlchemy 2.0 对 PostgreSQL 的
    DISTINCT ON 语法；2.0.49 无 ``distinct_on`` 成员，用了会在组 SQL 前抛
    AttributeError）。"""
    snap = FSalesReconciliationSnapshot
    return (
        select(
            snap.raw_order_id.label("raw_order_id"),
            snap.reconciled_amount.label("snap_reconciled"),
            snap.return_reconciled_amount.label("snap_return"),
            snap.pending_amount.label("snap_pending"),
            snap.source_modified_at.label("snap_modified"),
        )
        .distinct(snap.raw_order_id)
        .order_by(
            snap.raw_order_id,
            snap.source_modified_at.desc().nulls_last(),
            snap.import_batch_id.desc(),
            snap.id.desc(),
        )
        .subquery("latest_snapshot")
    )


def business_type_options(db: Session) -> dict:
    """业务类型筛选候选取值与计数（D-41 §4：原列「业务类型#」的 14 个原值，不映射）。

    只数已生效单；计数为全时间口径（与生产分布对账用）；NULL / 空白归一成 value=None
    一组不丢弃（DL-020）。排序：计数降序、同计数按值升序——确定序，分页/快照测试可复现。
    """
    so = FSalesOrder
    dim = _norm_dim(so.business_type)
    stmt = active_orders(select(dim.label("value"), func.count().label("orders")), so)
    # GROUP BY 原始列：SELECT 的归一表达式是分组列上的表达式（PG 合法）。
    stmt = stmt.group_by(so.business_type).order_by(
        func.count().desc(), dim.asc().nulls_last()
    )
    return {
        "business_types": [
            {"value": r.value, "orders": r.orders} for r in db.execute(stmt)
        ]
    }


OPTION_LIST_LIMIT = 50


def filter_options(
    db: Session,
    *,
    customer_visible: bool = True,
    amounts_visible: bool = True,
    customer_q: str = "",
    **filters,
) -> dict:
    """D-44 新筛选的候选取值：客户类型、销售人员、客户（按关键字补全）、金额上限。

    - 客户类型 / 销售人员：全时间已生效单去重值与单数，NULL / 空白归一成 value=None
      不丢；排序同 business_type_options（计数降序、值升序）。
    - 客户：按规范名去重（D-44 §12），``customer_q`` 子串匹配，最多 50 条；
      客户字段不可见时客户与客户类型都返回空表（D-44 §11，防存在性探测）。
    - 金额上限：在 ``filters``（当前其他筛选，不含金额区间本身）下三金额各自的
      最大值，供拖动条右端（D-44 §7，前端向上取整到 1k）。**恒取整单口径**
      （HEADER_BASIS_COLS）——拖动条比较的就是这组值，行级筛选下取匹配行最大值
      会与区间比较域不一致。**无 data_purchase_cost 时三个上限全部 null**
      （Q-A-03/A-AC03 修订：金额区间信息整体走成本权限，营收无例外；键名
      amount_*_max / revenue_max 已登记 purchase_cost 组双保险）。
    """
    so = FSalesOrder

    def _counts(col, *joins):
        dim = _norm_dim(col)
        stmt = select(dim.label("value"), func.count().label("orders")).select_from(so)
        for target, cond in joins:
            stmt = stmt.join(target, cond, isouter=True)
        stmt = active_orders(stmt, so).group_by(col).order_by(  # GROUP BY 原始列，见上
            func.count().desc(), dim.asc().nulls_last())
        return [{"value": r.value, "orders": r.orders} for r in db.execute(stmt)]

    salespeople = _counts(so.salesperson)
    customer_types: list[dict] = []
    customers: list[dict] = []
    if customer_visible:
        customer_types = _counts(
            DimCustomer.customer_type, (DimCustomer, so.customer_id == DimCustomer.id))
        name = _norm_dim(DimCustomer.name_normalized)
        stmt = (
            select(name.label("value"), func.count(so.id).label("orders"))
            .select_from(so)
            .join(DimCustomer, so.customer_id == DimCustomer.id)
            .where(name.is_not(None))
        )
        q = (customer_q or "").strip()
        if q:
            stmt = stmt.where(DimCustomer.name_normalized.ilike(
                f"%{_escape_like(q)}%", escape="\\"))
        stmt = active_orders(stmt, so).group_by(DimCustomer.name_normalized).order_by(
            func.count(so.id).desc(), name.asc()
        ).limit(OPTION_LIST_LIMIT)
        customers = [{"value": r.value, "orders": r.orders} for r in db.execute(stmt)]

    sl = FSalesLine

    def _line_counts(col, empty_label=None):
        dim = _norm_dim(col)
        stmt = active_orders(
            select(dim.label("value"), func.count(func.distinct(so.id)).label("orders"))
            .select_from(sl).join(so, sl.order_id == so.id), so,
        ).group_by(col).order_by(func.count(func.distinct(so.id)).desc(),
                                 dim.asc().nulls_last())
        return [{"value": (r.value if r.value is not None else empty_label),
                 "orders": r.orders} for r in db.execute(stmt)]

    categories = _line_counts(sl.category_major, UNCATEGORIZED_LABEL)
    brands = [b for b in _line_counts(sl.brand) if b["value"] is not None]

    for k in ("amount_min", "amount_max", "include_negative", "include_empty",
              "amount_basis"):
        filters.pop(k, None)
    if amounts_visible:
        base, _ = _order_query(**filters)
        sub = base.subquery()
        # 整单口径（HEADER_BASIS_COLS）：与区间比较域一致，行级筛选下不取匹配行值
        mx = db.execute(select(
            func.max(sub.c.header_amount_ex_tax).label("ex"),
            func.max(sub.c.header_amount_inc_tax).label("inc"),
            func.max(sub.c.header_revenue).label("rev"),
        ).select_from(sub)).one()
        amount_range = {
            "amount_ex_tax_max": _f(mx.ex),
            "amount_inc_tax_max": _f(mx.inc),
            "revenue_max": _f(mx.rev),
        }
    else:
        amount_range = {
            "amount_ex_tax_max": None, "amount_inc_tax_max": None, "revenue_max": None,
        }
    return {
        "salespeople": salespeople,
        "categories": categories,
        "brands": brands,
        "customer_types": customer_types,
        "customers": customers,
        "amount_range": amount_range,
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
    无订单时合计为 None 而非 0（区分"查无数据"与"合计为零"，Q-A-05：无订单不伪造
    0 元经营成果）。客户数按规范名去重（D-44 §12），未填客户不计。

    毛利 / 毛利率 / 覆盖率（Q-A-05/Q-A-07 修订）：毛利 = 已配成本营收 − 已配成本，
    全未配成本时为 NULL（前端显示「待配成本」）；**营收 > 0 且全未配时覆盖率
    = 0%**（确无任何已配事实，不是未知）；营收 ≤ 0 / 未知 / 无订单时覆盖率无有效
    基数为 None。毛利率分母 = 已配成本营收，**分母 ≤ 0（含负）不可比较为 None**。
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
        if gross is not None and agg.revenue_costed is not None and agg.revenue_costed > 0
        else None
    )
    coverage = None
    if agg.revenue_total is not None and agg.revenue_total > 0:
        coverage = round(float((agg.revenue_costed or 0) / agg.revenue_total), 4)
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
        #「整单入选、匹配行计额」。
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
    with_recon_blocks: bool = True,
    **filters,
) -> dict:
    """订单列表：按所选列升序或降序 + 分页（D-44 §4 + 本轮复审：所有可见数据列双向）。

    排序固定 ``<key> <dir> NULLS LAST, id <dir>``（空值升降都沉底，A-AC08；PG 默认
    DESC NULLS FIRST，必须显式），id 同向破并列，翻页不丢行/重行。字段遮蔽导致的
    排序回退在 API 层完成（行序防泄漏）；对账列 / 金额毛利列被遮蔽时这里也回退
    order_date 双保险（Q-A-04：单据表回公开日期/id，不退回营收——营收行序同样
    是排名信息）。对账列排序在内联最新快照子查询上做（reconciled_display 的
    定义与列表展示一致，随 include_returns）。user_ctx 无 data_reconciliation
    权限时每单 reconciliation 统一「无快照」形态，不查快照表（#397 (2)）。
    ``with_recon_blocks=False`` 供导出取数：不查快照表也不拼块（导出本就不含
    对账列，省一次按单号回表）。
    """
    today = as_of or business_today()
    if sort not in ORDER_SORTS:
        sort = "amount_ex_tax"
    if order not in SORT_ORDERS:
        order = "desc"
    recon_hidden = _reconciliation_hidden(user_ctx)
    if sort in RECON_SORTS and recon_hidden:
        sort = "order_date"
    if ((sort in AMOUNT_KEYS or sort == "gross_profit") and user_ctx is not None
            and security.is_field_hidden(user_ctx, sort)):
        sort = "order_date"
    base, sort_cols = _order_query(
        with_recon_sort=sort in RECON_SORTS, recon_returns=include_returns, **filters
    )
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    if order == "asc":
        keys = (sort_cols[sort].asc().nulls_last(), sort_cols["id"].asc())
    else:
        keys = (sort_cols[sort].desc().nulls_last(), sort_cols["id"].desc())
    stmt = base.order_by(*keys).limit(page_size).offset((page - 1) * page_size)
    rows = db.execute(stmt).all()
    snaps = {} if recon_hidden or not with_recon_blocks else latest_snapshots_by_raw_order_id(
        db, [r.raw_order_id for r in rows])
    items = []
    for r in rows:
        item = {**_order_fields(r, today)}
        if with_recon_blocks:
            item["reconciliation"] = _reconciliation_block(
                snaps.get(r.raw_order_id), include_returns=include_returns
            )
        items.append(item)
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
            if r.gross_profit is not None and r.revenue_costed is not None
            and r.revenue_costed > 0 else None
        ),
    }


def _order_lines(db: Session, order_id: int, *, cost_hidden: bool) -> list[dict]:
    """订单明细行（D-44 方案 §3.6）：PN、品类、数量、含税单价、行金额、营收、毛利。

    DL-006 修订（A-AC01）：无 data_purchase_cost 时，行级**营收组合**（营收 ÷ 数量
    × 1.13 恒等式可精确重建成交单价，反例 2000×1.13÷2=1130）随成交价一起遮蔽
    ——qty / unit_price / line_amount / revenue 全部置 None，只留身份与分类信息；
    单头聚合营收沿既有公开规则不受影响（聚合值无法逐行重建单价组合）。
    gross_profit / gross_margin 随 data_profit（键已登记 profit 组）；未配成本或
    不计营收的行毛利为 NULL（与 dashboard 已配成本口径一致，不当 0）。
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
            "qty": None if cost_hidden else (float(r.qty) if r.qty is not None else None),
            "unit_price": None if cost_hidden else _f(r.unit_price),
            "line_amount": None if cost_hidden else _f(r.line_amount),
            "revenue": None if cost_hidden else _f(rev),
            "counts_revenue": bool(r.counts_revenue),
            "gross_profit": _f(gp),
            "gross_margin": (
                round(float(gp / r.revenue_amount), 4)
                if gp is not None and r.revenue_amount is not None
                and r.revenue_amount > 0 else None
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
    """订单下钻：三金额 + 明细行 + 最新对账块 + 对账历史（每个快照一条，D-40 §3 只追加）。

    与列表同口径（已生效；非已生效单 404，由 API 层转换）。历史按最新值排序键的
    反序（旧→新：source_modified_at ASC NULLS FIRST → 批次 → id），末条与
    reconciliation 块一致。user_ctx 无 data_reconciliation 权限时 reconciliation
    为「无快照」形态、reconciliation_history=[]，不查快照表（#397 (2)）；
    无 data_purchase_cost 时明细行金额组合整体置空（见 _order_lines）。"""

    today = as_of or business_today()
    base, cols = _order_query()
    row = db.execute(base.where(cols["id"] == order_id)).one_or_none()
    if row is None:
        return None
    cost_hidden = user_ctx is not None and security.is_field_hidden(user_ctx, "unit_price")
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
        "lines": _order_lines(db, row.id, cost_hidden=cost_hidden),
        "as_of": today.isoformat(),
        "reconciliation": _reconciliation_block(
            latest, include_returns=include_returns
        ),
        "reconciliation_history": history,
    }
