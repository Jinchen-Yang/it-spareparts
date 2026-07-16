"""互通池历史价格分析的单一读模型（DEV-03/04）。

公开价格纪律场景不复用全局销售匿名化：经办人和订单事实对持有
``page_pool_analysis`` 的实名用户公开；全部价格事实与约束统一由池价格治理权限控制，
客户、供应商仍分别按 data_* 权限结构化隐藏。本模块只读，不含报价、审批、拦截或建议。
"""
from copy import deepcopy
from datetime import date, timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from app import config
from app.business_time import business_today
from app.models.dimensions import DimCustomer, DimPart, DimSupplier
from app.models.inventory import PartPool, PartPoolMember, PartPoolPricePolicy
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app import security
from app.services import pool, pool_metrics
from app.services.pool_price_rules import (
    apply_price_window as _window,
    purchase_priced_condition as _purchase_priced_condition,
    sales_priced_condition as _sales_priced_condition,
)
from app.services.pricing import purchase_ex_tax_expr, purchase_ex_unit, sale_ex_unit

_EMPTY_STATS = {
    "weighted_avg": None, "median": None, "min": None, "max": None, "latest": None,
    "total_amount": None, "total_qty": None, "order_count": 0, "line_count": 0,
    "latest_date": None, "violation_count": 0,
}


class WindowValidationError(ValueError):
    """统计窗口不完整或无有效日期。"""


def _r(value, digits: int = 2):
    return round(float(value), digits) if value is not None else None


def resolve_window(range_: str | None, date_from: date | None, date_to: date | None,
                   as_of: date | None = None) -> tuple[date | None, date, date, str]:
    # 生产容器可能是 UTC；早会窗口必须按北京时间跨日，不能在 00:00-08:00 少一天。
    today = as_of or business_today()
    if (date_from is None) != (date_to is None):
        raise WindowValidationError("自定义时间必须同时提供 date_from 和 date_to")
    if date_from is not None and date_to is not None:
        if date_from > date_to:
            raise WindowValidationError("date_from 不能晚于 date_to")
        upper = min(date_to, today)
        if date_from > upper:
            raise WindowValidationError("统计窗口不能完全位于未来")
        return date_from, upper, today, "custom"
    upper = today
    token = range_ or "90d"
    if token == "custom":
        raise WindowValidationError("range=custom 必须同时提供 date_from 和 date_to")
    if token not in {"30d", "90d", "365d", "all"}:
        raise WindowValidationError("range 仅支持 30d、90d、365d、all、custom")
    days = {"30d": 30, "90d": 90, "365d": 365}.get(token)
    lower = upper - timedelta(days=days - 1) if days else None
    return lower, upper, today, token


def _purchase_type_filter(stmt, purchase_type: str | None):
    return (stmt.where(FPurchaseOrder.source_type == purchase_type)
            if purchase_type and purchase_type.strip() else stmt)


def _stats(row) -> dict:
    if row is None:
        return dict(_EMPTY_STATS)
    qty = float(row.qty or 0)
    return {
        "weighted_avg": _r(float(row.amount) / qty) if row.amount is not None and qty else None,
        "median": _r(row.median), "min": _r(row.minimum), "max": _r(row.maximum),
        "latest": _r(row.latest), "total_amount": _r(row.amount),
        "total_qty": _r(row.qty, 3), "order_count": int(row.orders or 0),
        "line_count": int(row.lines or 0),
        "latest_date": (row.latest_date.isoformat()
                        if getattr(row, "latest_date", None) else None),
        "violation_count": int(getattr(row, "violations", 0) or 0),
    }


def _purchase_group_stats(db: Session, group_ids: list[int], lower: date | None,
                          upper: date, purchase_type: str | None = None) -> dict[int, dict]:
    if not group_ids:
        return {}
    ex = purchase_ex_unit()
    stmt = (
        select(PartPoolMember.group_id.label("key"),
               func.sum(purchase_ex_tax_expr()).label("amount"),
               func.sum(FPurchaseLine.qty).label("qty"),
               func.percentile_cont(0.5).within_group(ex).label("median"),
               func.min(ex).label("minimum"), func.max(ex).label("maximum"),
               func.array_agg(aggregate_order_by(ex, FPurchaseOrder.order_date.desc(),
                                                 FPurchaseLine.id.desc()))[1].label("latest"),
               func.max(FPurchaseOrder.order_date).label("latest_date"),
               func.count(func.distinct(FPurchaseOrder.id)).label("orders"),
               func.count(FPurchaseLine.id).label("lines"),
               func.count(FPurchaseLine.id).filter(
                   ex > PartPoolPricePolicy.purchase_ceiling_ex_tax
               ).label("violations"))
        .select_from(PartPoolMember)
        .join(FPurchaseLine, FPurchaseLine.part_id == PartPoolMember.part_id)
        .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
        .outerjoin(PartPoolPricePolicy, and_(
            PartPoolPricePolicy.group_id == PartPoolMember.group_id,
            PartPoolPricePolicy.valid_to.is_(None)))
        .where(PartPoolMember.group_id.in_(group_ids), _purchase_priced_condition())
    )
    stmt = _purchase_type_filter(stmt, purchase_type)
    stmt = _window(stmt, FPurchaseOrder, lower, upper).group_by(PartPoolMember.group_id)
    return {r.key: _stats(r) for r in db.execute(stmt)}


