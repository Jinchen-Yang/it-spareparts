"""老板早会价格纪律摘要（DEV-06）。

只对已经发生、已生效的订单做历史事实汇总。结果用于发现和复盘，不提交报价、不审批、
不拦截业务，也不评价员工能力或动机。人工约束价按“当前约束回看所选窗口”解释。
"""
from datetime import date

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember, PartPoolPricePolicy
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services import pool_price_analysis
from app.services.pricing import purchase_ex_unit, sale_ex_unit
from app.services.query_filters import active_orders


def _r(value, digits: int = 2):
    return round(float(value), digits) if value is not None else None


def _window(stmt, order_model, lower: date | None, upper: date):
    stmt = active_orders(stmt, order_model)
    if lower is not None:
        stmt = stmt.where(order_model.order_date >= lower)
    return stmt.where(order_model.order_date <= upper)


def _violation_rows(lower: date | None, upper: date):
    """构造双侧越线事实集；约束价为空时自然不入集，严格等于也不算越线。"""
    purchase_unit = purchase_ex_unit()
    purchase_gap = purchase_unit - PartPoolPricePolicy.purchase_ceiling_ex_tax
    purchases = (
        select(
            literal("purchase").label("side"),
            FPurchaseOrder.id.label("order_id"), FPurchaseOrder.order_no,
            FPurchaseOrder.order_date, FPurchaseLine.id.label("line_id"),
            FPurchaseLine.part_id, DimPart.pn_std,
            PartPool.group_id.label("pool_group_id"), PartPool.name.label("pool_name"),
            FPurchaseOrder.purchaser.label("person"), FPurchaseLine.qty.label("quantity"),
            purchase_unit.label("actual_unit_ex_tax"),
            PartPoolPricePolicy.purchase_ceiling_ex_tax.label("manual_limit_ex_tax"),
            purchase_gap.label("unit_gap"),
            (purchase_gap * FPurchaseLine.qty).label("total_gap"),
        )
        .select_from(FPurchaseLine)
        .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
        .join(DimPart, DimPart.id == FPurchaseLine.part_id)
        .join(PartPoolMember, PartPoolMember.part_id == FPurchaseLine.part_id)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .join(PartPoolPricePolicy, PartPoolPricePolicy.group_id == PartPool.group_id)
        .where(
            PartPool.status == "active", PartPoolPricePolicy.valid_to.is_(None),
            PartPoolPricePolicy.purchase_ceiling_ex_tax.is_not(None),
            pool_price_analysis._purchase_priced_condition(),
            purchase_unit > PartPoolPricePolicy.purchase_ceiling_ex_tax,
        )
    )
    purchases = _window(purchases, FPurchaseOrder, lower, upper)

    sales_unit = sale_ex_unit()
    sales_gap = PartPoolPricePolicy.sales_floor_ex_tax - sales_unit
    sales = (
        select(
            literal("sales").label("side"),
            FSalesOrder.id.label("order_id"), FSalesOrder.order_no,
            FSalesOrder.order_date, FSalesLine.id.label("line_id"),
            FSalesLine.part_id, DimPart.pn_std,
            PartPool.group_id.label("pool_group_id"), PartPool.name.label("pool_name"),
            FSalesOrder.salesperson.label("person"), FSalesLine.qty.label("quantity"),
            sales_unit.label("actual_unit_ex_tax"),
            PartPoolPricePolicy.sales_floor_ex_tax.label("manual_limit_ex_tax"),
            sales_gap.label("unit_gap"),
            (sales_gap * FSalesLine.qty).label("total_gap"),
        )
        .select_from(FSalesLine)
        .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
        .join(DimPart, DimPart.id == FSalesLine.part_id)
        .join(PartPoolMember, PartPoolMember.part_id == FSalesLine.part_id)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .join(PartPoolPricePolicy, PartPoolPricePolicy.group_id == PartPool.group_id)
        .where(
            PartPool.status == "active", PartPoolPricePolicy.valid_to.is_(None),
            PartPoolPricePolicy.sales_floor_ex_tax.is_not(None),
            pool_price_analysis._sales_priced_condition(),
            sales_unit < PartPoolPricePolicy.sales_floor_ex_tax,
        )
    )
    sales = _window(sales, FSalesOrder, lower, upper)
    return union_all(purchases, sales).subquery("price_discipline_violation")


def _empty_side() -> dict:
    return {"violation_line_count": 0, "order_count": 0, "pool_count": 0,
            "total_gap": 0.0}


def _missing_constraints(db: Session) -> dict:
    row = db.execute(
        select(
            func.count(PartPool.group_id).label("active_pools"),
            func.count(PartPool.group_id).filter(
                PartPoolPricePolicy.purchase_ceiling_ex_tax.is_(None)
            ).label("purchase_unset"),
            func.count(PartPool.group_id).filter(
                PartPoolPricePolicy.sales_floor_ex_tax.is_(None)
            ).label("sales_unset"),
            func.count(PartPool.group_id).filter(
                PartPoolPricePolicy.purchase_ceiling_ex_tax.is_(None),
                PartPoolPricePolicy.sales_floor_ex_tax.is_(None),
            ).label("both_unset"),
        )
        .select_from(PartPool)
        .outerjoin(
            PartPoolPricePolicy,
            (PartPoolPricePolicy.group_id == PartPool.group_id)
            & PartPoolPricePolicy.valid_to.is_(None),
        )
        .where(PartPool.status == "active")
    ).one()
    return {
        "active_pool_count": int(row.active_pools or 0),
        "purchase_ceiling_unset_count": int(row.purchase_unset or 0),
        "sales_floor_unset_count": int(row.sales_unset or 0),
        "both_unset_count": int(row.both_unset or 0),
    }


