"""维保出库成本核算引擎 + 项目聚合（docs/维保出库成本核算-开发方案.md §4-§5）。

独立旁路：不改 cost.replay / profit.recompute 的任何语义（回归红线）。

取价瀑布（每条有效出库行，六层，v2 §16.1 黄金样本校准后定型）：
  A0 direct    —— 专属采购直配：采购单「维保需求单」== 本行维保单号（WBDD），同 part 加权价
  A1 window    —— 出库日 ±MAINT_PRICE_WINDOW_DAYS 天内最近采购价（同距取更早、同日加权）
  A2 month_avg —— 同 part 出库当月采购加权均价（客户口径 Q1：当月加权）
  B  trace_avg —— 向前追溯最近有采购的月份，上限 MAINT_TRACE_MAX_MONTHS（Q1/Q6：≤3 月，≥1 月标注）
  C  sales_ref —— 「没有采购有销售」：备件销售真实成交价，同样 当月→追溯（Q3，客户原话标注）
  D  none      —— 无成本，留人工
每层取价 含税(inc) 优先、逐条标注 cost_tax_basis（Q4：原值口径，不换算）。
confidence：direct/window=high（校准中位偏差 0%）、month_avg=medium（6.9%）、
trace_avg/sales_ref=low（无近期采购的估价中位偏差 25%+，不伪装精确）。
起算日（MAINT_COST_START_DATE）前的行不计价：cost_source=NULL，区别于"算了但没算出来"的 none。
"""
import logging
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session, aliased

from app import config, security
from app.business_time import business_today
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, FProjectExpense
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.maintenance_match_keys import exact_match_key
from app.services.query_filters import active_orders, col_matches_any, keyword_groups_or_substr

_log = logging.getLogger("maintenance_cost")
_CENT = Decimal("0.01")
_ZERO = Decimal("0")
# Money 列为 Numeric(14,2)：绝对值上限 10^12（含）会溢出，回填前守卫
_MONEY_MAX = Decimal(10) ** 12
# 导入期写入的行级 flag（recompute 重建 flags 时保留；成本派生 flag 每轮重算重挂）
_IMPORT_FLAGS = frozenset({"future_date"})
COSTED_SOURCES = ("direct", "window", "month_avg", "trace_avg", "sales_ref")
# v2 §16.1：置信度按来源定档——direct/window 校准中位偏差 0%、month_avg 6.9%、追溯/销售参考 25%+
_CONFIDENCE = {"direct": "high", "window": "high", "month_avg": "medium",
               "trace_avg": "low", "sales_ref": "low"}


def _ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _ym_shift(ym: str, k: int) -> str:
    """ym 往前 k 个月（k≥0）。"""
    y, m = int(ym[:4]), int(ym[5:7])
    m -= k
    while m <= 0:
        y -= 1
        m += 12
    return f"{y:04d}-{m:02d}"


def _basis_order() -> tuple[str, str]:
    return ("inc", "ex") if config.MAINT_TAX_PREFERENCE == "inc_first" else ("ex", "inc")


def _purchase_pools(db: Session):
    """构建 直配池 + ±窗口日度池 + 采购月度池。

    direct:  (WBDD_NO_upper, part_id) -> {basis: [Σ金额, Σ数量, 采购单号(最小,可解释)]}
    daily:   part_id -> {采购日: {basis: [Σ金额, Σ数量]}}（window 层：同日加权）
    monthly: (part_id, 'YYYY-MM', basis) -> [Σ金额, Σ数量]
    金额一律 qty×unit_price（采购「其他款项」运费等不摊入单价，§0 客户口径）。
    直配池不过滤采购类型（显式关联到该维保单的采购就是它的成本）；
    日度/月度池按 COST_PURCHASE_TYPES 过滤，各池都排除打包占位 PN。
    """
    q = (
        select(FPurchaseLine.part_id, FPurchaseLine.pn_std, FPurchaseLine.qty,
               FPurchaseLine.unit_price,
               FPurchaseOrder.order_date, FPurchaseOrder.order_no,
               FPurchaseOrder.source_type, FPurchaseOrder.is_tax_inclusive,
               FPurchaseOrder.linked_maintenance_order_no)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseLine.qty.is_not(None), FPurchaseLine.qty > 0)
    )
    q = active_orders(q, FPurchaseOrder)
    excl = {exact_match_key(p) for p in config.MAINT_POOL_EXCLUDE_PNS}
    direct: dict[tuple, dict] = defaultdict(dict)
    daily: dict[int, dict] = defaultdict(dict)
    monthly: dict[tuple, list] = defaultdict(lambda: [_ZERO, _ZERO])
    for part, pn, qty, price, odate, ono, stype, inc, wbdd in db.execute(q):
        if exact_match_key(pn) in excl:
            continue
        basis = "inc" if inc else "ex"
        amt = qty * price
        if wbdd:
            slot = direct[(exact_match_key(wbdd), part)].setdefault(basis, [_ZERO, _ZERO, ono])
            slot[0] += amt
            slot[1] += qty
            if ono and (slot[2] is None or ono < slot[2]):
                slot[2] = ono
        if odate is not None and stype in config.COST_PURCHASE_TYPES:
            key = (part, _ym(odate), basis)
            monthly[key][0] += amt
            monthly[key][1] += qty
            dslot = daily[part].setdefault(odate, {}).setdefault(basis, [_ZERO, _ZERO])
            dslot[0] += amt
            dslot[1] += qty
    # window 层查找用：每 part 的采购日期升序表（bisect 定位 ±窗口）
    daily_dates = {part: sorted(days) for part, days in daily.items()}
    return direct, daily, daily_dates, monthly