def _sales_group_stats(db: Session, group_ids: list[int], lower: date | None,
                       upper: date) -> dict[int, dict]:
    if not group_ids:
        return {}
    ex = sale_ex_unit()
    stmt = (
        select(PartPoolMember.group_id.label("key"),
               func.sum(FSalesLine.revenue_amount).label("amount"),
               func.sum(FSalesLine.qty).label("qty"),
               func.percentile_cont(0.5).within_group(ex).label("median"),
               func.min(ex).label("minimum"), func.max(ex).label("maximum"),
               func.array_agg(aggregate_order_by(ex, FSalesOrder.order_date.desc(),
                                                 FSalesLine.id.desc()))[1].label("latest"),
               func.max(FSalesOrder.order_date).label("latest_date"),
               func.count(func.distinct(FSalesOrder.id)).label("orders"),
               func.count(FSalesLine.id).label("lines"),
               func.count(FSalesLine.id).filter(
                   ex < PartPoolPricePolicy.sales_floor_ex_tax
               ).label("violations"))
        .select_from(PartPoolMember)
        .join(FSalesLine, FSalesLine.part_id == PartPoolMember.part_id)
        .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
        .outerjoin(PartPoolPricePolicy, and_(
            PartPoolPricePolicy.group_id == PartPoolMember.group_id,
            PartPoolPricePolicy.valid_to.is_(None)))
        .where(PartPoolMember.group_id.in_(group_ids),
               _sales_priced_condition())
    )
    stmt = _window(stmt, FSalesOrder, lower, upper).group_by(PartPoolMember.group_id)
    return {r.key: _stats(r) for r in db.execute(stmt)}


def _purchase_part_stats(db: Session, part_ids: list[int], lower: date | None,
                         upper: date, purchase_type: str | None = None) -> dict[int, dict]:
    if not part_ids:
        return {}
    ex = purchase_ex_unit()
    stmt = (
        select(FPurchaseLine.part_id.label("key"),
               func.sum(purchase_ex_tax_expr()).label("amount"),
               func.sum(FPurchaseLine.qty).label("qty"),
               func.percentile_cont(0.5).within_group(ex).label("median"),
               func.min(ex).label("minimum"), func.max(ex).label("maximum"),
               func.array_agg(aggregate_order_by(ex, FPurchaseOrder.order_date.desc(),
                                                 FPurchaseLine.id.desc()))[1].label("latest"),
               func.max(FPurchaseOrder.order_date).label("latest_date"),
               func.count(func.distinct(FPurchaseOrder.id)).label("orders"),
               func.count(FPurchaseLine.id).label("lines"))
        .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
        .where(FPurchaseLine.part_id.in_(part_ids), _purchase_priced_condition())
    )
    stmt = _purchase_type_filter(stmt, purchase_type)
    stmt = _window(stmt, FPurchaseOrder, lower, upper).group_by(FPurchaseLine.part_id)
    return {r.key: _stats(r) for r in db.execute(stmt)}


def _sales_part_stats(db: Session, part_ids: list[int], lower: date | None,
                      upper: date) -> dict[int, dict]:
    if not part_ids:
        return {}
    ex = sale_ex_unit()
    stmt = (
        select(FSalesLine.part_id.label("key"),
               func.sum(FSalesLine.revenue_amount).label("amount"),
               func.sum(FSalesLine.qty).label("qty"),
               func.percentile_cont(0.5).within_group(ex).label("median"),
               func.min(ex).label("minimum"), func.max(ex).label("maximum"),
               func.array_agg(aggregate_order_by(ex, FSalesOrder.order_date.desc(),
                                                 FSalesLine.id.desc()))[1].label("latest"),
               func.max(FSalesOrder.order_date).label("latest_date"),
               func.count(func.distinct(FSalesOrder.id)).label("orders"),
               func.count(FSalesLine.id).label("lines"))
        .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
        .where(FSalesLine.part_id.in_(part_ids),
               _sales_priced_condition())
    )
    stmt = _window(stmt, FSalesOrder, lower, upper).group_by(FSalesLine.part_id)
    return {r.key: _stats(r) for r in db.execute(stmt)}


