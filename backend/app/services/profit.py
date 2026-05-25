"""利润计算与回填（§7.3）。

对每条已生效销售行：按时间回放成本(cost.replay,移动加权+FIFO)→ 算营收(默认不含税)、
成本、毛利、毛利率、异常标记、是否计营收 → 批量回填 f_sales_line。
规则改动(改 config)后调 recompute() 刷新。
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from datetime import date as _date

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from app import config, security
from app.models.dimensions import DimCustomer
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services import cost

_CENT = Decimal("0.01")
_RATE = Decimal("0.0001")
_ACTIVE = "已生效"


def _ex_tax(amount: Decimal, tax_rate: Decimal | None) -> Decimal:
    if config.TAX_BASIS == "ex_tax" and tax_rate:
        return amount / (Decimal(1) + tax_rate)
    return amount


def _load_purchase_events(db: Session):
    """按 pn 收集采购入库事件(不含税单价)+ 每 pn 兜底价(最近采购价)。"""
    q = (
        select(FPurchaseLine.pn_std, FPurchaseOrder.order_date,
                FPurchaseLine.qty, FPurchaseLine.unit_price, FPurchaseOrder.tax_rate)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseLine.qty.is_not(None), FPurchaseLine.qty > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))
    )
    if config.ACTIVE_STATUS_ONLY:
        q = q.where(FPurchaseOrder.data_status == _ACTIVE)

    events: dict[str, list] = defaultdict(list)
    recent: dict[str, tuple] = {}   # pn -> (date, ex_price)  最近采购价兜底
    for pn, odate, qty, price, trate in db.execute(q):
        ex = _ex_tax(price, trate)
        events[pn].append(cost.PurchaseEvent(odate, qty, ex))
        key = odate or date.min
        if pn not in recent or key >= recent[pn][0]:
            recent[pn] = (key, ex)
    fallback = {pn: v[1] for pn, v in recent.items()} if config.OPENING_COST_POLICY == "fallback_recent" else {}
    return events, fallback


def recompute(db: Session) -> dict:
    """重算所有已生效销售行的成本/利润,批量回填。返回统计。"""
    pur_events, fallback = _load_purchase_events(db)

    sq = (
        select(FSalesLine.id, FSalesLine.pn_std, FSalesOrder.order_date,
                FSalesLine.qty, FSalesLine.unit_price, FSalesLine.line_amount,
                FSalesOrder.tax_rate, FSalesOrder.business_type)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
    )
    if config.ACTIVE_STATUS_ONLY:
        sq = sq.where(FSalesOrder.data_status == _ACTIVE)
    sales_rows = db.execute(sq).all()

    # 按 pn 分组销售事件
    by_pn: dict[str, list] = defaultdict(list)
    meta: dict[int, dict] = {}
    for sid, pn, odate, qty, up, lamt, trate, btype in sales_rows:
        by_pn[pn].append(cost.SaleEvent(odate, sid, qty or Decimal(0)))
        meta[sid] = {"pn": pn, "qty": qty, "unit_price": up, "line_amount": lamt,
                     "tax_rate": trate, "business_type": btype}

    # 回放每个 pn
    line_cost: dict[int, cost.LineCost] = {}
    for pn, sevents in by_pn.items():
        line_cost.update(cost.replay(pur_events.get(pn, []), sevents, fallback.get(pn)))

    active_is_moving = config.COST_METHOD == "moving_avg"
    updates = []
    stats = {"sales_lines": len(sales_rows), "no_cost": 0, "neg_margin": 0,
             "fallback": 0, "counts_revenue": 0}

    for sid, m in meta.items():
        lc = line_cost.get(sid)
        qty = m["qty"] or Decimal(0)
        up = m["unit_price"] or Decimal(0)
        trate = m["tax_rate"]
        mov = lc.moving_avg if lc else None
        fifo = lc.fifo if lc else None
        active = mov if active_is_moving else fifo

        revenue = _ex_tax(qty * up, trate).quantize(_CENT)
        counts = m["business_type"] in config.REVENUE_BUSINESS_TYPES
        if counts:
            stats["counts_revenue"] += 1

        flags = []
        if up == 0:
            flags.append("zero_price")
        if (m["line_amount"] is not None and abs(m["line_amount"] - qty * up) > Decimal("0.05")):
            flags.append("amount_mismatch")
        if not counts:
            flags.append("excluded_business_type")

        if active is None:
            cost_amount = gross_profit = gross_margin = None
            flags.append("no_cost")
            stats["no_cost"] += 1
        else:
            cost_amount = (active * qty).quantize(_CENT)
            gross_profit = (revenue - cost_amount).quantize(_CENT)
            gross_margin = (gross_profit / revenue).quantize(_RATE) if revenue > 0 else None
            if gross_margin is not None and gross_margin < 0:
                flags.append("neg_margin")
                stats["neg_margin"] += 1
        if lc and lc.source == "fallback":
            stats["fallback"] += 1

        updates.append({
            "id": sid, "matched_cost": active,
            "cost_moving_avg": mov, "cost_fifo": fifo,
            "cost_source": (lc.source if lc else "none"),
            "revenue_amount": revenue, "cost_amount": cost_amount,
            "gross_profit": gross_profit, "gross_margin": gross_margin,
            "counts_revenue": counts, "anomaly_flags": flags,
        })

    for i in range(0, len(updates), 1000):
        db.execute(update(FSalesLine), updates[i:i + 1000])
    db.commit()
    stats["cost_method"] = config.COST_METHOD
    return stats


def _f(x):
    return float(x) if x is not None else None


def aggregate(db: Session, dimension: str, date_from: _date | None,
              date_to: _date | None, only_anomaly: bool,
              user_ctx: security.UserContext | None = None) -> dict:
    """利润三维度聚合（§7.3）。仅累计 counts_revenue=true 行；同时返回被排除营收。"""
    dim_col = {
        "part": FSalesLine.pn_std,
        "salesperson": FSalesOrder.salesperson,
        "customer": DimCustomer.name_normalized,
    }[dimension]

    sl = FSalesLine
    counts = sl.counts_revenue.is_(True)
    # "已配到成本"的行：moving/fifo 同步有无(引擎 source=none 时两者皆空)
    costed = and_(counts, sl.cost_moving_avg.is_not(None))

    rev_all = func.sum(sl.revenue_amount).filter(counts)
    rev_costed = func.sum(sl.revenue_amount).filter(costed)
    cost_ma = func.sum(sl.cost_moving_avg * sl.qty).filter(costed)
    cost_ff = func.sum(sl.cost_fifo * sl.qty).filter(costed)
    excl = func.sum(sl.revenue_amount).filter(sl.counts_revenue.is_(False))

    stmt = (
        select(
            dim_col.label("dim"),
            rev_all.label("revenue"), rev_costed.label("rev_costed"),
            cost_ma.label("cost_ma"), cost_ff.label("cost_ff"),
            func.count().filter(counts).label("lines"),
            func.count().filter(func.array_position(sl.anomaly_flags, "no_cost").is_not(None)).label("no_cost"),
            excl.label("excluded_revenue"),
        )
        .join(FSalesOrder, sl.order_id == FSalesOrder.id)
    )
    if dimension == "customer":
        stmt = stmt.join(DimCustomer, FSalesOrder.customer_id == DimCustomer.id, isouter=True)
    if config.ACTIVE_STATUS_ONLY:
        stmt = stmt.where(FSalesOrder.data_status == _ACTIVE)
    if date_from:
        stmt = stmt.where(FSalesOrder.order_date >= date_from)
    if date_to:
        stmt = stmt.where(FSalesOrder.order_date <= date_to)
    if only_anomaly:
        stmt = stmt.where(func.cardinality(sl.anomaly_flags) > 0)
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    stmt = stmt.group_by(dim_col).order_by(rev_all.desc().nullslast())

    def _margin(rev, cost):
        if rev and cost is not None and float(rev) != 0:
            return round((float(rev) - float(cost)) / float(rev), 4)
        return None

    def _gp(rev, cost):
        return round(float(rev) - float(cost), 2) if rev is not None and cost is not None else None

    rows = []
    for r in db.execute(stmt).all():
        rc = r.rev_costed
        rows.append({
            "dimension": r.dim or "(未知)",
            "revenue": _f(r.revenue),                 # 计营收总额(全部计营收行)
            "revenue_costed": _f(rc),                 # 其中已配到成本的营收(毛利分母)
            # 移动加权
            "cost_moving_avg": _f(r.cost_ma),
            "gross_profit_moving": _gp(rc, r.cost_ma),
            "gross_margin_moving": _margin(rc, r.cost_ma),
            # 先进先出
            "cost_fifo": _f(r.cost_ff),
            "gross_profit_fifo": _gp(rc, r.cost_ff),
            "gross_margin_fifo": _margin(rc, r.cost_ff),
            "lines": r.lines, "no_cost": r.no_cost, "excluded_revenue": _f(r.excluded_revenue),
        })
    return {"dimension": dimension, "rows": rows}
