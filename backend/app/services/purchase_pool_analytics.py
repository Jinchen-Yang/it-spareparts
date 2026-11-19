"""D-0064. 采购经营事实与正式价格样本分别聚合，同单位比较。"""

from sqlalchemy import and_, case, func, null, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.data_quality import FactDataQualityIssue
from app.models.dimensions import DimPart, DimSupplier
from app.models.inventory import PartPool, PartPoolMember, PartPoolPricePolicy
from app.models.purchase import FPurchaseLine as Line, FPurchaseOrder as Order
from app.services import purchase_analytics_cube as cube
from app.services.pool_price_rules import purchase_priced_condition
from app.services.pricing import purchase_ex_unit


def _facts(db, filters, scope, price_visible, *, part_id=None):
    query, _ = cube.facts(filters, scope, [], line_basis=True, db=db)
    unit = func.nullif(func.btrim(Line.unit), "")
    stmt = query.with_only_columns(
        Line.id.label("line_id"),
        Order.id.label("order_id"),
        Line.part_id,
        Order.supplier_id,
        unit.label("unit"),
        Line.qty,
    )
    if part_id is not None:
        stmt = stmt.where(Line.part_id == part_id)
    # An outer join to the distinct error set preserves every operating fact, including
    # confirmed errors, while excluding such rows from every formal price statistic.
    if price_visible:
        errors = (
            select(FactDataQualityIssue.line_id)
            .where(
                FactDataQualityIssue.side == "purchase",
                FactDataQualityIssue.status == "confirmed_source_error",
            )
            .distinct()
            .subquery()
        )
        valid = and_(
            purchase_priced_condition(),
            Order.order_date <= business_today(),
            unit.is_not(None),
            errors.c.line_id.is_(None),
        )
        stmt = stmt.outerjoin(errors, errors.c.line_id == Line.id)
        stmt = stmt.add_columns(case((valid, purchase_ex_unit())).label("sample_price"))
    else:
        stmt = stmt.add_columns(null().label("sample_price"))
    return stmt.subquery()


def _measures(f, price_visible):
    valid = f.c.sample_price.is_not(None)
    result = [
        func.count(func.distinct(f.c.order_id)).label("orders"),
        func.count().label("lines"),
        func.sum(f.c.qty).filter(f.c.unit.is_not(None)).label("quantity"),
    ]
    if price_visible:
        result.extend(
            [
                func.sum(f.c.sample_price * f.c.qty).label("sample_value"),
                func.sum(f.c.qty).filter(valid).label("price_qty"),
                func.count(func.distinct(f.c.order_id)).filter(valid).label("price_orders"),
                func.min(f.c.sample_price).label("price_min"),
                func.max(f.c.sample_price).label("price_max"),
            ]
        )
    return result


def _page_items(db, stmt, page, page_size):
    rows = db.execute(stmt.offset((page - 1) * page_size).limit(page_size + 1)).mappings().all()
    return rows[:page_size], len(rows) > page_size


def _number(value):
    return float(value) if value is not None else None


def _item(row, fields, price_visible):
    item = {key: value for key, value in row.items() if key not in fields}
    if "quantity" in item:
        item["quantity"] = _number(item["quantity"])
    # purchase_metrics is the registered purchase-cost container. Price sample
    # counts, eligibility and order are also structurally guarded at entry.
    item["purchase_metrics"] = (
        {key: _number(row[key]) for key in fields if key in row} if price_visible else None
    )
    return item