def _current_policies(db: Session, group_ids: list[int]) -> dict[int, PartPoolPricePolicy]:
    if not group_ids:
        return {}
    rows = db.execute(select(PartPoolPricePolicy).where(
        PartPoolPricePolicy.group_id.in_(group_ids), PartPoolPricePolicy.valid_to.is_(None)
    )).scalars()
    return {r.group_id: r for r in rows}


def _excluded_counts(db: Session, group_ids: list[int], lower: date | None,
                     upper: date, today: date,
                     purchase_type: str | None = None) -> dict[int, dict]:
    """按池返回四个可直接验证的真实排除计数，固定两条 SQL。

    anomaly_flags 同时承载经营异常，不能冒充“数据疑点”；人工确认无效的闭环也尚未
    建立。因此本接口在 DEV-05 口径落地前不声明这两个计数，更不能用假 0 代替。
    """
    if not group_ids:
        return {}
    empty = {"inactive_orders": 0, "nonpositive_price": 0,
             "nonpositive_qty": 0, "future_orders": 0}
    out = {gid: dict(empty) for gid in group_ids}

    def add_rows(line_model, order_model, source_type: str | None = None):
        date_window = order_model.order_date <= upper
        if lower is not None:
            date_window = and_(date_window, order_model.order_date >= lower)
        active = order_model.data_status == config.ACTIVE_STATUS
        stmt = (
            select(
                PartPoolMember.group_id,
                func.count(func.distinct(order_model.id)).filter(
                    and_(date_window, order_model.data_status.is_distinct_from(config.ACTIVE_STATUS))
                ).label("inactive"),
                func.count(line_model.id).filter(and_(
                    date_window, active,
                    or_(line_model.unit_price.is_(None), line_model.unit_price <= 0)
                )).label("bad_price"),
                func.count(line_model.id).filter(and_(
                    date_window, active, or_(line_model.qty.is_(None), line_model.qty <= 0)
                )).label("bad_qty"),
                func.count(func.distinct(order_model.id)).filter(and_(
                    active, order_model.order_date > today
                )).label("future"),
            )
            .select_from(PartPoolMember)
            .join(line_model, line_model.part_id == PartPoolMember.part_id)
            .join(order_model, order_model.id == line_model.order_id)
            .where(PartPoolMember.group_id.in_(group_ids))
            .group_by(PartPoolMember.group_id)
        )
        if source_type and source_type.strip():
            stmt = stmt.where(order_model.source_type == source_type)
        for row in db.execute(stmt):
            target = out[row.group_id]
            target["inactive_orders"] += int(row.inactive or 0)
            target["nonpositive_price"] += int(row.bad_price or 0)
            target["nonpositive_qty"] += int(row.bad_qty or 0)
            target["future_orders"] += int(row.future or 0)

    add_rows(FPurchaseLine, FPurchaseOrder, purchase_type)
    add_rows(FSalesLine, FSalesOrder)
    return out


def _constraint(policy: PartPoolPricePolicy | None, side: str) -> dict:
    if side == "purchase":
        value = policy.purchase_ceiling_ex_tax if policy else None
        basis = policy.purchase_input_basis if policy else None
    else:
        value = policy.sales_floor_ex_tax if policy else None
        basis = policy.sales_input_basis if policy else None
    return {
        "status": "set" if value is not None else "unset", "value": _r(value),
        "changed_by": policy.changed_by if policy else None,
        "changed_at": policy.valid_from.isoformat() if policy and policy.valid_from else None,
        "input_basis": basis,
    }


def _reference_side(side: str, pool_stats: dict | None, part_stats: dict | None,
                    policy: PartPoolPricePolicy | None) -> dict:
    constraint = _constraint(policy, side)
    if pool_stats is not None and constraint["status"] == "unset":
        pool_stats = {**pool_stats, "violation_count": None}
    pool_avg = (pool_stats or {}).get("weighted_avg")
    part_avg = (part_stats or {}).get("weighted_avg")
    limit = constraint["value"]
    delta_pool = _r(part_avg - pool_avg) if part_avg is not None and pool_avg is not None else None
    delta_limit = _r(part_avg - limit) if part_avg is not None and limit is not None else None
    if constraint["status"] == "unset":
        relation = "unset"
    elif part_avg is None:
        relation = None
    elif part_avg > limit:
        relation = "above"
    elif part_avg < limit:
        relation = "below"
    else:
        relation = "equal"
    return {
        "restricted": False, "pool_stats": pool_stats, "part_stats": part_stats,
        "constraint": constraint, "delta_to_pool_avg": delta_pool,
        "delta_to_constraint": delta_limit, "relation_to_constraint": relation,
    }


def _not_in_pool_side() -> dict:
    """未入有效池不是“无权限”或“无样本”，必须返回无价格的中性状态。"""
    return {
        "status": "not_in_pool", "restricted": False,
        "pool_stats": None, "part_stats": None,
        "constraint": {"status": "unset", "value": None, "changed_by": None,
                       "changed_at": None, "input_basis": None},
        "delta_to_pool_avg": None, "delta_to_constraint": None,
        "relation_to_constraint": None,
    }


