"""互通池窗口指标与价格参考——老板看板 v2 的**单一口径源**（编码前设计 §3）。

dashboard（订单 parts / 型号排名）与 pool（池清单 / 池详情）共用本模块取数，
池均价/约束价/越线/参考状态的算法只此一份，防多处漂移。

统计口径（全部沿用现行，勿在调用方重写）：
- 金额未税（services/pricing 单一真值源）；只统计已生效（query_filters.active_orders）；
  未来日期由调用方以 upper=min(date_to, today) 裁掉。
- 采购计价行 = COST_PURCHASE_TYPES + 单价>0 + 数量>0（与 dashboard._purchase_price_stats 同）。
- 销售计价行 = counts_revenue + 单价>0 + 数量>0（¥0 赠送/换货不进价格口径，复审 P1-6）。
- metrics 三元组自洽：weighted_avg_unit_price = total_amount / total_quantity；
  数量为 0/无计价行 → 均价 **null**，绝不用 0 冒充真实均价。
- 越线：行级计数、**严格不等**（等于约束价不算越线，§13）、按**当前**约束价回看窗口内
  历史（分析标签，非按历史时点约束价）；该侧无约束 → null（"无约束"≠"零越线"）。
- 只认 active 人工池；一个 PN 同时只属一个有效池（pool_catalog 写路径保证）。
- 这些只是历史分析标签：不做报价/审批/拦截，不阻止任何业务写入（产品边界 §1）。
"""
from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app import config
from app.models.inventory import PartPool, PartPoolMember, PartPoolPricePolicy
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.pricing import (
    purchase_ex_tax_expr as _purchase_ex_tax_expr,
    purchase_ex_unit as _purchase_ex_unit,
    sale_ex_unit as _sale_ex_unit,
)
from app.services.query_filters import active_orders


def _r(x, n=2):
    return round(float(x), n) if x is not None else None


def _iso(d):
    return d.isoformat() if d else None


# ---------------------------------------------------------------- 池归属 / 约束价

def active_pool_map(db: Session, part_ids) -> dict[int, dict]:
    """part_id → {group_id, pool_name}（仅 active 人工池；一个 PN 至多一条）。"""
    ids = list({p for p in part_ids if p is not None})
    if not ids:
        return {}
    rows = db.execute(
        select(PartPoolMember.part_id, PartPool.group_id, PartPool.name)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .where(PartPoolMember.part_id.in_(ids), PartPool.status == "active")
    ).all()
    return {pid: {"group_id": gid, "pool_name": name} for pid, gid, name in rows}


def current_policies(db: Session, group_ids) -> dict[int, dict]:
    """group_id → 当前约束价 {purchase_ceiling_ex_tax, sales_floor_ex_tax}（float，2 位）。
    无当前策略行的池不出现在结果里（调用方按"无约束"处理）。"""
    gids = list({g for g in group_ids if g is not None})
    if not gids:
        return {}
    rows = db.execute(
        select(PartPoolPricePolicy.group_id,
               PartPoolPricePolicy.purchase_ceiling_ex_tax,
               PartPoolPricePolicy.sales_floor_ex_tax)
        .where(PartPoolPricePolicy.group_id.in_(gids),
               PartPoolPricePolicy.valid_to.is_(None))
    ).all()
    return {gid: {"purchase_ceiling_ex_tax": _r(ceil), "sales_floor_ex_tax": _r(floor)}
            for gid, ceil, floor in rows}


# ---------------------------------------------------------------- 窗口聚合指标

def _metrics_row(amount, qty, orders, latest) -> dict:
    qty_f = float(qty) if qty is not None else 0.0
    return {
        "total_amount": _r(amount),
        "total_quantity": _r(qty, 3),
        # 三元组自洽：均价=总额/总量；数量 0 → null（不给 0 冒充均价）
        "weighted_avg_unit_price": _r(float(amount) / qty_f) if amount is not None and qty_f else None,
        "order_count": orders or 0,
        "latest_date": _iso(latest),
    }


def _purchase_priced():
    """采购计价行过滤条件（与 dashboard._purchase_price_stats 同口径）。"""
    return and_(FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
                FPurchaseLine.qty.is_not(None), FPurchaseLine.qty > 0,
                FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))


def _sales_priced():
    """销售计价行过滤条件（计营收 + 单价>0，复审 P1-6 口径）。"""
    return and_(FSalesLine.counts_revenue.is_(True),
                FSalesLine.unit_price.is_not(None), FSalesLine.unit_price > 0,
                FSalesLine.qty.is_not(None), FSalesLine.qty > 0)


