"""维保出库成本核算引擎 + 项目聚合（docs/维保出库成本核算-开发方案.md §4-§5）。

独立旁路：不改 cost.replay / profit.recompute 的任何语义（回归红线）。

取价瀑布（每条有效出库行，五层）：
  A0 direct    —— 专属采购直配：采购单「维保需求单」== 本行维保单号（WBDD），同 part 加权价
  A1 month_avg —— 同 part 出库当月采购加权均价（客户口径 Q1：当月加权）
  B  trace_avg —— 向前追溯最近有采购的月份，上限 MAINT_TRACE_MAX_MONTHS（Q1/Q6：≤3 月，≥1 月标注）
  C  sales_ref —— 「没有采购有销售」：备件销售真实成交价，同样 当月→追溯（Q3，客户原话标注）
  D  none      —— 无成本，留人工
每层取价 含税(inc) 优先、逐条标注 cost_tax_basis（Q4：原值口径，不换算）。
起算日（MAINT_COST_START_DATE）前的行不计价：cost_source=NULL，区别于"算了但没算出来"的 none。
"""
import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app import config
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.query_filters import active_orders

_log = logging.getLogger("maintenance_cost")
_CENT = Decimal("0.01")
_ZERO = Decimal("0")
# Money 列为 Numeric(14,2)：绝对值上限 10^12（含）会溢出，回填前守卫
_MONEY_MAX = Decimal(10) ** 12
# 与导入互斥用的应用级 advisory lock 键（与 pipeline._ADVISORY_LOCK_KEY 同键，串行化导入与重算）
_ADVISORY_LOCK_KEY = 0x5350_4152
# 导入期写入的行级 flag（recompute 重建 flags 时保留；成本派生 flag 每轮重算重挂）
_IMPORT_FLAGS = frozenset({"future_date"})
COSTED_SOURCES = ("direct", "month_avg", "trace_avg", "sales_ref")


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
    """构建 直配池 + 采购月度池。

    direct:  (wbdd_no, part_id) -> {basis: [Σ金额, Σ数量, 采购单号(最小,可解释)]}
    monthly: (part_id, 'YYYY-MM', basis) -> [Σ金额, Σ数量]
    金额一律 qty×unit_price（采购「其他款项」运费等不摊入单价，§0 客户口径）。
    直配池不过滤采购类型（显式关联到该维保单的采购就是它的成本）；
    月度池按 COST_PURCHASE_TYPES 过滤，两池都排除打包占位 PN。
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
    excl = set(config.MAINT_POOL_EXCLUDE_PNS)
    direct: dict[tuple, dict] = defaultdict(dict)
    monthly: dict[tuple, list] = defaultdict(lambda: [_ZERO, _ZERO])
    for part, pn, qty, price, odate, ono, stype, inc, wbdd in db.execute(q):
        if pn in excl:
            continue
        basis = "inc" if inc else "ex"
        amt = qty * price
        if wbdd:
            slot = direct[(wbdd, part)].setdefault(basis, [_ZERO, _ZERO, ono])
            slot[0] += amt
            slot[1] += qty
            if ono and (slot[2] is None or ono < slot[2]):
                slot[2] = ono
        if odate is not None and stype in config.COST_PURCHASE_TYPES:
            key = (part, _ym(odate), basis)
            monthly[key][0] += amt
            monthly[key][1] += qty
    return direct, monthly


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
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
    direct, monthly = _purchase_pools(db)
    sales_monthly = _sales_pool(db)

    db.execute(
        update(FMaintenanceLine).values(
            unit_cost=None, cost_amount=None, cost_source=None, cost_tax_basis=None,
            price_month=None, trace_months=None, linked_purchase_order_no=None,
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
    stats = {"lines_in_scope": 0, "out_of_scope": 0, "missing_qty": 0,
             "direct": 0, "month_avg": 0, "trace_avg": 0, "sales_ref": 0, "none": 0,
             "cost_overflow": 0}
    updates = []
    for lid, part, qty, rqty, flags, order_no, odate in rows:
        if odate is None or odate < start:      # 起算日外：不计价，flags 已由上面 SQL 收敛
            stats["out_of_scope"] += 1
            continue
        stats["lines_in_scope"] += 1
        base_flags = [f for f in (flags or []) if f in _IMPORT_FLAGS]
        ym = _ym(odate)
        unit_cost = basis = source = price_month = trace = linked_po = None

        if qty is None:                          # 在期但数量缺失：可见的 none，非静默丢弃
            source = "none"
            base_flags.append("missing_qty")
            stats["missing_qty"] += 1

        # A0 专属采购直配
        if source is None:
            slots = direct.get((order_no, part))
            if slots:
                for b in _basis_order():
                    s = slots.get(b)
                    if s and s[1] > 0:
                        unit_cost = (s[0] / s[1]).quantize(_CENT)
                        basis, source, price_month, trace, linked_po = b, "direct", ym, 0, s[2]
                        break
        # A1 当月均价
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
                source = "none"
                base_flags.append("cost_overflow")
                stats["cost_overflow"] += 1

        stats[source] += 1
        updates.append({
            "id": lid, "unit_cost": unit_cost, "cost_amount": cost_amount,
            "cost_source": source, "cost_tax_basis": basis,
            "price_month": price_month, "trace_months": trace,
            "linked_purchase_order_no": linked_po, "anomaly_flags": base_flags,
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


def projects_aggregate(db: Session, date_from: date | None = None,
                       date_to: date | None = None, q_text: str | None = None) -> dict:
    """项目维度聚合：成本按税口径分列小计（Q4：不混加），来源分布、覆盖率、合同额参考。

    合同额 = 项目关联 XSDD 在销售表的订单金额（含税≈不含税×(1+税率)，参考值）；
    同一 XSDD 挂多个项目时 contract_shared=true——Q5：本期不出毛利，合同额仅参考。
    部分/全部关联单号未在销售表时 contract_incomplete=true（避免按 0 静默低估）。
    """
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
            func.array_agg(func.distinct(mo.linked_sales_order_no))
                .filter(mo.linked_sales_order_no.is_not(None)).label("sales_orders"),
            *src_cols,
        )
        .join(mo, ml.order_id == mo.id)
        .group_by(proj)
    )
    stmt = _scoped_filters(stmt, date_from, date_to)
    if q_text:
        stmt = stmt.where(mo.project_std.ilike(f"%{q_text}%"))

    raw = db.execute(stmt).all()

    # order_no → 引用它的项目集合（判共用）；合同额一次性查回
    order_no_projects: dict[str, set] = defaultdict(set)
    for r in raw:
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
    for r in raw:
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
        })
    rows.sort(key=lambda r: r["cost_total"] or 0, reverse=True)
    return {"rows": rows, "start_date": config.MAINT_COST_START_DATE.isoformat()}


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
        "anomaly_flags": ln.anomaly_flags or [],
    } for ln, o in db.execute(paged).all()]}