def _compat_pool_metrics(stats: dict | None) -> dict:
    """把新统计契约投影为旧 PoolDetail 的展示形状。

    新全员详情页暂时复用已有图表组件；这里让它消费同一个 PoolPriceAnalysis 结果，
    避免页面暗中退回利润成本池的采购类型口径。
    """
    stats = stats or _EMPTY_STATS
    return {
        "total_amount": stats.get("total_amount"),
        "total_quantity": stats.get("total_qty"),
        "weighted_avg_unit_price": stats.get("weighted_avg"),
        "order_count": stats.get("order_count", 0),
        "latest_date": stats.get("latest_date"),
    }


def _compat_member_metrics(stats: dict | None, pool_avg: float | None,
                           limit: float | None) -> dict:
    result = _compat_pool_metrics(stats)
    average = result["weighted_avg_unit_price"]
    result["pool_avg_delta"] = (
        _r(average - pool_avg) if average is not None and pool_avg is not None else None)
    result["pool_avg_delta_pct"] = (
        round((average - pool_avg) / pool_avg, 4)
        if average is not None and pool_avg else None)
    result["manual_limit_delta"] = (
        _r(average - limit) if average is not None and limit is not None else None)
    result["manual_limit_delta_pct"] = (
        round((average - limit) / limit, 4)
        if average is not None and limit else None)
    return result


def references(db: Session, part_ids: list[int], *, range_: str | None = None,
               date_from: date | None = None, date_to: date | None = None,
               as_of: date | None = None,
               purchase_type: str | None = None) -> list[dict]:
    """按输入顺序返回参考卡；统计 SQL 数量与 part_id 数量无关。"""
    unique = list(dict.fromkeys(part_ids))
    if not unique:
        return []
    lower, upper, today, token = resolve_window(range_, date_from, date_to, as_of)
    active_membership = (
        select(PartPoolMember.part_id.label("part_id"), PartPool.group_id.label("group_id"),
               PartPool.name.label("pool_name"), PartPool.member_count.label("member_count"))
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .where(PartPool.status == "active").subquery()
    )
    rows = db.execute(
        select(DimPart.id, DimPart.pn_std, active_membership.c.group_id,
               active_membership.c.pool_name, active_membership.c.member_count)
        .outerjoin(active_membership, active_membership.c.part_id == DimPart.id)
        .where(DimPart.id.in_(unique))
    ).all()
    by_part = {r.id: r for r in rows}
    group_ids = list({r.group_id for r in rows if r.group_id is not None})
    pooled_part_ids = [r.id for r in rows if r.group_id is not None]
    p_group = _purchase_group_stats(db, group_ids, lower, upper, purchase_type)
    s_group = _sales_group_stats(db, group_ids, lower, upper)
    p_part = _purchase_part_stats(db, pooled_part_ids, lower, upper, purchase_type)
    s_part = _sales_part_stats(db, pooled_part_ids, lower, upper)
    policies = _current_policies(db, group_ids)
    excluded = _excluded_counts(db, group_ids, lower, upper, today, purchase_type)
    out = []
    for part_id in unique:
        row = by_part.get(part_id)
        if row is None:
            continue
        gid = row.group_id
        pool_info = ({"group_id": gid, "name": row.pool_name,
                      "member_count": row.member_count} if gid is not None else None)
        if gid is None:
            out.append({
                "status": "not_in_pool", "part_id": part_id, "pn_std": row.pn_std,
                "pool": None,
                "window": {"range": token,
                           "date_from": lower.isoformat() if lower else None,
                           "date_to": upper.isoformat(), "as_of": today.isoformat()},
                "basis": "ex_tax", "purchase_type": purchase_type,
                "purchase_reference": _not_in_pool_side(),
                "sales_reference": _not_in_pool_side(),
                "excluded": None,
            })
            continue
        policy = policies.get(gid)
        out.append({
            "status": "active_pool", "part_id": part_id, "pn_std": row.pn_std,
            "pool": pool_info,
            "window": {"range": token,
                       "date_from": lower.isoformat() if lower else None,
                       "date_to": upper.isoformat(), "as_of": today.isoformat()},
            "basis": "ex_tax", "purchase_type": purchase_type,
            "purchase_reference": _reference_side(
                "purchase", p_group.get(gid), p_part.get(part_id), policy),
            "sales_reference": _reference_side(
                "sales", s_group.get(gid), s_part.get(part_id), policy),
            "excluded": excluded.get(gid, {
                "inactive_orders": 0, "nonpositive_price": 0,
                "nonpositive_qty": 0, "future_orders": 0}),
        })
    return out


