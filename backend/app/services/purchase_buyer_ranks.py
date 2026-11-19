"""采购员全域排行。各列的事实粒度由业务筛选决定，排序只选择一列。"""

from sqlalchemy import Numeric, and_, case, cast, func, null, select
from sqlalchemy.orm import Session

from app.models import DimPart
from app.models import FPurchaseLine as PL
from app.models import FPurchaseOrder as PO
from app.security import UserContext, is_field_hidden
from app.services import purchase_analytics as pa
from app.services import purchase_analytics_cube as cube


def _quantity(unit, qty, quantity_unit):
    if not quantity_unit:
        return cast(null(), Numeric)
    return func.sum(qty).filter(unit == quantity_unit)


def _by_matched_line(query, quantity_unit):
    # PN、品类、池或页面单位已限定成交行，与精确下钻共享同一行集。
    line = query.add_columns(
        cube.norm(DimPart.pn_std).label("product_pn"),
        cube.norm(PL.unit).label("product_unit"),
    ).subquery()
    return select(
        line.c.buyer,
        func.count(func.distinct(line.c.order_id)).label("orders"),
        func.count().label("lines"),
        func.count(func.distinct(line.c.supplier_identity)).label("supplier_count"),
        func.sum(line.c.value).label("amount"),
        func.count(func.distinct(line.c.product_pn)).label("pn_count"),
        _quantity(line.c.product_unit, line.c.qty, quantity_unit).label("quantity"),
    ).group_by(line.c.buyer)


def _by_order(query, quantity_unit, has_sales_link):
    facts = query.add_columns(PO.import_batch_id.label("batch_id")).subquery()
    heads = select(
        facts.c.order_id,
        facts.c.batch_id,
        facts.c.buyer,
        facts.c.value,
        facts.c.supplier_identity,
    ).cte("buyer_scoped_orders")
    # 普通筛选的两条聚合路径可分别优化；销采关联先去重，复用其有界订单集合。
    if not has_sales_link:
        heads = heads.prefix_with("NOT MATERIALIZED", dialect="postgresql")
    # 先按采购员、供应商分组可以同时保住单头金额及去重供应商数。
    by_supplier = (
        select(
            heads.c.buyer,
            heads.c.supplier_identity,
            func.count().label("orders"),
            func.sum(heads.c.value).label("amount"),
        )
        .group_by(heads.c.buyer, heads.c.supplier_identity)
        .subquery()
    )
    orders = (
        select(
            by_supplier.c.buyer,
            func.sum(by_supplier.c.orders).label("orders"),
            func.sum(by_supplier.c.amount).label("amount"),
            func.count(by_supplier.c.supplier_identity).label("supplier_count"),
        )
        .group_by(by_supplier.c.buyer)
        .subquery()
    )
    pn = cube.norm(DimPart.pn_std)
    by_pn = (
        select(
            heads.c.buyer,
            pn.label("pn"),
            func.count().label("lines"),
            _quantity(cube.norm(PL.unit), PL.qty, quantity_unit).label("quantity"),
        )
        .select_from(heads)
        .join(
            PL,
            and_(PL.order_id == heads.c.order_id, PL.import_batch_id == heads.c.batch_id),
        )
        .join(DimPart, DimPart.id == PL.part_id)
        .group_by(heads.c.buyer, pn)
        .subquery()
    )
    lines = (
        select(
            by_pn.c.buyer,
            func.sum(by_pn.c.lines).label("lines"),
            func.count(by_pn.c.pn).label("pn_count"),
            func.sum(by_pn.c.quantity).label("quantity"),
        )
        .group_by(by_pn.c.buyer)
        .subquery()
    )
    # 当前批次无明细的单据仍计入单头；采购员空值也能连接到对应的明细。
    return select(
        orders,
        func.coalesce(lines.c.lines, 0).label("lines"),
        func.coalesce(lines.c.pn_count, 0).label("pn_count"),
        lines.c.quantity,
    ).outerjoin(lines, orders.c.buyer.is_not_distinct_from(lines.c.buyer))


def ranks(
    db: Session,
    ctx: UserContext,
    filters: dict,
    scope: dict,
    *,
    metric: str,
    basis: str,
    quantity_unit: str | None,
    page: int,
    page_size: int,
):
    quantity_unit = (quantity_unit or "").strip() or None
    if "unit" in scope:
        if quantity_unit and quantity_unit != scope["unit"]:
            cube.invalid("数量单位与页面单位筛选不一致")
        quantity_unit = scope["unit"]
    if metric == "qty" and not quantity_unit:
        cube.invalid("按采购数量排行须指定计量单位")

    query, line_basis = cube.facts(filters, scope, ["buyer"], basis=basis, metric="orders", db=db)
    if line_basis:
        grouped = _by_matched_line(query, quantity_unit).subquery()
    else:
        has_sales_link = (
            filters.get("customer_id")
            or filters.get("salesperson")
            or scope.keys() & cube.LINK_AXES
        )
        grouped = _by_order(query, quantity_unit, has_sales_link).subquery()
    field = {"pns": "pn_count", "qty": "quantity", "suppliers": "supplier_count"}.get(
        metric, metric
    )
    value = grouped.c[field]
    rank = case((value.is_not(None), func.rank().over(order_by=value.desc().nullslast())))
    query = select(grouped, value.label("value"), rank.label("rank"))
    rows, total = cube.ranked_page(db, query, "buyer", page_size, page)
    financial = not is_field_hidden(ctx, "amount_paid")
    supplier = not is_field_hidden(ctx, "supplier")
    drill = {**scope, **({"_line": "1"} if line_basis else {})}
    return {
        "total": total,
        "items": [
            {
                "key": row["buyer"],
                "label": row["buyer"] if row["buyer"] is not None else cube.null_label("buyer"),
                "rank": row["rank"],
                "scope": {**drill, "buyer": row["buyer"]},
                "orders": int(row["orders"]),
                "lines": int(row["lines"]),
                "pn_count": row["pn_count"],
                "quantity": pa._f(row["quantity"]),
                "amount": pa._f(row["amount"]) if financial else None,
                "supplier_count": row["supplier_count"] if supplier else None,
            }
            for row in rows
        ],
    }