def _pick_window(daily: dict, daily_dates: dict, part: int, odate: date, max_days: int):
    """±max_days 内最近采购日（同距取更早），该日按税口径优先取加权均价。

    返回 (unit_cost, basis, distance_days, 取价日) | None。日期近优先于税口径偏好
    （用户口径"近日价更准"），税口径只在同一取价日内做取舍。
    """
    dates = daily_dates.get(part)
    if not dates:
        return None
    lo = bisect_left(dates, odate - timedelta(days=max_days))
    hi = bisect_right(dates, odate + timedelta(days=max_days))
    best = None
    for d in dates[lo:hi]:
        key = (abs((d - odate).days), d)          # 距离升序；同距日期更早者胜
        if best is None or key < best:
            best = key
    if best is None:
        return None
    slots = daily[part][best[1]]
    for basis in _basis_order():
        s = slots.get(basis)
        if s and s[1] > 0:
            return (s[0] / s[1]).quantize(_CENT), basis, best[0], best[1]
    return None


def _sales_pool(db: Session):
    """销售参考池（Q3）：备件销售的真实成交单价 → (part_id, 'YYYY-MM', basis) -> [Σ金额, Σ数量]。"""
    q = (
        select(FSalesLine.part_id, FSalesLine.pn_std, FSalesLine.qty, FSalesLine.unit_price,
               FSalesOrder.order_date, FSalesOrder.tax_rate)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.unit_price.is_not(None), FSalesLine.unit_price > 0,
               FSalesLine.qty.is_not(None), FSalesLine.qty > 0,
               FSalesOrder.business_type.in_(config.MAINT_SALES_REF_BUSINESS_TYPES))
    )
    q = active_orders(q, FSalesOrder)
    excl = set(config.MAINT_POOL_EXCLUDE_PNS)
    monthly: dict[tuple, list] = defaultdict(lambda: [_ZERO, _ZERO])
    for part, pn, qty, price, odate, trate in db.execute(q):
        if pn in excl or odate is None:
            continue
        basis = "inc" if (trate and trate > 0) else "ex"
        key = (part, _ym(odate), basis)
        monthly[key][0] += qty * price
        monthly[key][1] += qty
    return monthly


def _pick(monthly: dict, part: int, ym: str):
    """按税口径优先级取该月加权均价 → (unit_cost, basis) | None。"""
    for basis in _basis_order():
        slot = monthly.get((part, ym, basis))
        if slot and slot[1] > 0:
            return (slot[0] / slot[1]).quantize(_CENT), basis
    return None


# 清零成本字段并把 anomaly_flags 收敛到仅导入期 flag（no_cost/has_return/cost_overflow 每轮重挂）。
# 用 SQL 覆盖全表（含 active_orders 过滤掉的已取消行），避免这些行残留上一轮的成本派生 flag。
_KEEP_FLAGS_SQL = "ARRAY(SELECT f FROM unnest(anomaly_flags) AS f WHERE f = ANY(:keep_flags))"