def reference(db: Session, part_id: int, **kwargs) -> dict | None:
    items = references(db, [part_id], **kwargs)
    return items[0] if items else None


def list_pools(db: Session, *, range_: str | None = None,
               date_from: date | None = None, date_to: date | None = None,
               q: str | None = None, pn: str | None = None,
               purchase_type: str | None = None,
               page: int = 1, page_size: int = 20,
               requested_sort: str = "member_count",
               effective_sort: str = "member_count",
               ranking_restricted: bool = False) -> dict:
    """全员分析池清单；池名与成员搜索共享同一查询，统计按当页池批量计算。"""
    lower, upper, today, token = resolve_window(range_, date_from, date_to)
    stmt = select(PartPool).where(PartPool.status == "active")
    if q and q.strip():
        term = f"%{q.strip()}%"
        member_match = (
            select(1).select_from(PartPoolMember)
            .join(DimPart, DimPart.id == PartPoolMember.part_id)
            .where(PartPoolMember.group_id == PartPool.group_id,
                   or_(DimPart.pn_std.ilike(term), DimPart.description.ilike(term),
                       DimPart.brand.ilike(term))).exists()
        )
        stmt = stmt.where(or_(PartPool.name.ilike(term), PartPool.description.ilike(term),
                              member_match))
    if pn and pn.strip():
        pn_term = pn.strip()
        member_pn = (
            select(1).select_from(PartPoolMember)
            .join(DimPart, DimPart.id == PartPoolMember.part_id)
            .where(PartPoolMember.group_id == PartPool.group_id,
                   func.upper(DimPart.pn_std) == pn_term.upper()).exists()
        )
        stmt = stmt.where(member_pn)
    pools = db.execute(stmt.order_by(PartPool.group_id.asc())).scalars().all()
    total = len(pools)
    gids = [p.group_id for p in pools]
    purchase_stats = _purchase_group_stats(db, gids, lower, upper, purchase_type)
    sales_stats = _sales_group_stats(db, gids, lower, upper)
    policies = _current_policies(db, gids)
    items = []
    for row in pools:
        purchase = _reference_side(
            "purchase", purchase_stats.get(row.group_id), None, policies.get(row.group_id))
        sales = _reference_side(
            "sales", sales_stats.get(row.group_id), None, policies.get(row.group_id))
        items.append({
            "group_id": row.group_id, "name": row.name,
            "description": row.description, "member_count": row.member_count,
            "purchase_reference": purchase, "sales_reference": sales,
        })
    metric_sorts = {
        "purchase_average": lambda item: item["purchase_reference"]["pool_stats"] and
        item["purchase_reference"]["pool_stats"]["weighted_avg"],
        "purchase_total": lambda item: item["purchase_reference"]["pool_stats"] and
        item["purchase_reference"]["pool_stats"]["total_amount"],
        "sales_average": lambda item: item["sales_reference"]["pool_stats"] and
        item["sales_reference"]["pool_stats"]["weighted_avg"],
        "sales_total": lambda item: item["sales_reference"]["pool_stats"] and
        item["sales_reference"]["pool_stats"]["total_amount"],
    }
    if effective_sort in metric_sorts:
        getter = metric_sorts[effective_sort]
        items.sort(key=lambda item: (getter(item) is None,
                                     -(getter(item) or 0), item["group_id"]))
    else:
        items.sort(key=lambda item: (-item["member_count"], item["group_id"]))
    items = items[(page - 1) * page_size: page * page_size]
    return {
        "total": total, "page": page, "page_size": page_size,
        "window": {"range": token, "date_from": lower.isoformat() if lower else None,
                   "date_to": upper.isoformat(), "as_of": today.isoformat()},
        "basis": "ex_tax", "purchase_type": purchase_type, "sort": requested_sort,
        "effective_sort": effective_sort, "ranking_restricted": ranking_restricted,
        "items": items,
    }


def _order_has_active_pool_member(line_model, order_id: int):
    return exists(
        select(1)
        .select_from(line_model)
        .join(PartPoolMember, PartPoolMember.part_id == line_model.part_id)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .where(line_model.order_id == order_id, PartPool.status == "active")
    )


