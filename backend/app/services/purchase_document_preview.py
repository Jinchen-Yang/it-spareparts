"""Bounded product preview for the exact visible purchases behind a document link."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DimPart, FPurchaseLine, FPurchaseOrder
from app.services import purchase_analytics_cube as cube
from app.services.purchase_analytics import _f

LIMIT = 8


def chart_sample(db: Session, filters, scope):
    """One matching current-batch line from the newest order the viewer can inspect."""
    query, _ = cube.facts(filters, scope, {"pn"}, metric="lines", visible_only=True, db=db)
    po, line, part = FPurchaseOrder, FPurchaseLine, DimPart
    stmt = (
        query.with_only_columns(
            po.order_no, po.order_date, part.pn_std,
            func.coalesce(cube.norm(line.description), cube.norm(part.description)).label("description"),
            cube.norm(part.category_major).label("category"),
            line.qty, cube.norm(line.unit).label("unit"),
        )
        .order_by(po.order_date.desc().nullslast(), po.id.desc(), line.line_no.asc().nullslast(), line.id)
        .limit(1)
    )
    row = db.execute(stmt).mappings().first()
    return {"sample": dict(row, qty=_f(row["qty"])) if row else None}


def preview(db: Session, ctx, filters, raw_scope, *, order_id=None, sales_no=None):
    if (order_id is None) == (sales_no is None):
        cube.invalid("请指定一张采购单或一个关联销售单号")
    scope = cube.parse_scope(raw_scope)
    cube.guard_dimensions(ctx, [], scope, "lines")
    axes = {"pn", "unit", "category"}
    query, _ = cube.facts(filters, scope, axes, metric="lines", visible_only=True, db=db)
    # Preserve an inbound document filter as an intersection. Use the typed column so a hover
    # can probe the order primary key or the index on the linked sales number.
    doc_filter = (
        FPurchaseOrder.id == order_id
        if order_id is not None
        else FPurchaseOrder.linked_sales_order_no == sales_no
    )
    lines = query.where(doc_filter).subquery()
    count = db.scalar(select(func.count(func.distinct(lines.c.order_id))))
    qty = func.sum(lines.c.qty).filter(lines.c.unit.is_not(None)).label("qty")
    frequency = func.count().desc()
    description = func.coalesce(
        func.min(func.nullif(func.btrim(FPurchaseLine.description), "")),
        func.min(func.nullif(func.btrim(DimPart.description), "")),
    ).label("description")
    products = (
        db.execute(
            select(lines.c.pn, lines.c.unit, qty, description)
            .select_from(lines)
            .join(FPurchaseLine, FPurchaseLine.id == lines.c.line_id)
            .join(DimPart, DimPart.id == lines.c.part_id)
            .group_by(lines.c.pn, lines.c.unit)
            .order_by(frequency, lines.c.pn, lines.c.unit)
            .limit(LIMIT + 1)
        )
        .mappings()
        .all()
    )
    categories = (
        db.execute(
            select(lines.c.category, lines.c.unit, qty)
            .group_by(lines.c.category, lines.c.unit)
            .order_by(frequency, lines.c.category, lines.c.unit)
            .limit(LIMIT + 1)
        )
        .mappings()
        .all()
    )
    return {
        "orders": count,
        "products": [{**row, "qty": _f(row["qty"])} for row in products[:LIMIT]],
        "categories": [{**row, "qty": _f(row["qty"])} for row in categories[:LIMIT]],
        "more_products": len(products) > LIMIT,
        "more_categories": len(categories) > LIMIT,
    }
