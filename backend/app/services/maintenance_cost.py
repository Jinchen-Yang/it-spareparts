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

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import config
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.query_filters import active_orders

_log = logging.getLogger("maintenance_cost")
_CENT = Decimal("0.01")
_ZERO = Decimal("0")
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


def recompute(db: Session) -> dict:
    """重算所有作用域内维保出库行的成本，批量回填。返回各来源计数。

    作用域 = 已生效 且 order_date ≥ MAINT_COST_START_DATE 且 qty 非空。
    先整体清零成本字段（口径改动后不残留旧值），作用域外的行保持 NULL。
    """
    direct, monthly = _purchase_pools(db)
    sales_monthly = _sales_pool(db)

    db.execute(update(FMaintenanceLine).values(
        unit_cost=None, cost_amount=None, cost_source=None, cost_tax_basis=None,
        price_month=None, trace_months=None, linked_purchase_order_no=None))

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
    stats = {"lines_in_scope": 0, "out_of_scope": 0,
             "direct": 0, "month_avg": 0, "trace_avg": 0, "sales_ref": 0, "none": 0}
    updates = []
    for lid, part, qty, rqty, flags, order_no, odate in rows:
        if odate is None or odate < start or qty is None:
            stats["out_of_scope"] += 1
            continue
        stats["lines_in_scope"] += 1
        base_flags = [f for f in (flags or []) if f in _IMPORT_FLAGS]
        ym = _ym(odate)
        unit_cost = basis = source = price_month = trace = linked_po = None

        # A0 专属采购直配
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
        eff_qty = (qty or _ZERO) - (rqty or _ZERO)
        cost_amount = None
        if unit_cost is not None:
            cost_amount = (max(eff_qty, _ZERO) * unit_cost).quantize(_CENT)

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
# 项目聚合 / 明细（§5）——只统计作用域内（已生效 + 起算日后）的出库行
# ============================================================

def _f(x):
    return float(x) if x is not None else None


def _scoped_lines_stmt():
    stmt = (
        select(FMaintenanceLine, FMaintenanceOrder)
        .join(FMaintenanceOrder, FMaintenanceLine.order_id == FMaintenanceOrder.id)
        .where(FMaintenanceOrder.order_date >= config.MAINT_COST_START_DATE)
    )
    return active_orders(stmt, FMaintenanceOrder)


