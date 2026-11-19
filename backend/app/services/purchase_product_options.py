"""Bounded search across only the eligible product dimensions of the current purchase scope."""

from sqlalchemy import String, cast, func, or_, select

from app.models import DimPart
from app.models.inventory import PartPool
from app.services import purchase_analytics_cube as cube


def choices(db, ctx, filters, scope, axis, term):
    cube.guard_dimensions(ctx, [axis], scope, "orders")
    omit = {axis, "partId"} if axis == "pn" else {axis}
    query, _ = cube.facts(
        filters,
        {k: v for k, v in scope.items() if k not in omit},
        [axis],
        metric="orders",
        db=db,
    )
    matched = query.subquery()
    key = matched.c[axis]
    grouped = select(key.label("key"), func.count().label("n")).group_by(key).subquery()
    value = grouped.c.key
    term = term.strip()
    if axis == "pool":
        title = PartPool.name
        stmt = (
            select(value, title.label("label"))
            .select_from(grouped)
            .outerjoin(PartPool, cast(PartPool.group_id, String) == value)
        )
        if term:
            stmt = stmt.where(title.icontains(term, autoescape=True))
    elif axis == "pn":
        # Cleaned PN is the cube identity. One deterministic catalog label per key.
        pn_key = cube.norm(DimPart.pn_std)
        directory = (
            select(pn_key.label("pn"), DimPart.description)
            .distinct(pn_key)
            .order_by(pn_key, DimPart.id)
            .subquery()
        )
        title = directory.c.description
        stmt = (
            select(value, title.label("label"))
            .select_from(grouped)
            .outerjoin(directory, directory.c.pn == value)
        )
        if term:
            stmt = stmt.where(
                or_(
                    value.icontains(term, autoescape=True),
                    title.icontains(term, autoescape=True),
                )
            )
    else:
        stmt = select(value, value.label("label"))
        if term:
            stmt = stmt.where(value.icontains(term, autoescape=True))
    rows = (
        db.execute(stmt.order_by(grouped.c.n.desc(), value.asc().nullslast()).limit(31))
        .mappings()
        .all()
    )
    return {"items": [dict(r) for r in rows[:30]], "has_more": len(rows) > 30}