def _window_payload(lower: date | None, upper: date, today: date, token: str) -> dict:
    return {
        "range": token, "date_from": lower.isoformat() if lower else None,
        "date_to": upper.isoformat(), "as_of": today.isoformat(),
    }


def restricted_summary(*, date_from: date | None = None, date_to: date | None = None,
                       as_of: date | None = None) -> dict:
    """治理权限关闭时的零查询稳定形状；连次数和人员列表也不暴露。"""
    lower, upper, today, token = pool_price_analysis.resolve_window(
        "90d", date_from, date_to, as_of)
    return {
        "restricted": True, "basis": "ex_tax",
        "window": _window_payload(lower, upper, today, token),
        "purchase": None, "sales": None, "most_severe_pool": None,
        "handler_summary": {"purchase": [], "sales": []},
        "recent_violations": [], "missing_constraints": None,
    }


def summary(db: Session, *, date_from: date | None = None, date_to: date | None = None,
            as_of: date | None = None) -> dict:
    """返回所选窗口的越线事实摘要；所有金额为未税，严格越线且差额不为负。"""
    lower, upper, today, token = pool_price_analysis.resolve_window(
        "90d", date_from, date_to, as_of)
    violations = _violation_rows(lower, upper)

    side_totals = {"purchase": _empty_side(), "sales": _empty_side()}
    for row in db.execute(
        select(
            violations.c.side,
            func.count().label("lines"),
            func.count(func.distinct(violations.c.order_id)).label("orders"),
            func.count(func.distinct(violations.c.pool_group_id)).label("pools"),
            func.sum(violations.c.total_gap).label("total_gap"),
        ).group_by(violations.c.side)
    ):
        side_totals[row.side] = {
            "violation_line_count": int(row.lines or 0),
            "order_count": int(row.orders or 0), "pool_count": int(row.pools or 0),
            "total_gap": _r(row.total_gap) or 0.0,
        }

    purchase_gap = func.coalesce(func.sum(violations.c.total_gap).filter(
        violations.c.side == "purchase"), 0)
    sales_gap = func.coalesce(func.sum(violations.c.total_gap).filter(
        violations.c.side == "sales"), 0)
    pool_row = db.execute(
        select(
            violations.c.pool_group_id, violations.c.pool_name,
            purchase_gap.label("purchase_gap"), sales_gap.label("sales_gap"),
            func.sum(violations.c.total_gap).label("total_gap"),
            func.count().label("lines"),
        )
        .group_by(violations.c.pool_group_id, violations.c.pool_name)
        .order_by(func.sum(violations.c.total_gap).desc(), violations.c.pool_group_id.asc())
        .limit(1)
    ).one_or_none()
    most_severe = None if pool_row is None else {
        "pool_group_id": pool_row.pool_group_id, "pool_name": pool_row.pool_name,
        "purchase_total_gap": _r(pool_row.purchase_gap) or 0.0,
        "sales_total_gap": _r(pool_row.sales_gap) or 0.0,
        "total_gap": _r(pool_row.total_gap) or 0.0,
        "violation_line_count": int(pool_row.lines or 0),
    }

    handlers = {"purchase": [], "sales": []}
    for row in db.execute(
        select(
            violations.c.side, violations.c.person,
            func.count().label("lines"),
            func.count(func.distinct(violations.c.order_id)).label("orders"),
            func.sum(violations.c.total_gap).label("total_gap"),
        )
        .group_by(violations.c.side, violations.c.person)
        .order_by(violations.c.side, func.sum(violations.c.total_gap).desc(),
                  violations.c.person.asc().nullslast())
    ):
        handlers[row.side].append({
            "person": row.person,
            "violation_line_count": int(row.lines or 0),
            "order_count": int(row.orders or 0), "total_gap": _r(row.total_gap) or 0.0,
        })

    recent = []
    for row in db.execute(
        select(violations)
        .order_by(violations.c.order_date.desc().nullslast(),
                  violations.c.order_id.desc(), violations.c.line_id.desc())
        .limit(10)
    ).mappings():
        item = dict(row)
        item["order_date"] = item["order_date"].isoformat() if item["order_date"] else None
        item["quantity"] = _r(item["quantity"], 3)
        for key in ("actual_unit_ex_tax", "manual_limit_ex_tax", "unit_gap", "total_gap"):
            item[key] = _r(item[key])
        recent.append(item)

    return {
        "restricted": False, "basis": "ex_tax",
        "window": _window_payload(lower, upper, today, token),
        "purchase": side_totals["purchase"], "sales": side_totals["sales"],
        "most_severe_pool": most_severe, "handler_summary": handlers,
        "recent_violations": recent, "missing_constraints": _missing_constraints(db),
    }