def recompute(db: Session) -> dict:
    """重算所有作用域内维保出库行的成本，批量回填。返回各来源计数。

    作用域 = 已生效 且 order_date ≥ MAINT_COST_START_DATE；起算日前/无日期 → 不计价（cost_source=NULL）；
    在期但 qty 缺失 → none + missing_qty 标记（可见可查，不静默丢）。
    先整体清零（口径改动后不残留旧值/旧 flag），与导入用同一 advisory lock 串行（防并发重算/导入交错）。
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": config.DATA_CHANGE_ADVISORY_LOCK_KEY},
    )
    direct, daily, daily_dates, monthly = _purchase_pools(db)
    sales_monthly = _sales_pool(db)

    db.execute(
        update(FMaintenanceLine).values(
            unit_cost=None, cost_amount=None, cost_source=None, cost_tax_basis=None,
            price_month=None, trace_months=None, linked_purchase_order_no=None,
            price_distance_days=None, confidence=None,
            anomaly_flags=text(_KEEP_FLAGS_SQL),
        ).execution_options(synchronize_session=False),
        {"keep_flags": list(_IMPORT_FLAGS)},
    )

    q = (
        select(FMaintenanceLine.id, FMaintenanceLine.part_id,
               FMaintenanceLine.qty, FMaintenanceLine.return_qty,
               FMaintenanceLine.anomaly_flags,
               FMaintenanceOrder.order_no, FMaintenanceOrder.order_date)
        .join(FMaintenanceOrder, FMaintenanceLine.order_id == FMaintenanceOrder.id)
    )
    q = active_orders(q, FMaintenanceOrder)
    rows = db.execute(q).all()

    start = config.MAINT_COST_START_DATE
    max_trace = config.MAINT_TRACE_MAX_MONTHS
    window_days = config.MAINT_PRICE_WINDOW_DAYS
    stats = {"lines_in_scope": 0, "out_of_scope": 0, "missing_qty": 0,
             "direct": 0, "window": 0, "month_avg": 0, "trace_avg": 0, "sales_ref": 0,
             "none": 0, "cost_overflow": 0}
    updates = []
    for lid, part, qty, rqty, flags, order_no, odate in rows:
        if odate is None or odate < start:      # 起算日外：不计价，flags 已由上面 SQL 收敛
            stats["out_of_scope"] += 1
            continue
        stats["lines_in_scope"] += 1
        base_flags = [f for f in (flags or []) if f in _IMPORT_FLAGS]
        ym = _ym(odate)
        unit_cost = basis = source = price_month = trace = linked_po = None
        distance = None                          # v2：window 层取价日距离（direct=0）

        if qty is None:                          # 在期但数量缺失：可见的 none，非静默丢弃
            source = "none"
            base_flags.append("missing_qty")
            stats["missing_qty"] += 1

        # A0 专属采购直配
        if source is None:
            slots = direct.get((exact_match_key(order_no), part))
            if slots:
                for b in _basis_order():
                    s = slots.get(b)
                    if s and s[1] > 0:
                        unit_cost = (s[0] / s[1]).quantize(_CENT)
                        basis, source, price_month, trace, linked_po = b, "direct", ym, 0, s[2]
                        distance = 0
                        break
        # A1 ±窗口最近价（v2 §16.1：近日价显著更准，对决 272:122）
        if source is None:
            w = _pick_window(daily, daily_dates, part, odate, window_days)
            if w:
                unit_cost, basis, distance, pdate = w
                source, price_month, trace = "window", _ym(pdate), 0
        # A2 当月均价
        if source is None:
            picked = _pick(monthly, part, ym)
            if picked:
                (unit_cost, basis), source, price_month, trace = picked, "month_avg", ym, 0
        # B 追溯 ≤ max_trace 个月
        if source is None:
            for k in range(1, max_trace + 1):
                picked = _pick(monthly, part, _ym_shift(ym, k))
                if picked:
                    (unit_cost, basis), source = picked, "trace_avg"
                    price_month, trace = _ym_shift(ym, k), k
                    break
        # C 没有采购有销售（同样 当月→追溯）
        if source is None:
            for k in range(0, max_trace + 1):
                picked = _pick(sales_monthly, part, _ym_shift(ym, k))
                if picked:
                    (unit_cost, basis), source = picked, "sales_ref"
                    price_month, trace = _ym_shift(ym, k), k
                    break
        if source is None:
            source = "none"
            base_flags.append("no_cost")

        if rqty and rqty > 0:
            base_flags.append("has_return")
        cost_amount = None
        if unit_cost is not None:
            eff_qty = max((qty or _ZERO) - (rqty or _ZERO), _ZERO)
            cost_amount = (eff_qty * unit_cost).quantize(_CENT)
            # 溢出守卫：单价/数量异常导致金额超 Numeric(14,2) 容量 → 行级隔离（可见可修，不拖垮全批）
            if cost_amount >= _MONEY_MAX or unit_cost >= _MONEY_MAX:
                unit_cost = cost_amount = basis = price_month = trace = linked_po = None
                distance = None
                source = "none"
                base_flags.append("cost_overflow")
                stats["cost_overflow"] += 1

        stats[source] += 1
        updates.append({
            "id": lid, "unit_cost": unit_cost, "cost_amount": cost_amount,
            "cost_source": source, "cost_tax_basis": basis,
            "price_month": price_month, "trace_months": trace,
            "linked_purchase_order_no": linked_po,
            "price_distance_days": distance,
            "confidence": _CONFIDENCE.get(source),
            "anomaly_flags": base_flags,
        })

    for i in range(0, len(updates), 1000):
        db.execute(update(FMaintenanceLine), updates[i:i + 1000])
    db.commit()
    _log.info("maintenance_cost.recompute: %s", stats)
    return stats


# ============================================================
# 项目聚合 / 明细（§5）——只统计作用域内（已生效 + 起算日后）的出库行。
# 聚合下推到 SQL（GROUP BY project_std）避免全量 ORM 物化（大数据量级内存/耗时可控）。
# ============================================================

def _f(x):
    return float(x) if x is not None else None


def _scoped_filters(stmt, date_from, date_to):
    stmt = stmt.where(FMaintenanceOrder.order_date >= config.MAINT_COST_START_DATE)
    stmt = active_orders(stmt, FMaintenanceOrder)
    if date_from:
        stmt = stmt.where(FMaintenanceOrder.order_date >= date_from)
    if date_to:
        stmt = stmt.where(FMaintenanceOrder.order_date <= date_to)
    return stmt


def _matched_maintenance_contracts(date_from, date_to, q_text: str):
    """找出命中项目搜索的合同号，但不裁掉同合同下的其他项目成本。

    看板是合同粒度。搜索只负责召回合同；后续主查询仍聚合该合同在当前日期
    作用域内的完整项目、备件成本和期限，避免把共享合同重算得虚假健康。
    """
    match_line = aliased(FMaintenanceLine)
    match_order = aliased(FMaintenanceOrder)
    contract = func.coalesce(match_order.linked_sales_order_no, "")
    stmt = (
        select(contract)
        .select_from(match_line)
        .join(match_order, match_line.order_id == match_order.id)
        .where(
            match_order.order_date >= config.MAINT_COST_START_DATE,
            match_order.linked_sales_order_no.is_not(None),
        )
    )
    stmt = active_orders(stmt, match_order)
    if date_from:
        stmt = stmt.where(match_order.order_date >= date_from)
    if date_to:
        stmt = stmt.where(match_order.order_date <= date_to)
    for keyword_group in keyword_groups_or_substr(q_text):
        stmt = stmt.where(col_matches_any(match_order.project_std, keyword_group))
    return stmt.distinct()


_LIFECYCLE_FILTERS = frozenset({"ongoing", "ended", "missing", "all"})


def _normalize_lifecycle(lifecycle: str) -> str:
    if lifecycle not in _LIFECYCLE_FILTERS:
        raise ValueError(f"unsupported maintenance lifecycle: {lifecycle}")
    return lifecycle


def _lifecycle_status(missing_count: int, latest_end: date | None, as_of: date) -> str:
    """聚合期限：任一底层单缺日期优先判 missing，否则以最晚终止日判断。"""
    if missing_count or latest_end is None:
        return "missing"
    return "ended" if latest_end < as_of else "ongoing"


def projects_aggregate(db: Session, date_from: date | None = None,
                       date_to: date | None = None, q_text: str | None = None,
                       user_ctx: security.UserContext | None = None,
                       lifecycle: str = "all",
                       as_of: date | None = None) -> dict:
    """项目维度聚合：成本按税口径分列小计（Q4：不混加），来源分布、覆盖率、合同额参考。

    合同额 = 项目关联 XSDD 在销售表的订单金额（含税≈不含税×(1+税率)，参考值）；
    同一 XSDD 挂多个项目时 contract_shared=true——Q5：本期不出毛利，合同额仅参考。
    部分/全部关联单号未在销售表时 contract_incomplete=true（避免按 0 静默低估）。
    """
    lifecycle = _normalize_lifecycle(lifecycle)
    as_of = as_of or business_today()
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    proj = func.coalesce(mo.project_std, "(未填项目)")
    src_cols = [
        func.count().filter(ml.cost_source == s).label(f"src_{s}")
        for s in (*COSTED_SOURCES, "none")
    ]
    stmt = (
        select(
            proj.label("project"),
            func.count().label("lines"),
            func.coalesce(func.sum(ml.qty), 0).label("qty"),
            func.coalesce(func.sum(ml.cost_amount).filter(ml.cost_tax_basis == "inc"), 0).label("cost_inc"),
            func.coalesce(func.sum(ml.cost_amount).filter(ml.cost_tax_basis == "ex"), 0).label("cost_ex"),
            func.count().filter(ml.cost_source.in_(COSTED_SOURCES)).label("costed"),
            func.count(func.distinct(func.date_trunc("month", mo.order_date))).label("months"),
            func.count().filter(mo.maint_end.is_(None)).label("maint_end_missing"),
            func.max(mo.maint_end).label("latest_maint_end"),
            func.array_agg(func.distinct(mo.linked_sales_order_no))
                .filter(mo.linked_sales_order_no.is_not(None)).label("sales_orders"),
            *src_cols,
        )
        .join(mo, ml.order_id == mo.id)
        .group_by(proj)
    )
    stmt = _scoped_filters(stmt, date_from, date_to)
    if q_text and q_text.strip():
        # 分词模糊（大小写不敏感 + 变体，与全站搜索同源）：'联通 备件' 词序无关即可命中项目名
        for g in keyword_groups_or_substr(q_text):
            stmt = stmt.where(col_matches_any(mo.project_std, g))

    raw_all = db.execute(stmt).all()
    lifecycle_counts = {"ongoing": 0, "ended": 0, "missing": 0}
    classified = []
    for row in raw_all:
        lifecycle_status = _lifecycle_status(
            row.maint_end_missing, row.latest_maint_end, as_of,
        )
        lifecycle_counts[lifecycle_status] += 1
        classified.append((row, lifecycle_status))
    raw = [
        (row, lifecycle_status)
        for row, lifecycle_status in classified
        if lifecycle == "all" or lifecycle_status == lifecycle
    ]

    # order_no → 引用它的项目集合（判共用）；合同额一次性查回
    order_no_projects: dict[str, set] = defaultdict(set)
    # 共享合同判定仍看 lifecycle 过滤前的同一业务作用域，避免切换期限后
    # 把本来由多个项目共用的合同误标成独占。
    for r, _lifecycle in classified:
        for ono in (r.sales_orders or []):
            order_no_projects[ono].add(r.project)
    contract: dict[str, Decimal] = {}
    all_orders = list(order_no_projects.keys())
    if all_orders:
        cq = active_orders(
            select(FSalesOrder.order_no, FSalesOrder.amount_ex_tax, FSalesOrder.tax_rate)
            .where(FSalesOrder.order_no.in_(all_orders)), FSalesOrder)
        for ono, ex, trate in db.execute(cq).all():
            if ex is None:
                continue
            inc = (ex * (Decimal(1) + (trate or _ZERO))).quantize(_CENT)
            contract[ono] = max(contract.get(ono, _ZERO), inc)

    rows = []
    for r, lifecycle_status in raw:
        sales_orders = sorted(r.sales_orders or [])
        by_source = {s: getattr(r, f"src_{s}") for s in (*COSTED_SOURCES, "none")}
        missing = [o for o in sales_orders if o not in contract]
        contract_amt = sum((contract.get(o) or _ZERO) for o in sales_orders)
        cost_inc = Decimal(r.cost_inc).quantize(_CENT)
        cost_ex = Decimal(r.cost_ex).quantize(_CENT)
        rows.append({
            "project": r.project,
            "lines": r.lines, "qty": _f(r.qty),
            "cost_inc": _f(cost_inc), "cost_ex": _f(cost_ex),
            "cost_total": _f((cost_inc + cost_ex).quantize(_CENT)),
            "coverage_pct": round(r.costed / r.lines * 100, 1) if r.lines else None,
            "by_source": by_source,
            "months": r.months,
            "sales_orders": sales_orders,
            "contract_amount": _f(contract_amt) if sales_orders else None,
            "contract_shared": any(len(order_no_projects[o]) > 1 for o in sales_orders),
            # 部分/全部关联单号未在销售表中找到金额 → 合同额被低估，前端标注
            "contract_incomplete": bool(sales_orders) and len(missing) > 0,
            "maint_end": (
                r.latest_maint_end.isoformat()
                if lifecycle_status != "missing" and r.latest_maint_end else None
            ),
            "lifecycle_status": lifecycle_status,
        })
    cost_restricted = security.is_field_hidden(user_ctx, "cost_total")
    if cost_restricted:
        # 叶子金额置 null 还不够：按隐藏 cost_total 排序本身会泄漏相对成本，Agent
        # 再截前 N 条时泄漏更严重。无成本权限统一退回项目名稳定序。
        rows.sort(key=lambda r: (r["project"] or "").casefold())
    else:
        rows.sort(key=lambda r: r["cost_total"] or 0, reverse=True)
    return {
        "rows": rows, "start_date": config.MAINT_COST_START_DATE.isoformat(),
        "effective_sort": "project" if cost_restricted else "cost_total",
        "ranking_restricted": cost_restricted,
        "as_of": as_of.isoformat(),
        "lifecycle_filter": lifecycle,
        "lifecycle_counts": lifecycle_counts,
    }


def project_lines(db: Session, project: str, month: str | None = None,
                  date_from: date | None = None, date_to: date | None = None,
                  page: int = 1, page_size: int = 50) -> dict:
    """单项目 SKU 级明细（分页）：含成本来源/税口径/追溯月/关联采购单，逐行可解释。"""
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    base = select(ml, mo).join(mo, ml.order_id == mo.id)
    base = base.where(mo.project_std == project if project != "(未填项目)"
                      else mo.project_std.is_(None))
    base = _scoped_filters(base, date_from, date_to)
    if month:
        base = base.where(func.to_char(mo.order_date, "YYYY-MM") == month)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = max(page, 1)
    paged = base.order_by(
        mo.order_date.desc().nullslast(), ml.id.desc()
    ).offset((page - 1) * page_size).limit(page_size)
    return {"total": total, "page": page, "page_size": page_size, "rows": [{
        "order_no": o.order_no, "order_date": o.order_date.isoformat() if o.order_date else None,
        "demand_type": o.demand_type, "business_type": o.business_type,
        "warehouse": o.warehouse,
        "pn_std": ln.pn_std, "description": ln.description,
        "qty": _f(ln.qty), "return_qty": _f(ln.return_qty),
        "unit_cost": _f(ln.unit_cost), "cost_amount": _f(ln.cost_amount),
        "cost_source": ln.cost_source, "cost_tax_basis": ln.cost_tax_basis,
        "price_month": ln.price_month, "trace_months": ln.trace_months,
        "linked_purchase_order_no": ln.linked_purchase_order_no,
        "price_distance_days": ln.price_distance_days, "confidence": ln.confidence,
        "anomaly_flags": ln.anomaly_flags or [],
    } for ln, o in db.execute(paged).all()]}


# ============================================================
# v2 §16.2 盈亏看板（合同 XSDD 级）+ §16.4 工作簿导出数据
# ============================================================

def _contract_amounts(db: Session, order_nos: list[str]) -> dict[str, Decimal]:
    """XSDD → 合同额（含税参考 = 不含税×(1+税率)）；重复单号取最大。"""
    out: dict[str, Decimal] = {}
    if not order_nos:
        return out
    cq = active_orders(
        select(FSalesOrder.order_no, FSalesOrder.amount_ex_tax, FSalesOrder.tax_rate)
        .where(FSalesOrder.order_no.in_(order_nos)), FSalesOrder)
    for ono, ex, trate in db.execute(cq).all():
        if ex is None:
            continue
        inc = (ex * (Decimal(1) + (trate or _ZERO))).quantize(_CENT)
        out[ono] = max(out.get(ono, _ZERO), inc)
    return out


def _expense_by_contract(db: Session, date_from: date | None = None,
                         date_to: date | None = None) -> dict[str, Decimal]:
    """报销费用（仅生效口径 MAINT_EXPENSE_ACTIVE_STATUS）按 XSDD 归集。"""
    pe = FProjectExpense
    stmt = (
        select(pe.linked_sales_order_no, func.coalesce(func.sum(pe.amount), 0))
        .where(pe.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS,
               pe.linked_sales_order_no.is_not(None))
        .group_by(pe.linked_sales_order_no)
    )
    if date_from:
        stmt = stmt.where(pe.expense_date >= date_from)
    if date_to:
        stmt = stmt.where(pe.expense_date <= date_to)
    return {k: Decimal(v) for k, v in db.execute(stmt).all()}


def board(db: Session, date_from: date | None = None, date_to: date | None = None,
          status: str | None = None,
          user_ctx: security.UserContext | None = None,
          lifecycle: str = "all",
          as_of: date | None = None,
          q_text: str | None = None) -> dict:
    """盈亏看板（用户口径：绿=赚钱、黄=剩余≤20% 报警、红=超支/亏损；合同级聚合天然解决共用合同）。

    预算 = 合同(XSDD)金额（含税参考口径，前端注明）；已花 = 备件成本(混合口径参考) + 生效报销费用。
    无合同额（未关联 XSDD / 销售表查无金额）→ status='no_budget' 单列，只看成本。
    """
    lifecycle = _normalize_lifecycle(lifecycle)
    as_of = as_of or business_today()
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    contract_col = func.coalesce(mo.linked_sales_order_no, "")
    proj = func.coalesce(mo.project_std, "(未填项目)")
    stmt = (
        select(
            contract_col.label("contract"), proj.label("project"),
            func.count().label("lines"),
            func.count().filter(ml.cost_source.in_(COSTED_SOURCES)).label("costed"),
            func.coalesce(func.sum(ml.cost_amount), 0).label("spent_parts"),
            func.coalesce(func.sum(ml.cost_amount).filter(ml.confidence == "low"), 0).label("low_conf"),
            func.min(mo.maint_start).label("mstart"), func.max(mo.maint_end).label("mend"),
            func.count().filter(mo.maint_end.is_(None)).label("mend_missing"),
            func.min(mo.order_date).label("first_out"), func.max(mo.order_date).label("last_out"),
        )
        .join(mo, ml.order_id == mo.id)
        .group_by(contract_col, proj)
    )
    stmt = _scoped_filters(stmt, date_from, date_to)
    if q_text and q_text.strip():
        keyword_groups = keyword_groups_or_substr(q_text)
        unlinked_match = and_(
            mo.linked_sales_order_no.is_(None),
            *(col_matches_any(mo.project_std, group) for group in keyword_groups),
        )
        stmt = stmt.where(or_(
            and_(
                mo.linked_sales_order_no.is_not(None),
                contract_col.in_(_matched_maintenance_contracts(date_from, date_to, q_text)),
            ),
            unlinked_match,
        ))
    raw = db.execute(stmt).all()

    groups: dict[str, dict] = {}
    for r in raw:
        g = groups.setdefault(r.contract, {
            "projects": [], "lines": 0, "costed": 0,
            "spent_parts": _ZERO, "low_conf": _ZERO,
            "mstart": None, "mend": None, "mend_missing": 0,
            "first_out": None, "last_out": None,
        })
        g["projects"].append({"project": r.project, "lines": r.lines,
                              "spent_parts": _f(Decimal(r.spent_parts).quantize(_CENT))})
        g["lines"] += r.lines
        g["costed"] += r.costed
        g["spent_parts"] += Decimal(r.spent_parts)
        g["low_conf"] += Decimal(r.low_conf)
        g["mend_missing"] += r.mend_missing
        for k, v in (("mstart", r.mstart), ("mend", r.mend)):
            if v is not None and (g[k] is None or (v < g[k] if k == "mstart" else v > g[k])):
                g[k] = v
        for k, v, fn in (("first_out", r.first_out, min), ("last_out", r.last_out, max)):
            if v is not None:
                g[k] = v if g[k] is None else fn(g[k], v)

    contracts = _contract_amounts(db, [c for c in groups if c])
    expenses = _expense_by_contract(db, date_from, date_to)
    warn = Decimal(str(config.MAINT_BUDGET_WARN_PCT))

    rows = []
    for cno, g in groups.items():
        spent_parts = g["spent_parts"].quantize(_CENT)
        expense = (expenses.get(cno) or _ZERO).quantize(_CENT)
        spent = (spent_parts + expense).quantize(_CENT)
        budget = contracts.get(cno) if cno else None
        if budget:
            remaining = (budget - spent).quantize(_CENT)
            if spent >= budget:
                st = "red"
            elif remaining <= budget * warn:
                st = "yellow"
            else:
                st = "green"
            remaining_pct = float((remaining / budget * 100).quantize(_CENT))
        else:
            st, remaining, remaining_pct = "no_budget", None, None
        cost_restricted = security.is_field_hidden(user_ctx, "cost_total")
        if cost_restricted:
            g["projects"].sort(key=lambda p: (p["project"] or "").casefold())
        else:
            g["projects"].sort(key=lambda p: -(p["spent_parts"] or 0))
        lifecycle_status = _lifecycle_status(g["mend_missing"], g["mend"], as_of)
        rows.append({
            "contract": cno or None, "status": st,
            "projects": g["projects"],
            "lines": g["lines"],
            "coverage_pct": round(g["costed"] / g["lines"] * 100, 1) if g["lines"] else None,
            "spent_parts": _f(spent_parts), "spent_expense": _f(expense), "spent": _f(spent),
            "budget": _f(budget), "remaining": _f(remaining), "remaining_pct": remaining_pct,
            # 低置信成本占比高 → 卡片提示"估算成分高"
            "low_conf_pct": round(float(g["low_conf"] / spent_parts * 100), 1) if spent_parts else 0.0,
            "maint_start": g["mstart"].isoformat() if g["mstart"] else None,
            "maint_end": (
                g["mend"].isoformat()
                if lifecycle_status != "missing" and g["mend"] else None
            ),
            "lifecycle_status": lifecycle_status,
            "first_out": g["first_out"].isoformat() if g["first_out"] else None,
            "last_out": g["last_out"].isoformat() if g["last_out"] else None,
        })
    order = {"red": 0, "yellow": 1, "green": 2, "no_budget": 3}
    profit_restricted = security.is_field_hidden(user_ctx, "gross_profit")
    if profit_restricted:
        # status 与 status_counts 本身就是盈亏结论；即使金额随后被 mask，红黄绿归属、
        # 状态筛选及排序仍会泄漏。受限用户忽略 status 请求，移除分类结构并按最近出库
        # 日期稳定排序。Agent 与 API 共用本函数，不得各自遗漏。
        lifecycle_counts = {
            lifecycle_status: sum(
                1 for row in rows if row["lifecycle_status"] == lifecycle_status
            )
            for lifecycle_status in ("ongoing", "ended", "missing")
        }
        if lifecycle != "all":
            rows = [row for row in rows if row["lifecycle_status"] == lifecycle]
        rows.sort(
            key=lambda r: (r["last_out"] is not None, r["last_out"] or "", r["contract"] or ""),
            reverse=True,
        )
        for row in rows:
            row.pop("status", None)
        return {
            "rows": rows,
            "profit_restricted": True,
            "ranking_restricted": True,
            "effective_sort": "last_out",
            "status_filter_applied": False,
            "warn_pct": float(warn),
            "start_date": config.MAINT_COST_START_DATE.isoformat(),
            "as_of": as_of.isoformat(),
            "lifecycle_filter": lifecycle,
            "lifecycle_counts": lifecycle_counts,
        }
    rows.sort(key=lambda r: (order[r["status"]], -(r["spent"] or 0)))
    if status:
        rows = [r for r in rows if r["status"] == status]
    lifecycle_counts = {
        lifecycle_status: sum(
            1 for row in rows if row["lifecycle_status"] == lifecycle_status
        )
        for lifecycle_status in ("ongoing", "ended", "missing")
    }
    if lifecycle != "all":
        rows = [row for row in rows if row["lifecycle_status"] == lifecycle]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in order}
    return {"rows": rows, "status_counts": counts,
            "profit_restricted": False, "ranking_restricted": False,
            "effective_sort": "status_then_spent", "status_filter_applied": bool(status),
            "warn_pct": float(warn), "start_date": config.MAINT_COST_START_DATE.isoformat(),
            "as_of": as_of.isoformat(), "lifecycle_filter": lifecycle,
            "lifecycle_counts": lifecycle_counts}


def contract_workbook_data(db: Session, contract: str) -> dict:
    """§16.4 工作簿导出数据：合同抬头 + 月度×分类汇总 + 出库明细(单据级回填) + 报销明细。"""
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    stmt = (
        select(ml, mo).join(mo, ml.order_id == mo.id)
        .where(mo.linked_sales_order_no == contract)
    )
    stmt = _scoped_filters(stmt, None, None)
    stmt = stmt.order_by(mo.order_date.asc().nullslast(), mo.order_no, ml.line_no.asc().nullslast(), ml.id)
    lines = db.execute(stmt).all()

    # 单据级总成本（财务习惯：产品成本恒填在每张 WBDD 首行 = Σ行成本）
    doc_total: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for ln, o in lines:
        if ln.cost_amount is not None:
            doc_total[o.order_no] += ln.cost_amount

    pe = FProjectExpense
    exp_rows = db.execute(
        select(pe).where(pe.linked_sales_order_no == contract)
        .order_by(pe.expense_date.asc().nullslast(), pe.bxd_no, pe.line_no, pe.id)
    ).scalars().all()

    # 月度 × 分类汇总：备件成本按出库月，费用按报销月/费用分类（仅生效）
    monthly: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: _ZERO))
    for ln, o in lines:
        if ln.cost_amount is not None and o.order_date:
            monthly[_ym(o.order_date)]["备件消耗"] += ln.cost_amount
    for e in exp_rows:
        if (e.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS
                and e.amount is not None and e.expense_date):
            monthly[_ym(e.expense_date)][e.fee_category or "(未分类费用)"] += e.amount

    budget = _contract_amounts(db, [contract]).get(contract)
    so = db.execute(active_orders(
        select(FSalesOrder).where(FSalesOrder.order_no == contract), FSalesOrder)
    ).scalars().first()
    return {"contract": contract, "budget": budget, "sales_order": so,
            "lines": lines, "doc_total": doc_total, "expenses": exp_rows, "monthly": monthly}