def _window(stmt, order_model, date_from: date | None, upper: date):
    stmt = active_orders(stmt, order_model)
    if date_from:
        stmt = stmt.where(order_model.order_date >= date_from)
    return stmt.where(order_model.order_date <= upper)


def purchase_group_stats(db: Session, date_from: date | None, upper: date,
                         group_ids=None) -> dict[int, dict]:
    """按池聚合的窗口采购指标 + 越线行数（一条 GROUP BY，与池数/订单数无关）。

    返回 group_id → {total_amount, total_quantity, weighted_avg_unit_price, order_count,
    latest_date, violations}。violations 为原始行计数（约束价该侧为空时恒 0），
    是否呈现为 null 由调用方对照 current_policies 决定。
    """
    stmt = (
        select(
            PartPoolMember.group_id,
            func.sum(_purchase_ex_tax_expr()).label("amount"),
            func.sum(FPurchaseLine.qty).label("qty"),
            func.count(func.distinct(FPurchaseOrder.id)).label("orders"),
            func.max(FPurchaseOrder.order_date).label("latest"),
            func.count().filter(
                _purchase_ex_unit() > PartPoolPricePolicy.purchase_ceiling_ex_tax
            ).label("violations"),
        )
        .join(FPurchaseLine, FPurchaseLine.part_id == PartPoolMember.part_id)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .outerjoin(PartPoolPricePolicy,
                   and_(PartPoolPricePolicy.group_id == PartPoolMember.group_id,
                        PartPoolPricePolicy.valid_to.is_(None)))
        .where(PartPool.status == "active", _purchase_priced())
    )
    if group_ids is not None:
        stmt = stmt.where(PartPoolMember.group_id.in_(list(group_ids)))
    stmt = _window(stmt, FPurchaseOrder, date_from, upper).group_by(PartPoolMember.group_id)
    out = {}
    for r in db.execute(stmt):
        out[r.group_id] = {**_metrics_row(r.amount, r.qty, r.orders, r.latest),
                           "violations": r.violations or 0}
    return out


def sales_group_stats(db: Session, date_from: date | None, upper: date,
                      group_ids=None) -> dict[int, dict]:
    """按池聚合的窗口销售指标 + 越线行数（低于销售下限）。结构同 purchase_group_stats。"""
    stmt = (
        select(
            PartPoolMember.group_id,
            func.sum(FSalesLine.revenue_amount).label("amount"),
            func.sum(FSalesLine.qty).label("qty"),
            func.count(func.distinct(FSalesOrder.id)).label("orders"),
            func.max(FSalesOrder.order_date).label("latest"),
            func.count().filter(
                _sale_ex_unit() < PartPoolPricePolicy.sales_floor_ex_tax
            ).label("violations"),
        )
        .join(FSalesLine, FSalesLine.part_id == PartPoolMember.part_id)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .outerjoin(PartPoolPricePolicy,
                   and_(PartPoolPricePolicy.group_id == PartPoolMember.group_id,
                        PartPoolPricePolicy.valid_to.is_(None)))
        .where(PartPool.status == "active", _sales_priced())
    )
    if group_ids is not None:
        stmt = stmt.where(PartPoolMember.group_id.in_(list(group_ids)))
    stmt = _window(stmt, FSalesOrder, date_from, upper).group_by(PartPoolMember.group_id)
    out = {}
    for r in db.execute(stmt):
        out[r.group_id] = {**_metrics_row(r.amount, r.qty, r.orders, r.latest),
                           "violations": r.violations or 0}
    return out


def purchase_part_stats(db: Session, part_ids, date_from: date | None, upper: date) -> dict[int, dict]:
    """按成员 PN 聚合的窗口采购指标（池详情成员板块用；**页面窗口**，区别于遗留
    supply-window 的 purchase_price 统计——两者并存，不互改）。"""
    if not part_ids:
        return {}
    stmt = (
        select(
            FPurchaseLine.part_id,
            func.sum(_purchase_ex_tax_expr()).label("amount"),
            func.sum(FPurchaseLine.qty).label("qty"),
            func.count(func.distinct(FPurchaseOrder.id)).label("orders"),
            func.max(FPurchaseOrder.order_date).label("latest"),
        )
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.part_id.in_(list(part_ids)), _purchase_priced())
    )
    stmt = _window(stmt, FPurchaseOrder, date_from, upper).group_by(FPurchaseLine.part_id)
    return {r.part_id: _metrics_row(r.amount, r.qty, r.orders, r.latest) for r in db.execute(stmt)}


