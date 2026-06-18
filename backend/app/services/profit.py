"""利润计算与回填（§7.3）。

对每条已生效销售行：按时间回放成本(cost.replay,移动加权+FIFO)→ 算营收(默认不含税)、
成本、毛利、毛利率、异常标记、是否计营收 → 批量回填 f_sales_line。
规则改动(改 config)后调 recompute() 刷新。

整改 P3：成本回放与利润聚合一律按 part_id 分组（pn_std 仅展示）。
未合并库中 pn_std↔part_id 双射，分组结果与旧口径逐行一致（有等价性测试）；
合并后源/目标的采购销售事件流自动归并为同一条时间线。
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from datetime import date as _date

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from app import config, security
from app.etl import anomaly
from app.models.dimensions import DimCustomer, DimPart
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
    """按 part_id 收集采购入库事件(不含税单价)+ 每 part 兜底价(最近采购价)。"""
    q = (
        select(FPurchaseLine.id, FPurchaseLine.part_id, FPurchaseOrder.order_date,
                FPurchaseLine.qty, FPurchaseLine.unit_price, FPurchaseOrder.tax_rate)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseLine.qty.is_not(None), FPurchaseLine.qty > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))
        # 显式排序：保证重算结果可复现(同日多笔时,数据库返回顺序本身不固定)
        .order_by(FPurchaseOrder.order_date.asc().nullsfirst(), FPurchaseLine.id.asc())
    )
    if config.ACTIVE_STATUS_ONLY:
        q = q.where(FPurchaseOrder.data_status == _ACTIVE)

    events: dict[int, list] = defaultdict(list)
    recent: dict[int, tuple] = {}   # part -> ((date, line_id), ex_price)  含 line_id 保证同日不歧义
    for pl_id, part, odate, qty, price, trate in db.execute(q):
        ex = _ex_tax(price, trate)
        events[part].append(cost.PurchaseEvent(odate, qty, ex))
        key = (odate or date.min, pl_id)
        if part not in recent or key >= recent[part][0]:
            recent[part] = (key, ex)
    fallback = {p: v[1] for p, v in recent.items()} if config.OPENING_COST_POLICY == "fallback_recent" else {}
    return events, fallback


def recompute(db: Session) -> dict:
    """重算所有已生效销售行的成本/利润,批量回填。返回统计。"""
    pur_events, fallback = _load_purchase_events(db)

    sq = (
        select(FSalesLine.id, FSalesLine.part_id, FSalesOrder.order_date,
                FSalesLine.qty, FSalesLine.unit_price, FSalesLine.line_amount,
                FSalesOrder.tax_rate, FSalesOrder.business_type)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        # 同上：显式排序保证重算可复现
        .order_by(FSalesOrder.order_date.asc().nullsfirst(), FSalesLine.id.asc())
    )
    if config.ACTIVE_STATUS_ONLY:
        sq = sq.where(FSalesOrder.data_status == _ACTIVE)
    sales_rows = db.execute(sq).all()

    # 按 part 分组销售事件
    by_part: dict[int, list] = defaultdict(list)
    meta: dict[int, dict] = {}
    for sid, part, odate, qty, up, lamt, trate, btype in sales_rows:
        by_part[part].append(cost.SaleEvent(odate, sid, qty or Decimal(0)))
        meta[sid] = {"part": part, "qty": qty, "unit_price": up, "line_amount": lamt,
                     "tax_rate": trate, "business_type": btype}

    # 回放每个 part
    line_cost: dict[int, cost.LineCost] = {}
    for part, sevents in by_part.items():
        line_cost.update(cost.replay(pur_events.get(part, []), sevents, fallback.get(part)))

    # 数据治理：被人工标记为非标/排除的型号，不计入营收/利润统计（#25）
    excluded_parts = set(db.execute(
        select(DimPart.id).where(DimPart.is_excluded.is_(True))
    ).scalars().all())

    active_is_moving = config.COST_METHOD == "moving_avg"
    updates = []
    stats = {"sales_lines": len(sales_rows), "no_cost": 0, "neg_margin": 0,
             "fallback": 0, "counts_revenue": 0, "excluded_part": 0}

    for sid, m in meta.items():
        lc = line_cost.get(sid)
        qty = m["qty"] or Decimal(0)
        up = m["unit_price"] or Decimal(0)
        trate = m["tax_rate"]
        mov = lc.moving_avg if lc else None
        fifo = lc.fifo if lc else None
        active = mov if active_is_moving else fifo

        revenue = _ex_tax(qty * up, trate).quantize(_CENT)
        is_excluded_part = m["part"] in excluded_parts
        counts = (m["business_type"] in config.REVENUE_BUSINESS_TYPES) and not is_excluded_part
        if counts:
            stats["counts_revenue"] += 1

        # 行级数值异常取决策树根：与 transform 共用同一套规则与容差（app/etl/anomaly.py），
        # 不再在此重写一份 zero_price/amount_mismatch + 0.05（旧实现两处易漂移）。
        flags = anomaly.line_flags(qty, up, m["line_amount"])
        # 以下成本/业务维度分支只有重算时才算得出，是 profit 专属，挂在根之上：
        if is_excluded_part:
            flags.append("excluded_part")
            stats["excluded_part"] += 1
        if not counts and not is_excluded_part:
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
    """利润三维度聚合（§7.3）。仅累计 counts_revenue=true 行；同时返回被排除营收。

    part 维度按 part_id 分组、展示 dim_part.pn_std（合并后历史自动归并到目标型号，
    且 PN 文本变化不破坏历史报表口径）。
    """
    # 数据层兜底（TOOLS-3）：销售员/客户维度会暴露同事的经营数据，受限销售一律不得聚合。
    # 这道防线不单点依赖 agent 工具记得拦——prompts 也声明"真正的防线在数据层"。
    # API 侧本就 require_admin（admin 非 scoped，is_scoped_sales=False，不受影响），
    # 受限销售只可能经 agent 工具到达，此处兜底；正常流里工具已先给出友好提示。
    if dimension in ("salesperson", "customer") and security.is_scoped_sales(user_ctx):
        raise PermissionError("受限销售不得按销售员/客户维度聚合经营数据（防恶性竞争）")

    dim_col = {
        "part": DimPart.pn_std,
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
    elif dimension == "part":
        stmt = stmt.join(DimPart, sl.part_id == DimPart.id)
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