def order_detail(db: Session, side: str, order_id: int) -> dict | None:
    """按稳定订单主键返回完整订单；不使用可能重复或被改写的显示单号定位。"""
    if side == "purchase":
        order = db.execute(
            select(
                FPurchaseOrder.id.label("order_id"), FPurchaseOrder.order_no,
                FPurchaseOrder.order_date, FPurchaseOrder.purchaser,
                DimSupplier.name_normalized.label("supplier"),
                FPurchaseOrder.source_type, FPurchaseOrder.source_type_raw,
                FPurchaseOrder.linked_sales_order_no,
                FPurchaseOrder.linked_maintenance_order_no,
                FPurchaseOrder.data_status, FPurchaseOrder.is_tax_inclusive,
                FPurchaseOrder.tax_rate,
                FPurchaseOrder.amount_ex_tax.label("purchase_order_amount_ex_tax"),
                FPurchaseOrder.amount_inc_tax.label("purchase_order_amount_inc_tax"),
                FPurchaseOrder.tax_amount.label("purchase_tax_amount"),
            )
            .outerjoin(DimSupplier, DimSupplier.id == FPurchaseOrder.supplier_id)
            .where(FPurchaseOrder.id == order_id,
                   FPurchaseOrder.data_status == config.ACTIVE_STATUS,
                   _order_has_active_pool_member(FPurchaseLine, order_id))
        ).mappings().one_or_none()
        if order is None:
            return None
        rows = db.execute(
            select(
                FPurchaseLine.id.label("line_id"), FPurchaseLine.part_id,
                DimPart.pn_std, FPurchaseLine.description, FPurchaseLine.brand,
                FPurchaseLine.line_no, FPurchaseLine.qty.label("quantity"), FPurchaseLine.unit,
                FPurchaseLine.unit_price.label("purchase_original_unit_price"),
                purchase_ex_unit().label("purchase_unit_price_ex_tax"),
                purchase_ex_tax_expr().label("purchase_line_value_ex_tax"),
                FPurchaseLine.anomaly_flags,
            )
            .join(DimPart, DimPart.id == FPurchaseLine.part_id)
            .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
            .where(FPurchaseLine.order_id == order_id)
            .order_by(FPurchaseLine.line_no.asc().nullslast(), FPurchaseLine.id.asc())
        ).mappings().all()
        price_keys = ("purchase_original_unit_price", "purchase_unit_price_ex_tax",
                      "purchase_line_value_ex_tax")
    elif side == "sales":
        order = db.execute(
            select(
                FSalesOrder.id.label("order_id"), FSalesOrder.order_no,
                FSalesOrder.order_date, FSalesOrder.salesperson,
                DimCustomer.name_normalized.label("customer"),
                FSalesOrder.business_type, FSalesOrder.warehouse,
                FSalesOrder.data_status, FSalesOrder.tax_rate,
                FSalesOrder.amount_ex_tax.label("sale_order_amount_ex_tax"),
            )
            .outerjoin(DimCustomer, DimCustomer.id == FSalesOrder.customer_id)
            .where(FSalesOrder.id == order_id,
                   FSalesOrder.data_status == config.ACTIVE_STATUS,
                   _order_has_active_pool_member(FSalesLine, order_id))
        ).mappings().one_or_none()
        if order is None:
            return None
        rows = db.execute(
            select(
                FSalesLine.id.label("line_id"), FSalesLine.part_id,
                DimPart.pn_std, FSalesLine.description, FSalesLine.brand,
                FSalesLine.line_no, FSalesLine.qty.label("quantity"), FSalesLine.unit,
                FSalesLine.unit_price.label("sale_original_unit_price_inc_tax"),
                sale_ex_unit().label("sale_unit_price_ex_tax"),
                FSalesLine.revenue_amount.label("sale_line_value_ex_tax"),
                FSalesLine.counts_revenue, FSalesLine.anomaly_flags,
            )
            .join(DimPart, DimPart.id == FSalesLine.part_id)
            .where(FSalesLine.order_id == order_id)
            .order_by(FSalesLine.line_no.asc().nullslast(), FSalesLine.id.asc())
        ).mappings().all()
        price_keys = ("sale_original_unit_price_inc_tax", "sale_unit_price_ex_tax",
                      "sale_line_value_ex_tax")
    else:
        raise ValueError(f"unsupported order side: {side}")

    pool_map = pool_metrics.active_pool_map(db, [row["part_id"] for row in rows])
    items = []
    for source in rows:
        row = dict(source)
        for key in price_keys:
            row[key] = _r(row.get(key))
        row["quantity"] = _r(row.get("quantity"), 3)
        identity = pool_map.get(row["part_id"])
        row["pool_group_id"] = identity["group_id"] if identity else None
        row["pool_name"] = identity["pool_name"] if identity else None
        items.append(row)
    header = dict(order)
    if header.get("order_date") is not None:
        header["order_date"] = header["order_date"].isoformat()
    for key, value in list(header.items()):
        if key.endswith("_amount_ex_tax") or key.endswith("_amount_inc_tax") or key.endswith("_tax_amount"):
            header[key] = _r(value)
        elif key == "tax_rate":
            header[key] = _r(value, 4)
    return {
        "side": side, "order": header, "items": items,
        "price_restricted": False, "supplier_restricted": False,
        "customer_restricted": False,
    }