def parts(
    db: Session,
    filters,
    scope,
    *,
    q,
    focus,
    pool_id,
    page,
    page_size,
    price_visible,
    supplier_visible,
):
    f = _facts(db, filters, scope, price_visible)
    counts = _measures(f, price_visible)
    if supplier_visible:
        counts.append(func.count(func.distinct(f.c.supplier_id)).label("suppliers"))
    else:
        counts.append(null().label("suppliers"))
    grouped = select(f.c.part_id, f.c.unit, *counts).group_by(f.c.part_id, f.c.unit).subquery()
    g = grouped.c
    members = cube.active_membership()
    stmt = (
        select(
            g.part_id,
            DimPart.pn_std.label("pn"),
            DimPart.description,
            g.unit,
            g.orders,
            g.lines,
            g.quantity,
            g.suppliers,
            members.c.group_id.label("pool_id"),
            PartPool.name.label("pool_name"),
        )
        .select_from(grouped)
        .join(DimPart, DimPart.id == g.part_id)
        .outerjoin(members, members.c.part_id == func.coalesce(DimPart.merged_into_id, DimPart.id))
        .outerjoin(PartPool, PartPool.group_id == members.c.group_id)
    )
    if price_visible:
        # The policy carries no unit. Only an unambiguous catalogue unit shared
        # by the active members gives a pool ceiling a comparable denominator.
        known_unit = func.nullif(func.btrim(DimPart.unit), "")
        units = (
            select(PartPoolMember.group_id, func.min(known_unit).label("cap_unit"))
            .join(DimPart, DimPart.id == PartPoolMember.part_id)
            .group_by(PartPoolMember.group_id)
            .having(
                func.count(known_unit) == func.count(), func.count(func.distinct(known_unit)) == 1
            )
            .subquery()
        )
        policy = PartPoolPricePolicy
        stmt = stmt.outerjoin(units, units.c.group_id == members.c.group_id).outerjoin(
            policy,
            and_(
                policy.group_id == members.c.group_id,
                policy.valid_to.is_(None),
                g.unit == units.c.cap_unit,
            ),
        )
        partition = [members.c.group_id, g.unit]
        pool_price = case(
            (
                members.c.group_id.is_not(None),
                func.sum(g.sample_value).over(partition_by=partition)
                / func.nullif(func.sum(g.price_qty).over(partition_by=partition), 0),
            )
        )
        stmt = stmt.add_columns(
            (g.sample_value / func.nullif(g.price_qty, 0)).label("price"),
            pool_price.label("pool_price"),
            policy.purchase_ceiling_ex_tax.label("ceiling"),
            g.price_qty,
            g.price_orders,
            g.price_min,
            g.price_max,
        )
    # Search and focus select rows only after the pool baseline has been computed.
    data = stmt.subquery().c
    out = select(*data)
    if q:
        out = out.where(
            data.pn.icontains(q, autoescape=True) | data.description.icontains(q, autoescape=True)
        )
    if pool_id is not None:
        out = out.where(data.pool_id == pool_id)
    if focus == "unpooled":
        out = out.where(data.pool_id.is_(None))
    elif focus == "single":
        out = out.where(data.suppliers == 1)
    sort = data.orders
    if price_visible:
        delta_pool = (data.price - data.pool_price) / func.nullif(data.pool_price, 0)
        delta_cap = (data.price - data.ceiling) / func.nullif(data.ceiling, 0)
        out = out.add_columns(delta_pool.label("delta_pool"), delta_cap.label("delta_cap"))
        if focus in ("above_pool", "above_cap"):
            sort = delta_pool if focus == "above_pool" else delta_cap
            out = out.where(sort > 0)
    rows, more = _page_items(
        db,
        out.order_by(sort.desc().nulls_last(), data.part_id, data.unit.asc().nulls_last()),
        page,
        page_size,
    )
    fields = {
        "price",
        "pool_price",
        "ceiling",
        "delta_pool",
        "delta_cap",
        "price_qty",
        "price_orders",
        "price_min",
        "price_max",
    }
    return {
        "items": [_item(row, fields, price_visible) for row in rows],
        "has_more": more,
        "price_visible": price_visible,
        "supplier_visible": supplier_visible,
    }


def sources(db, filters, scope, *, part_id, unit, page, page_size, price_visible):
    f = _facts(db, filters, scope, price_visible, part_id=part_id)
    grouped = (
        select(f.c.supplier_id, *_measures(f, price_visible))
        .where(f.c.unit == unit)
        .group_by(f.c.supplier_id)
        .subquery()
    )
    g = grouped.c
    stmt = (
        select(
            g.supplier_id,
            func.coalesce(DimSupplier.name_normalized, DimSupplier.name_raw).label("supplier"),
            g.orders,
            g.lines,
            g.quantity,
        )
        .select_from(grouped)
        .outerjoin(DimSupplier, DimSupplier.id == g.supplier_id)
    )
    if price_visible:
        stmt = stmt.add_columns(
            (g.sample_value / func.nullif(g.price_qty, 0)).label("price"),
            g.price_min,
            g.price_max,
            g.price_orders,
            g.price_qty,
        )
    rows, more = _page_items(
        db,
        stmt.order_by(g.quantity.desc().nulls_last(), g.supplier_id.asc().nulls_last()),
        page,
        page_size,
    )
    fields = {"price", "price_min", "price_max", "price_orders", "price_qty"}
    return {"items": [_item(row, fields, price_visible) for row in rows], "has_more": more}


def peers(db, filters, scope, *, pool_id, unit, page, page_size, price_visible):
    active = cube.active_membership()
    # Preserve the source PN of merged records so clicking a peer reproduces
    # its exact facts. List a tombstone only when it contributed in this scope.
    members = (
        select(
            DimPart.id.label("part_id"), DimPart.pn_std, DimPart.description, DimPart.merged_into_id
        )
        .join(active, active.c.part_id == func.coalesce(DimPart.merged_into_id, DimPart.id))
        .where(active.c.group_id == pool_id)
        .subquery()
    )
    f = _facts(db, filters, scope, price_visible)
    group = (
        select(f.c.part_id, *_measures(f, price_visible))
        .where(f.c.part_id.in_(select(members.c.part_id)), f.c.unit == unit)
        .group_by(f.c.part_id)
        .subquery()
    )
    g = group.c
    m = members.c
    stmt = (
        select(
            m.part_id,
            m.pn_std.label("pn"),
            m.description,
            func.coalesce(g.orders, 0).label("orders"),
            g.quantity,
        )
        .select_from(members)
        .outerjoin(group, g.part_id == m.part_id)
        .where(m.merged_into_id.is_(None) | g.part_id.is_not(None))
    )
    if price_visible:
        stmt = stmt.add_columns((g.sample_value / func.nullif(g.price_qty, 0)).label("price"))
    rows, more = _page_items(
        db, stmt.order_by(g.orders.desc().nulls_last(), m.pn_std, m.part_id), page, page_size
    )
    return {"items": [_item(row, {"price"}, price_visible) for row in rows], "has_more": more}
