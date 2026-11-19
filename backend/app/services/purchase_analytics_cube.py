"""D-0064. Read-only purchase facts, paged dimensions and exact drill scope."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.orm import Session

from app import security
from app.models import DimCustomer, DimPart, FPurchaseLine as PL, FPurchaseOrder as PO
from app.models.dimensions import DimSupplier
from app.models.inventory import PartPool, PartPoolMember
from app.services import departed_visibility, purchase_analytics as pa

LINE_AXES = {"pn", "pool", "category", "unit"}
LINK_AXES = {"customer", "salesperson", "linkStatus"}
DIMENSIONS = (
    LINE_AXES
    | LINK_AXES
    | {
        "buyer",
        "type",
        "day",
        "week",
        "month",
        "supplier",
        "linkedSalesOrderNo",
        "orderId",
    }
)
METRICS = {"amount", "avgOrder", "orders", "lines", "qty", "suppliers", "pns"}
BASIS = {"paid", "inc", "ex"}


def invalid(message):
    raise pa.FilterValidationError(message)


def parse_scope(raw):
    try:
        scope = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        invalid("scope 需为 JSON 对象")
    if not isinstance(scope, dict) or set(scope) - DIMENSIONS - {"_line", "partId"}:
        invalid("scope 含未知维度")
    for key, val in scope.items():
        if val is not None and (not isinstance(val, str) or len(val) > 256):
            invalid("范围须为精确文本键或 null")
        if key == "_line" and val != "1":
            invalid("行口径标记非法")
        if key in {"supplier", "pool", "customer", "orderId", "partId"} and val is not None:
            if not val.isascii() or not val.isdigit() or not 1 <= int(val) <= 2147483647:
                invalid("身份键越界")
    return scope


def guard_dimensions(ctx, axes, scope, metric):
    keys = set(axes) | (set(scope) - {"_line"})
    if set(axes) - DIMENSIONS or keys - DIMENSIONS - {"partId"} or metric not in METRICS:
        invalid("未知维度或指标")
    if ("supplier" in keys or metric == "suppliers") and security.is_field_hidden(ctx, "supplier"):
        raise HTTPException(403, "无供应商权限")
    if "customer" in keys and security.is_field_hidden(ctx, "customer"):
        raise HTTPException(403, "无客户信息权限")
    if metric in {"amount", "avgOrder"} and security.is_field_hidden(ctx, "amount_paid"):
        raise HTTPException(403, "无采购成本权限")


def active_membership():
    return (
        select(PartPoolMember.part_id, PartPool.group_id)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .where(PartPool.status == "active")
        .subquery()
    )


def norm(col):
    return func.nullif(func.btrim(col), "")


def dimension_expressions(link=None, member=None):
    values = {
        "buyer": func.nullif(PO.purchaser, ""),
        "type": func.nullif(PO.source_type_raw, ""),
        "supplier": cast(PO.supplier_id, String),
        "orderId": cast(PO.id, String),
        "linkedSalesOrderNo": func.nullif(PO.linked_sales_order_no, ""),
        "day": func.to_char(PO.order_date, "YYYY-MM-DD"),
        "month": func.to_char(PO.order_date, "YYYY-MM"),
        "week": func.to_char(func.date_trunc("week", PO.order_date), "YYYY-MM-DD"),
        "partId": cast(PL.part_id, String),
        "pn": norm(DimPart.pn_std),
        "category": norm(DimPart.category_major),
        "unit": norm(PL.unit),
    }
    if link is not None:
        values.update(
            customer=cast(link.c.customer_id, String),
            salesperson=func.nullif(link.c.salesperson, ""),
            linkStatus=case(
                (func.nullif(PO.linked_sales_order_no, "").is_(None), "未关联"),
                (link.c.order_no.is_(None), "未找到销售单"),
                else_="已关联",
            ),
        )
    if member is not None:
        values["pool"] = cast(member.c.group_id, String)
    return values


def facts(
    filters,
    scope,
    axes,
    *,
    metric="amount",
    basis="paid",
    line_basis=False,
    visible_only=False,
    db: Session,
):
    """Produce one row per current-batch line or one row per head, including no-line orders."""
    keys = set(axes) | (set(scope) - {"_line"})
    if metric == "pns":
        keys.add("pn")
    if basis not in BASIS or keys - DIMENSIONS - {"partId"}:
        invalid("金额口径或维度无效")
    pa._validate_common(filters.get("date_from"), filters.get("date_to"))
    common = {
        key: filters.get(key)
        for key in (
            "date_from",
            "date_to",
            "source_type",
            "purchaser",
            "salesperson",
            "customer_id",
            "supplier",
            "order_no",
            "linked_sales_order_no",
        )
    }
    po_where = pa._base_where(PO, None, **{**common, "salesperson": None, "customer_id": None})
    link = (
        pa._sales_link_bounded(po_where)
        if keys & LINK_AXES or common["customer_id"] or common["salesperson"]
        else None
    )
    cat = pa._norm_categories(filters.get("category"))
    line = bool(
        line_basis
        or "_line" in scope
        or cat
        or keys & (LINE_AXES | {"partId"})
        or metric in {"qty", "pns"}
    )
    # Grouping on count-of-distinct PN or quantity intrinsically requires the line domain.
    where = pa._base_where(PO, link, **common)
    visibility = []
    if visible_only or keys & {"linkedSalesOrderNo", "orderId"}:
        hidden_nos = departed_visibility.hidden_sales_order_nos(db)
        if hidden_nos:
            permitted = or_(
                PO.linked_sales_order_no.is_(None),
                PO.linked_sales_order_no.not_in(hidden_nos),
            )
            if visible_only or set(scope) & {"linkedSalesOrderNo", "orderId"}:
                where.append(permitted)
            else:
                visibility.append(permitted.label("show_document"))
    if "salesperson" in keys:
        hidden = departed_visibility.hidden_salespersons(db)
        if hidden:
            permitted = or_(link.c.salesperson.is_(None), link.c.salesperson.not_in(hidden))
            if "salesperson" in scope:
                where.append(permitted)
            else:
                visibility.append(permitted.label("show_person"))
    member = active_membership() if "pool" in keys else None
    values = dimension_expressions(link, member)
    for key, val in scope.items():
        if key != "_line":
            where.append(values[key] == val)
    if cat:
        where.append(pa._category_match(cat))
    if line:
        ex, inc = pa._line_amount_exprs()
        paid = PL.line_amount
        count = literal(1)
        extras = [
            PL.id.label("line_id"),
            PL.part_id.label("part_id"),
            PL.qty.label("qty"),
        ]
    else:
        ex, inc, paid, _ = pa._order_amount_exprs()
        # One grouped scan of the in-window batch lines. A LEFT JOIN retains zero-line orders.
        line_counts = (
            select(PL.order_id, func.count().label("n"))
            .select_from(PO)
            .join(PL, pa._batch_line_join())
            .where(*po_where)
            .group_by(PL.order_id)
            .subquery()
        )
        count = func.coalesce(line_counts.c.n, 0)
        extras = []
    query = select(
        PO.id.label("order_id"),
        PO.supplier_id.label("supplier_identity"),
        count.label("line_count"),
        inc.label("amount_inc"),
        ex.label("amount_ex"),
        paid.label("amount_paid"),
        {"paid": paid, "inc": inc, "ex": ex}[basis].label("value"),
        *extras,
        *visibility,
        *(values[key].label(key) for key in sorted(keys)),
    ).select_from(PO)
    if line:
        query = query.join(PL, pa._batch_line_join()).join(DimPart, DimPart.id == PL.part_id)
    else:
        query = query.outerjoin(line_counts, line_counts.c.order_id == PO.id)
    if member is not None:
        query = query.outerjoin(
            member,
            member.c.part_id == func.coalesce(DimPart.merged_into_id, DimPart.id),
        )
    return pa._link_join(query, PO, link).where(*where), line


def aggregate(f, axes, metric, *, grouping_sets=False):
    orders = func.count(func.distinct(f.c.order_id)) if "line_id" in f.c else func.count()
    lines = func.coalesce(func.sum(f.c.line_count), 0)
    money = func.sum(f.c.value)
    suppliers = func.count(func.distinct(f.c.supplier_identity))
    metric_value = {
        "amount": money,
        "avgOrder": money / func.nullif(orders, 0),
        "orders": orders,
        "lines": lines,
        "suppliers": suppliers,
    }.get(metric)
    if metric == "qty":
        metric_value = func.sum(f.c.qty).filter(f.c.unit.is_not(None))
    if metric == "pns":
        metric_value = func.count(func.distinct(f.c.pn))
    keys = [f.c[key] for key in axes]
    query = (
        select(
            *keys,
            metric_value.label("value"),
            orders.label("orders"),
            lines.label("lines"),
            suppliers.label("supplier_count"),
            *(func.sum(f.c[key]).label(key) for key in ("amount_paid", "amount_inc", "amount_ex")),
        )
        .select_from(f)
        .group_by(*keys)
    )

    # D-0063 hides document/person identities only in groups exposing that axis.
    # The grand total and other margins still include those orders.
    for axis in axes:
        flag = {
            "orderId": "show_document",
            "linkedSalesOrderNo": "show_document",
            "salesperson": "show_person",
        }.get(axis)
        if flag and flag in f.c:
            allowed = func.bool_or(f.c[flag])
            if grouping_sets:
                allowed = or_(func.grouping(f.c[axis]) == 1, allowed)
            query = query.having(allowed)
    return query


def labels(db, axis, keys):
    ids = (
        [int(k) for k in keys if k is not None]
        if axis in {"supplier", "customer", "pool", "orderId"}
        else []
    )
    if axis == "pn":
        clean = [k for k in keys if k is not None]
        return {
            k: (k, d)
            for k, d in db.execute(
                select(norm(DimPart.pn_std), DimPart.description)
                .where(norm(DimPart.pn_std).in_(clean))
                .order_by(DimPart.id.desc())
            )
        }
    columns = {
        "supplier": (
            DimSupplier.id,
            func.coalesce(DimSupplier.name_normalized, DimSupplier.name_raw),
        ),
        "customer": (
            DimCustomer.id,
            func.coalesce(DimCustomer.name_normalized, DimCustomer.name_raw),
        ),
        "pool": (PartPool.group_id, PartPool.name),
        "orderId": (PO.id, PO.order_no),
    }
    if axis in columns and ids:
        key, name = columns[axis]
        return {str(k): (n, None) for k, n in db.execute(select(key, name).where(key.in_(ids)))}
    return {}


def null_label(axis):
    return {
        "pool": "未入池",
        "category": "未分类",
        "linkedSalesOrderNo": "未关联",
        "day": "无日期",
        "week": "无日期",
        "month": "无日期",
    }.get(axis, "未填写")


def pack(row, ctx, scope):
    return {
        **{
            key: pa._f(row[key]) if not security.is_field_hidden(ctx, key) else None
            for key in ("amount_paid", "amount_inc", "amount_ex")
        },
        "orders": int(row["orders"]),
        "lines": int(row["lines"]),
        "value": pa._f(row["value"]),
        "scope": scope,
        "supplier_count": None
        if security.is_field_hidden(ctx, "supplier")
        else row["supplier_count"],
    }


def ranked_page(db, query, axis, size, page, sort="desc", value_max=None):
    grouped = query.subquery()
    stmt = select(grouped, func.count().over().label("num_groups"))
    if value_max is not None:
        stmt = stmt.where(grouped.c.value <= value_max)
    order_by = (
        grouped.c.value.asc().nullslast() if sort == "asc" else grouped.c.value.desc().nullslast()
    )
    if sort in {"absolute", "increase", "decrease"}:
        delta = grouped.c.change
        order_by = (
            func.abs(delta).desc()
            if sort == "absolute"
            else delta.desc()
            if sort == "increase"
            else delta.asc()
        ).nullslast()
    if sort == "key":
        order_by = grouped.c[axis].asc().nullslast()
    result = (
        db.execute(
            stmt.order_by(order_by, grouped.c[axis].asc().nullslast())
            .offset((page - 1) * size)
            .limit(size)
        )
        .mappings()
        .all()
    )
    count = (
        result[0]["num_groups"]
        if result
        else (db.scalar(select(func.count()).select_from(stmt.subquery())) if page > 1 else 0)
    )
    return result, count


def single_pass(
    db,
    ctx,
    f,
    drill,
    out,
    *,
    row,
    column,
    metric,
    sort,
    page,
    page_size,
    column_page,
    column_size,
    value_max,
):
    """One fact scan for KPI, both independently ranked margins and their exact intersections."""
    axes = [key for key in (row, column) if key]
    base = aggregate(f, axes, metric, grouping_sets=True)
    if not axes:
        out["total"] = pack(db.execute(base).mappings().one(), ctx, drill)
        return out
    cols = [f.c[a] for a in axes]
    sets = [tuple_(), *(tuple_(c) for c in cols)]
    if column:
        sets.append(tuple_(*cols))
    combined = (
        base.group_by(None)
        .add_columns(func.grouping(*cols).label("level"))
        .group_by(func.grouping_sets(*sets))
        .cte("aggregates")
    )
    total_level = 3 if column else 1

    # group flags: (row,col) 00=cell, 01=row, 10=column, 11=grand;
    # a single dimension uses 0=group, 1=grand. NULL remains a real drillable key.
    def margin(axis, level, size, number):
        groups = select(combined).where(combined.c.level == level)
        if value_max is not None:
            groups = groups.where(combined.c.value <= value_max)
        all_groups = groups.subquery()
        value = all_groups.c.value
        key = all_groups.c[axis]
        order = key.asc() if sort == "key" else value.asc() if sort == "asc" else value.desc()
        subset = (
            select(all_groups)
            .order_by(order.nullslast(), key.asc().nullslast())
            .limit(size)
            .offset((number - 1) * size)
            .cte()
        )
        return subset, select(func.count()).select_from(all_groups).scalar_subquery()

    rows, row_count = margin(row, 1 if column else 0, page_size, page)
    pieces = [select(combined).where(combined.c.level == total_level), select(rows)]
    col_count = literal(0)
    if column:
        columns, col_count = margin(column, 2, column_size, column_page)
        pieces.append(select(columns))

        def member(axis, table):
            # A hashable IN for normal keys; NULL participates only when its margin was selected.
            return or_(
                combined.c[axis].in_(select(table.c[axis])),
                and_(
                    combined.c[axis].is_(None),
                    select(table.c[axis]).where(table.c[axis].is_(None)).exists(),
                ),
            )

        pieces.append(
            select(combined).where(
                combined.c.level == 0, member(row, rows), member(column, columns)
            )
        )
    selected = union_all(*pieces).subquery()
    # Repeat the margin ordering in the outer query: ORDER BY inside a UNION branch
    # decides membership, but does not define the ordering sent to clients.
    order = [selected.c.level]
    if sort != "key":
        direction = selected.c.value.asc() if sort == "asc" else selected.c.value.desc()
        order.append(direction.nullslast())
    order.extend(selected.c[a].asc().nullslast() for a in axes)
    records = (
        db.execute(select(selected, row_count.label("nr"), col_count.label("nc")).order_by(*order))
        .mappings()
        .all()
    )
    out["total_rows"], out["total_columns"] = records[0]["nr"], records[0]["nc"]
    for rec in records:
        if rec["level"] == total_level:
            out["total"] = pack(rec, ctx, drill)
        elif column and rec["level"] == 0:
            out["cells"].append(
                {
                    **pack(rec, ctx, {**drill, row: rec[row], column: rec[column]}),
                    "row_key": rec[row],
                    "column_key": rec[column],
                }
            )
    for axis, flag, name in [(row, 1 if column else 0, "rows"), (column, 2, "columns")]:
        if not axis:
            continue
        group_rows = [rec for rec in records if rec["level"] == flag]
        dictionary = labels(db, axis, [rec[axis] for rec in group_rows])
        for rec in group_rows:
            key = rec[axis]
            label, desc = dictionary.get(key, (key if key is not None else null_label(axis), None))
            out[name].append(
                {
                    **pack(rec, ctx, {**drill, axis: key}),
                    "key": key,
                    "label": label,
                    "description": desc,
                }
            )
    if metric == "qty" and "unit" not in drill:
        out["total"]["value"] = None
    return out


def cube(
    db,
    ctx,
    filters,
    scope,
    *,
    row,
    column,
    metric,
    basis,
    page,
    page_size,
    column_page,
    column_size,
    compare=False,
    sort="desc",
    value_max=None,
):
    axes = [key for key in (row, column) if key]
    if column and (not row or row == column) or compare and (column or not row):
        invalid("请选择不同的交叉维度；对比仅支持单维")
    if metric == "qty" and "unit" not in scope and ("unit" not in axes or column):
        invalid("采购数量请指定单位，或按单位查看")
    if sort in {"absolute", "increase", "decrease"} and not compare:
        invalid("变动排序需要对比期间")
    q, line = facts(filters, scope, axes, metric=metric, basis=basis, db=db)
    f = q.subquery()
    drill = {**scope, **({"_line": "1"} if line else {})}

    def total(source):
        return pack(db.execute(aggregate(source, [], metric)).mappings().one(), ctx, drill)

    out = {
        "row": row,
        "column": column,
        "metric": metric,
        "basis": basis,
        "basis_mode": "line" if line else "order",
        "rows": [],
        "columns": [],
        "cells": [],
        "total_rows": 0,
        "total_columns": 0,
    }
    if not compare:
        return single_pass(
            db,
            ctx,
            f,
            drill,
            out,
            row=row,
            column=column,
            metric=metric,
            sort=sort,
            page=page,
            page_size=page_size,
            column_page=column_page,
            column_size=column_size,
            value_max=value_max,
        )
    start, end = filters.get("date_from"), filters.get("date_to")
    if not start or not end:
        invalid("对比需要完整日期范围")
    try:
        prior_end = start - timedelta(days=1)
        prior_start = prior_end - (end - start)
    except OverflowError:
        invalid("对比区间超出可用日期")
    prior_query, _ = facts(
        {**filters, "date_from": prior_start, "date_to": prior_end},
        scope,
        axes,
        metric=metric,
        basis=basis,
        db=db,
    )
    previous = prior_query.subquery()
    out.update(
        total=total(f),
        previous_from=prior_start.isoformat(),
        previous_to=prior_end.isoformat(),
        previous_total=total(previous),
    )
    if metric == "qty" and "unit" not in scope:
        out["total"]["value"] = out["previous_total"]["value"] = None
    # UNION of period rows preserves NULL dimension keys and each period's distinct counts.
    current = aggregate(f, [row], metric).subquery()
    then = aggregate(previous, [row], metric).subquery()
    names = (
        "value",
        "orders",
        "lines",
        "supplier_count",
        "amount_paid",
        "amount_inc",
        "amount_ex",
    )
    union = (
        select(
            current.c[row],
            *(current.c[n] for n in names),
            literal(None).label("prev"),
            literal(0).label("prev_orders"),
        )
        .union_all(
            select(
                then.c[row],
                *(literal(None) for _ in names),
                then.c.value,
                then.c.orders,
            )
        )
        .subquery()
    )
    now_v, prev_v = func.max(union.c.value), func.max(union.c.prev)
    # A missing period contributes zero for an additive quantity; it has no per-order average.
    if metric != "avgOrder":
        now_v = case((func.max(union.c.orders).is_(None), 0), else_=now_v)
        prev_v = case((func.max(union.c.prev_orders) == 0, 0), else_=prev_v)
    if metric == "qty" and row == "unit":
        now_v = case((union.c[row].is_not(None), now_v))
        prev_v = case((union.c[row].is_not(None), prev_v))
    grouped = select(
        union.c[row],
        now_v.label("value"),
        *(
            func.coalesce(func.max(union.c[n]), 0).label(n)
            for n in ("orders", "lines", "supplier_count")
        ),
        *(func.max(union.c[n]).label(n) for n in names[4:]),
        prev_v.label("previous_value"),
        func.max(union.c.prev_orders).label("previous_orders"),
        (now_v - prev_v).label("change"),
    ).group_by(union.c[row])
    results, out["total_rows"] = ranked_page(db, grouped, row, page_size, page, sort, value_max)
    dictionary = labels(db, row, [rec[row] for rec in results])
    for rec in results:
        key = rec[row]
        name, description = dictionary.get(key, (key if key is not None else null_label(row), None))
        prev, delta = rec["previous_value"], rec["change"]
        out["rows"].append(
            {
                **pack(rec, ctx, {**drill, row: key}),
                "key": key,
                "label": name,
                "description": description,
                "previous_value": pa._f(prev),
                "previous_orders": rec["previous_orders"],
                "change": pa._f(delta),
                "change_rate": float(delta / prev)
                if prev and prev > 0 and delta is not None
                else None,
            }
        )

    return out


def shared_pns(db, ctx, filters, scope, supplier_id, page, page_size):
    facts_query, _ = facts(filters, scope, ["supplier", "pn"], db=db)
    f = facts_query.subquery()
    pairs = (
        select(f.c.supplier_identity.label("supplier_id"), f.c.pn)
        .distinct()
        .where(f.c.pn.is_not(None), f.c.supplier_identity.is_not(None))
        .cte()
    )
    center = select(pairs.c.pn).where(pairs.c.supplier_id == supplier_id)
    matches = (
        select(
            pairs,
            func.count().over(partition_by=pairs.c.supplier_id).label("shared_pns"),
        )
        .where(pairs.c.supplier_id != supplier_id, pairs.c.pn.in_(center))
        .subquery()
    )
    total = db.scalar(select(func.count()).select_from(matches))
    rows = (
        db.execute(
            select(matches)
            .order_by(matches.c.shared_pns.desc(), matches.c.supplier_id, matches.c.pn)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .mappings()
        .all()
    )
    names = labels(db, "supplier", [r["supplier_id"] for r in rows])
    parts = labels(db, "pn", [r["pn"] for r in rows])
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "supplier_id": str(r["supplier_id"]),
                "supplier": names[str(r["supplier_id"])][0],
                "pn": r["pn"],
                "description": parts.get(r["pn"], (None, None))[1],
                "shared_pns": r["shared_pns"],
                "center_scope": {**scope, "supplier": str(supplier_id), "pn": r["pn"]},
                "peer_scope": {
                    **scope,
                    "supplier": str(r["supplier_id"]),
                    "pn": r["pn"],
                },
            }
            for r in rows
        ],
    }


def concentration(db, ctx, filters, scope, page, page_size):
    q, _ = facts(filters, scope, ["supplier", "pn", "unit"], db=db)
    f = q.subquery()
    # Only a complete, nonnegative, unit-comparable group can yield a meaningful sourcing share.
    g = (
        select(
            f.c.pn,
            f.c.unit,
            f.c.supplier_identity,
            func.sum(f.c.qty).label("qty"),
            func.count(f.c.qty).label("known"),
            func.count().label("n"),
            func.min(f.c.qty).label("minimum"),
        )
        .where(f.c.pn.is_not(None), f.c.unit.is_not(None))
        .group_by(f.c.pn, f.c.unit, f.c.supplier_identity)
        .subquery()
    )
    window = [g.c.pn, g.c.unit]
    ranked = select(
        g,
        func.sum(g.c.qty).over(partition_by=window).label("total_qty"),
        func.sum(g.c.known - g.c.n).over(partition_by=window).label("missing_check"),
        func.min(g.c.minimum).over(partition_by=window).label("min_qty"),
        func.row_number()
        .over(
            partition_by=window,
            order_by=[
                g.c.supplier_identity.is_(None),
                g.c.qty.desc().nullslast(),
                g.c.supplier_identity,
            ],
        )
        .label("position"),
    ).subquery()
    ratio = (ranked.c.qty / func.nullif(ranked.c.total_qty, 0)).label("share")
    result = (
        select(ranked, ratio).where(
            ranked.c.position == 1,
            ranked.c.supplier_identity.is_not(None),
            ranked.c.total_qty > 0,
            ranked.c.qty > 0,
            ranked.c.min_qty >= 0,
            ranked.c.missing_check == 0,
        )
    ).subquery()
    total = db.scalar(select(func.count()).select_from(result))
    rows = (
        db.execute(
            select(result)
            .order_by(result.c.share.desc(), result.c.pn, result.c.unit)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .mappings()
        .all()
    )
    names = labels(db, "supplier", [r["supplier_identity"] for r in rows])
    parts = labels(db, "pn", [r["pn"] for r in rows])
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "pn": r["pn"],
                "description": parts.get(r["pn"], (None, None))[1],
                "unit": r["unit"],
                "share": float(r["share"]),
                "leading_supplier": names[str(r["supplier_identity"])][0],
                "supplier_id": str(r["supplier_identity"]),
                "leading_qty": float(r["qty"]),
                "total_qty": float(r["total_qty"]),
                "scope": {**scope, "pn": r["pn"], "unit": r["unit"]},
            }
            for r in rows
        ],
    }