def sales_part_stats(db: Session, part_ids, date_from: date | None, upper: date) -> dict[int, dict]:
    """按成员 PN 聚合的窗口销售指标（池详情成员板块用）。"""
    if not part_ids:
        return {}
    stmt = (
        select(
            FSalesLine.part_id,
            func.sum(FSalesLine.revenue_amount).label("amount"),
            func.sum(FSalesLine.qty).label("qty"),
            func.count(func.distinct(FSalesOrder.id)).label("orders"),
            func.max(FSalesOrder.order_date).label("latest"),
        )
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.part_id.in_(list(part_ids)), _sales_priced())
    )
    stmt = _window(stmt, FSalesOrder, date_from, upper).group_by(FSalesLine.part_id)
    return {r.part_id: _metrics_row(r.amount, r.qty, r.orders, r.latest) for r in db.execute(stmt)}


# ---------------------------------------------------------------- 价格参考判定

def price_reference(side: str, unit_ex: float | None, in_pool: bool,
                    pool_avg: float | None, limit: float | None,
                    manual_restricted: bool) -> dict:
    """单行价格合理性参考（历史分析标签，不拦截）。

    side="purchase"：limit=采购最高价，越线=严格高于；side="sales"：limit=销售最低价，
    越线=严格低于。等于约束价不算越线（§13）。

    reference_status 判定序（全可见）：
      no_pool → no_price → above_manual_max/below_manual_min → above/below_pool_average
      → within_limit（有约束且未越线未劣于池均价）→ no_manual_limit（无约束且不劣于池均价）。

    unit_ex 必须传**未税原值**（不得预先四舍五入）：round 2 位是输出规则不是比较输入，
    否则 (limit, limit+0.005) 带内的行在池级 SQL 计数越线、行级却显示 within_limit，
    看板与明细互相矛盾（审计 P2）。输出差额才做舍入。

    manual_restricted（data_pool_price_governance 关闭）：涉约束价的状态**降级为仅池均价
    口径**（above/below_pool_average / within_pool_average / no_pool_average），且约束价
    与差额一律 None——多行"可见价格×越线布尔"可二分逼出约束价原值，状态本身必须去约束化。
    异常状态对无采购成本/利润权限的用户仍可见（任务要求：状态非金额）；金额靠
    FIELD_GROUPS 键名递归脱敏，本函数不重复处理。
    """
    out = {"pool_avg_delta": None, "pool_avg_delta_pct": None,
           "manual_limit_delta": None, "manual_limit_delta_pct": None}
    if not in_pool:
        out["reference_status"] = "no_pool"
        return out
    if unit_ex is None or unit_ex <= 0:
        # ¥0 赠送/无价行不是价格信号：状态 no_price 且**不输出任何差额**——
        # 否则 0 元行会被当作"低于池均价 100%"的深红异常渲染（审计 P2）。
        out["reference_status"] = "no_price"
        return out
    worse_than_pool = (pool_avg is not None
                       and (unit_ex > pool_avg if side == "purchase" else unit_ex < pool_avg))
    if pool_avg is not None:
        out["pool_avg_delta"] = _r(unit_ex - pool_avg)
        out["pool_avg_delta_pct"] = round((unit_ex - pool_avg) / pool_avg, 4) if pool_avg else None

    if manual_restricted:
        if pool_avg is None:
            out["reference_status"] = "no_pool_average"
        else:
            out["reference_status"] = (("above_pool_average" if side == "purchase"
                                        else "below_pool_average") if worse_than_pool
                                       else "within_pool_average")
        return out

    if limit is not None:
        out["manual_limit_delta"] = _r(unit_ex - limit)
        out["manual_limit_delta_pct"] = round((unit_ex - limit) / limit, 4) if limit else None
        crossed = unit_ex > limit if side == "purchase" else unit_ex < limit
        if crossed:
            out["reference_status"] = "above_manual_max" if side == "purchase" else "below_manual_min"
        elif worse_than_pool:
            out["reference_status"] = "above_pool_average" if side == "purchase" else "below_pool_average"
        else:
            out["reference_status"] = "within_limit"
    else:
        # 约束价为空：绝不误标越线（验收 10）；也不标 within_limit——没有 limit 可 within
        out["reference_status"] = (("above_pool_average" if side == "purchase"
                                    else "below_pool_average") if worse_than_pool
                                   else "no_manual_limit")
    return out