def apply_visibility(data, ctx: security.UserContext):
    """池价格接口的结构性权限净化。

    池历史采购/销售价是价格纪律事实，不是利润成本，故不读取 ``data_purchase_cost``。
    ``data_pool_price_governance`` 是两侧价格、约束、差额、越线和价格排序的唯一开关；
    供应商/客户仍分别按其 data 权限隐藏，经办人和订单号始终保留。
    """
    price_restricted = security.is_field_hidden(ctx, "purchase_ceiling_ex_tax")
    supplier_restricted = security.is_field_hidden(ctx, "supplier")
    customer_restricted = security.is_field_hidden(ctx, "customer")

    def scrub_price_side(side: dict | None):
        if isinstance(side, dict):
            side["restricted"] = True
            side["pool_stats"] = side["part_stats"] = None
            side["delta_to_pool_avg"] = side["delta_to_constraint"] = None
            side["relation_to_constraint"] = None
            side["constraint"] = {
                "status": "restricted", "value": None, "changed_by": None,
                "changed_at": None, "input_basis": None,
            }

    def rows(node: dict, block_key: str):
        block = node.get(block_key)
        return (block or {}).get("items") or [] if isinstance(block, dict) else []

    nodes = list(data) if isinstance(data, list) else [data]
    # 池清单是分页 envelope；每个 item 也必须经过同一结构化净化，不能只降级排序后
    # 把真实价格继续留在响应体里。
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        nodes.extend(item for item in data["items"] if isinstance(item, dict))
    for node in nodes:
        if not isinstance(node, dict):
            continue
        order_side = node.get("side") if isinstance(node.get("order"), dict) else None
        if order_side in {"purchase", "sales"}:
            header = node["order"]
            node["price_restricted"] = price_restricted
            node["supplier_restricted"] = supplier_restricted
            node["customer_restricted"] = customer_restricted
            if price_restricted:
                header_price_keys = (
                    ("purchase_order_amount_ex_tax", "purchase_order_amount_inc_tax",
                     "purchase_tax_amount") if order_side == "purchase"
                    else ("sale_order_amount_ex_tax",)
                )
                line_price_keys = (
                    ("purchase_original_unit_price", "purchase_unit_price_ex_tax",
                     "purchase_line_value_ex_tax") if order_side == "purchase"
                    else ("sale_original_unit_price_inc_tax", "sale_unit_price_ex_tax",
                          "sale_line_value_ex_tax")
                )
                for key in header_price_keys:
                    if key in header:
                        header[key] = None
                for row in node.get("items") or []:
                    for key in line_price_keys:
                        if key in row:
                            row[key] = None
            if supplier_restricted and "supplier" in header:
                header["supplier"] = None
            if customer_restricted and "customer" in header:
                header["customer"] = None
        if price_restricted and node.get("status") != "not_in_pool":
            scrub_price_side(node.get("purchase_reference"))
            scrub_price_side(node.get("sales_reference"))
        for member in node.get("members") or []:
            if price_restricted:
                scrub_price_side(member.get("purchase_reference"))
                scrub_price_side(member.get("sales_reference"))
                for key in ("purchase_metrics", "sales_metrics"):
                    if key in member:
                        member[key] = None
        if price_restricted and node.get("status") != "not_in_pool":
            for key in ("purchase_metrics", "sales_metrics"):
                if key in node:
                    node[key] = None
            for block_key in ("purchase_transactions", "purchase_orders"):
                for row in rows(node, block_key):
                    row["purchase_unit_price_ex_tax"] = None
                    row["purchase_line_value_ex_tax"] = None
            for block_key in ("sales_transactions", "sales_orders"):
                for row in rows(node, block_key):
                    row["sale_unit_price_ex_tax"] = None
                    row["sale_line_value_ex_tax"] = None
            for key in ("max_purchase_price", "min_sale_price",
                        "purchase_violation_count", "sale_violation_count"):
                if key in node:
                    node[key] = None
            node["manual_reference_restricted"] = True
            demand = node.get("demand")
            if isinstance(demand, dict):
                demand["total_revenue_ex_tax"] = None
        if supplier_restricted:
            for block_key in ("purchase_transactions", "purchase_orders"):
                for row in rows(node, block_key):
                    if "supplier" in row:
                        row["supplier"] = None
        if customer_restricted:
            for block_key in ("sales_transactions", "sales_orders"):
                for row in rows(node, block_key):
                    if "customer" in row:
                        row["customer"] = None
        if customer_restricted and "customer_cross_brand" in node:
            node["customer_cross_brand"] = {"restricted": True, "customers": []}
    return data