def projects_aggregate(db: Session, date_from: date | None = None,
                       date_to: date | None = None, q_text: str | None = None) -> dict:
    """项目维度聚合：成本按税口径分列小计（Q4：不混加），来源分布、覆盖率、合同额参考。

    合同额 = 项目关联 XSDD 在销售表的订单金额（含税≈不含税×(1+税率)，参考值）；
    同一 XSDD 挂多个项目时 contract_shared=true——Q5：本期不出毛利，合同额仅参考。
    """
    stmt = _scoped_lines_stmt()
    if date_from:
        stmt = stmt.where(FMaintenanceOrder.order_date >= date_from)
    if date_to:
        stmt = stmt.where(FMaintenanceOrder.order_date <= date_to)
    if q_text:
        stmt = stmt.where(FMaintenanceOrder.project_std.ilike(f"%{q_text}%"))

    projects: dict[str, dict] = {}
    order_no_projects: dict[str, set] = defaultdict(set)
    for ln, mo in db.execute(stmt).all():
        key = mo.project_std or "(未填项目)"
        p = projects.setdefault(key, {
            "project": key, "lines": 0, "qty": _ZERO,
            "cost_inc": _ZERO, "cost_ex": _ZERO,
            "by_source": {s: 0 for s in (*COSTED_SOURCES, "none")},
            "months": set(), "sales_orders": set(),
        })
        p["lines"] += 1
        p["qty"] += ln.qty or _ZERO
        if ln.cost_amount is not None and ln.cost_tax_basis in ("inc", "ex"):
            p["cost_" + ln.cost_tax_basis] += ln.cost_amount
        if ln.cost_source:
            p["by_source"][ln.cost_source] = p["by_source"].get(ln.cost_source, 0) + 1
        if mo.order_date:
            p["months"].add(_ym(mo.order_date))
        if mo.linked_sales_order_no:
            p["sales_orders"].add(mo.linked_sales_order_no)
            order_no_projects[mo.linked_sales_order_no].add(key)

    # 合同额（参考）：关联 XSDD 的订单金额（含税≈ex×(1+税率)）
    contract: dict[str, Decimal] = {}
    all_orders = list(order_no_projects.keys())
    if all_orders:
        cq = (select(FSalesOrder.order_no, FSalesOrder.amount_ex_tax, FSalesOrder.tax_rate)
              .where(FSalesOrder.order_no.in_(all_orders)))
        cq = active_orders(cq, FSalesOrder)
        for ono, ex, trate in db.execute(cq).all():
            if ex is None:
                continue
            inc = (ex * (Decimal(1) + (trate or _ZERO))).quantize(_CENT)
            # 同一单号理论唯一；防御性取最大
            contract[ono] = max(contract.get(ono, _ZERO), inc)

    rows = []
    for p in projects.values():
        costed = sum(v for k, v in p["by_source"].items() if k in COSTED_SOURCES)
        contract_amt = sum((contract.get(o) or _ZERO) for o in p["sales_orders"])
        rows.append({
            "project": p["project"],
            "lines": p["lines"], "qty": _f(p["qty"]),
            "cost_inc": _f(p["cost_inc"].quantize(_CENT)),
            "cost_ex": _f(p["cost_ex"].quantize(_CENT)),
            "cost_total": _f((p["cost_inc"] + p["cost_ex"]).quantize(_CENT)),
            "coverage_pct": round(costed / p["lines"] * 100, 1) if p["lines"] else None,
            "by_source": p["by_source"],
            "months": len(p["months"]),
            "sales_orders": sorted(p["sales_orders"]),
            "contract_amount": _f(contract_amt) if p["sales_orders"] else None,
            "contract_shared": any(len(order_no_projects[o]) > 1 for o in p["sales_orders"]),
        })
    rows.sort(key=lambda r: r["cost_total"] or 0, reverse=True)
    return {"rows": rows, "start_date": config.MAINT_COST_START_DATE.isoformat()}


def project_lines(db: Session, project: str, month: str | None = None,
                  page: int = 1, page_size: int = 50) -> dict:
    """单项目 SKU 级明细（分页）：含成本来源/税口径/追溯月/关联采购单，逐行可解释。"""
    stmt = _scoped_lines_stmt().where(
        (FMaintenanceOrder.project_std == project)
        if project != "(未填项目)" else FMaintenanceOrder.project_std.is_(None))
    rows = db.execute(stmt.order_by(
        FMaintenanceOrder.order_date.desc().nullslast(), FMaintenanceLine.id.desc()
    )).all()
    if month:
        rows = [(ln, mo) for ln, mo in rows if mo.order_date and _ym(mo.order_date) == month]
    total = len(rows)
    page = max(page, 1)
    chunk = rows[(page - 1) * page_size: page * page_size]
    return {"total": total, "page": page, "page_size": page_size, "rows": [{
        "order_no": mo.order_no, "order_date": mo.order_date.isoformat() if mo.order_date else None,
        "demand_type": mo.demand_type, "business_type": mo.business_type,
        "warehouse": mo.warehouse,
        "pn_std": ln.pn_std, "description": ln.description,
        "qty": _f(ln.qty), "return_qty": _f(ln.return_qty),
        "unit_cost": _f(ln.unit_cost), "cost_amount": _f(ln.cost_amount),
        "cost_source": ln.cost_source, "cost_tax_basis": ln.cost_tax_basis,
        "price_month": ln.price_month, "trace_months": ln.trace_months,
        "linked_purchase_order_no": ln.linked_purchase_order_no,
        "anomaly_flags": ln.anomaly_flags or [],
    } for ln, mo in chunk]}