def pool_detail(db: Session, group_id: int, date_from: date | None = None,
                date_to: date | None = None, *, range_: str | None = None,
                purchase_type: str | None = None,
                purchase_page: int = 1, sales_page: int = 1,
                page_size: int = 30) -> dict | None:
    """复用现有丰富池详情，映射出新全员契约；``user_ctx=None`` 是显式池分析上下文，
    仅绕开旧销售整段隐藏，真实权限在 API 边界结构化净化。"""
    lower, upper, today, token = resolve_window(range_, date_from, date_to)
    legacy = pool.analyze(db, group_id, lower, upper, user_ctx=None, with_v2=True,
                          purchase_page=purchase_page, sales_page=sales_page,
                          orders_page_size=page_size,
                          purchase_source_type=purchase_type)
    if legacy is None:
        return None
    member_ids = [m["part_id"] for m in legacy["members"]]
    reference_window = ({"date_from": lower, "date_to": upper}
                        if token == "custom" else {})
    refs = references(db, member_ids, range_=token, as_of=today,
                      purchase_type=purchase_type, **reference_window)
    ref_by_part = {r["part_id"]: r for r in refs}
    members = []
    for legacy_member in legacy["members"]:
        mapped = deepcopy(ref_by_part[legacy_member["part_id"]])
        members.append({**mapped, "brand": legacy_member.get("brand"),
                        "description": legacy_member.get("description")})
    first = refs[0] if refs else None
    pool_purchase = deepcopy((first or {}).get("purchase_reference")) or _reference_side(
        "purchase", None, None, None)
    pool_sales = deepcopy((first or {}).get("sales_reference")) or _reference_side(
        "sales", None, None, None)
    # 池级条目没有“当前 PN”，只保留池统计/约束；成员差额在 members 内。
    for side in (pool_purchase, pool_sales):
        side["part_stats"] = None
        side["delta_to_pool_avg"] = None
        side["delta_to_constraint"] = None
        side["relation_to_constraint"] = side["constraint"]["status"] == "unset" and "unset" or None
    purchase_pool_stats = pool_purchase.get("pool_stats")
    sales_pool_stats = pool_sales.get("pool_stats")
    purchase_pool_avg = (purchase_pool_stats or {}).get("weighted_avg")
    sales_pool_avg = (sales_pool_stats or {}).get("weighted_avg")
    purchase_limit = pool_purchase["constraint"].get("value")
    sales_limit = pool_sales["constraint"].get("value")
    for member in members:
        member["purchase_metrics"] = _compat_member_metrics(
            member["purchase_reference"].get("part_stats"), purchase_pool_avg, purchase_limit)
        member["sales_metrics"] = _compat_member_metrics(
            member["sales_reference"].get("part_stats"), sales_pool_avg, sales_limit)
    purchase_transactions = {
        **legacy["purchase_orders"],
        "items": [{
            **{k: v for k, v in row.items() if k not in {"unit_price_ex_tax", "amount"}},
            "purchase_unit_price_ex_tax": row.get("unit_price_ex_tax"),
            "purchase_line_value_ex_tax": row.get("amount"),
        } for row in legacy["purchase_orders"]["items"]],
    }
    sales_transactions = {
        **legacy["sales_orders"],
        "restricted": False,
        "items": [{
            **{k: v for k, v in row.items() if k not in {"unit_price_ex_tax", "amount"}},
            "sale_unit_price_ex_tax": row.get("unit_price_ex_tax"),
            "sale_line_value_ex_tax": row.get("amount"),
        } for row in legacy["sales_orders"]["items"]],
    }
    return {
        "group_id": group_id, "name": legacy.get("name"),
        "description": legacy.get("description"), "member_count": legacy.get("member_count", 0),
        "needs_calibration": bool(legacy.get("needs_calibration")),
        "oversized": bool(legacy.get("oversized")),
        "window": {"range": token, "date_from": lower.isoformat() if lower else None,
                   "date_to": upper.isoformat(), "as_of": today.isoformat()},
        "basis": "ex_tax", "purchase_type": purchase_type,
        "purchase_reference": pool_purchase,
        "sales_reference": pool_sales, "members": members,
        "excluded": deepcopy((first or {}).get("excluded")) or {
            "inactive_orders": 0, "nonpositive_price": 0, "nonpositive_qty": 0,
            "future_orders": 0,
        },
        # 兼容既有详情图表，但数值来自本模块的全采购类型统一口径。
        "purchase_metrics": _compat_pool_metrics(purchase_pool_stats),
        "sales_metrics": _compat_pool_metrics(sales_pool_stats),
        "max_purchase_price": purchase_limit,
        "min_sale_price": sales_limit,
        "purchase_violation_count": (purchase_pool_stats or {}).get("violation_count"),
        "sale_violation_count": (sales_pool_stats or {}).get("violation_count"),
        "manual_reference_restricted": False,
        # 新契约别名；旧 purchase_orders/sales_orders 继续保留。
        "purchase_orders": purchase_transactions,
        "sales_orders": sales_transactions,
        "purchase_transactions": purchase_transactions,
        "sales_transactions": sales_transactions,
    }
