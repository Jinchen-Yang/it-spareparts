"""Read-only warehouse activity analysis at document and matching spare-part line grain."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy import Date, String, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models.warehouse_analytics import WarehouseAnalyticsDocument as Document
from app.models.warehouse_analytics import WarehouseAnalyticsLine as Line

SOURCE_NAMES = {"inbound": "入库单", "return": "返库单", "direct": "直采直退", "outbound": "实际出库单"}
LINE_DIMENSIONS = {"pn", "unit", "category", "brand", "warehouse", "bin_name", "test_result"}
DIMENSIONS = LINE_DIMENSIONS | {
    "day",
    "week",
    "source",
    "center",
    "kind",
    "operator",
    "operator_role",
    "tester",
    "whole_pn",
    "whole_unit",
}
MEASURES = {"documents", "lines", "pns", "quantity", "active_days"}


class InvalidQuery(ValueError):
    pass


@dataclass(frozen=True)
class Filters:
    date_from: date | None
    date_to: date | None
    # Exact values from the page and the nested drill are intersected, even for the same key.
    exact: tuple[tuple[str, str | None], ...]
    q: str | None
    testing: str | None
    require_lines: bool = False

    def has_unit(self):
        return any(key == "unit" and value for key, value in self.exact)


def read_filters(date_from, date_to, fields, scope, q, testing, require_lines=False):
    if date_from and date_to and date_from > date_to:
        raise InvalidQuery("开始日期不能晚于结束日期")
    exact = [(k, v) for k, v in fields.items() if v is not None]
    if scope is not None:
        try:
            parsed = json.loads(scope)
        except (ValueError, RecursionError) as exc:
            raise InvalidQuery("筛选范围必须为 JSON 对象") from exc
        if not isinstance(parsed, dict) or any(
            k not in DIMENSIONS or (v is not None and (not isinstance(v, str) or len(v) > 256))
            for k, v in parsed.items()
        ):
            raise InvalidQuery("筛选范围包含无效的维度或取值")
        exact.extend(parsed.items())
    for key, value in exact:
        if value is None:
            continue
        if key == "source" and value not in SOURCE_NAMES:
            raise InvalidQuery("未知来源")
        if key == "operator_role" and value not in ("pick", "direct", "outbound"):
            raise InvalidQuery("未知作业岗位")
        if key in ("day", "week"):
            try:
                d = date.fromisoformat(value)
            except ValueError as exc:
                raise InvalidQuery("日期请使用 YYYY-MM-DD") from exc
            if value != d.isoformat() or (key == "week" and d.weekday() != 0):
                raise InvalidQuery("周筛选需使用周一的业务日期")
    return Filters(date_from, date_to, tuple(exact), (q or "").strip() or None, testing, require_lines)


def _role():
    return case(
        (Document.source == "direct", "direct"),
        (Document.source == "outbound", "outbound"),
        else_="pick",
    )


def _scope_columns():
    return {
        "day": cast(Document.business_date, String),
        "week": cast(cast(func.date_trunc("week", Document.business_date), Date), String),
        "source": Document.source,
        "kind": Document.kind,
        "center": Document.center,
        "operator": Document.operator,
        "operator_role": _role(),
        "tester": Document.tester,
        "whole_pn": Document.whole_pn,
        "whole_unit": Document.whole_unit,
        **{key: getattr(Line, key) for key in LINE_DIMENSIONS},
    }


def _test_condition(testing):
    required = Document.needs_test.is_(True)
    return {
        "required": required,
        "completed": and_(required, Document.tested.is_(True)),
        "pending": and_(required, Document.tested.is_(False)),
        "unknown": and_(required, Document.tested.is_(None)),
        "attributed": and_(required, Document.tested.is_(True), Document.tester.is_not(None)),
    }[testing]


def _operator_filters(filters):
    if any(key in ("operator_role", "source") for key, _ in filters.exact):
        return filters
    # The same name may be a picker as well as a direct-return handler.
    return replace(filters, exact=(*filters.exact, ("operator_role", "pick")))


def _facts(filters: Filters, *, require_lines=False, document_id=None):
    if any(key == "operator" for key, _ in filters.exact):
        filters = _operator_filters(filters)
    dims = _scope_columns()
    conditions = [Document.status == "已生效"]
    if document_id is not None:
        conditions.append(Document.id == document_id)
    if filters.date_from:
        conditions.append(Document.business_date >= filters.date_from)
    if filters.date_to:
        conditions.append(Document.business_date <= filters.date_to)
    if filters.q:
        conditions.append(
            or_(
                *[
                    col.icontains(filters.q, autoescape=True)
                    for col in (Document.number, Line.pn, Line.description)
                ]
            )
        )
    if filters.testing:
        conditions.append(_test_condition(filters.testing))
    for key, value in filters.exact:
        conditions.append(dims[key] == value)
    # A real line lacking e.g. a warehouse belongs to the unknown bucket. A document
    # without any spare-part lines still counts on head-only charts.
    if require_lines or filters.require_lines or any(k in LINE_DIMENSIONS for k, _ in filters.exact):
        conditions.append(Line.id.is_not(None))
    return (
        select(
            Document.id.label("doc_id"),
            Document.business_date,
            Document.whole_qty,
            Document.whole_description,
            Line.id.label("line_id"),
            Line.quantity,
            Line.description,
            *(col.label(key) for key, col in sorted(dims.items())),
        )
        .select_from(Document)
        .outerjoin(Line, Line.document_id == Document.id)
        .where(*conditions)
        .subquery()
    )


def _measures(f):
    return {
        "documents": func.count(func.distinct(f.c.doc_id)),
        "lines": func.count(f.c.line_id),
        "pns": func.count(func.distinct(f.c.pn)),
        "quantity": func.sum(f.c.quantity),
        "active_days": func.count(func.distinct(f.c.business_date)),
    }


def _validate_metric(filters, metric):
    if metric not in MEASURES:
        raise InvalidQuery("未知统计指标")
    if metric == "quantity" and not filters.has_unit():
        raise InvalidQuery("比较数量时请选择一个明确的单位")


def _label(dim, key):
    if key is None:
        return "未填"
    if dim == "source":
        return SOURCE_NAMES[key]
    if dim == "operator_role":
        return {"pick": "拣货", "direct": "直退经办", "outbound": "实际出库"}[key]
    return key


def _description(f, dim):
    return func.min(f.c.description) if dim == "pn" else func.min(f.c.whole_description)


def _axis(db, f, dim, metric, limit):
    key = f.c[dim]
    measure = _measures(f)[metric]
    cols = [key.label("key"), measure.label("value"), func.count().over().label("total_groups")]
    if dim in ("pn", "whole_pn"):
        cols.append(_description(f, dim).label("description"))
    order = (
        [key.desc().nulls_last()]
        if dim in ("day", "week")
        else [measure.desc().nulls_last(), key.asc().nulls_last()]
    )
    rows = db.execute(select(*cols).group_by(key).order_by(*order).limit(limit)).mappings().all()
    total = rows[0]["total_groups"] if rows else 0
    if dim in ("day", "week"):
        rows = sorted(rows, key=lambda r: (r["key"] is None, r["key"] or ""))
    return [
        {
            "key": r["key"],
            "label": _label(dim, r["key"]),
            "value": r["value"],
            "description": r.get("description"),
        }
        for r in rows
    ], total


def _in_window(column, groups):
    keys = [r["key"] for r in groups]
    values = column.in_([k for k in keys if k is not None])
    return or_(values, column.is_(None)) if None in keys else values


def cube(db, filters, row, col, metric, row_limit, col_limit):
    _validate_metric(filters, metric)
    if row not in DIMENSIONS or (col is not None and (col not in DIMENSIONS or col == row)):
        raise InvalidQuery("请选择有效且不同的行、列维度")
    if "operator" in (row, col):
        filters = _operator_filters(filters)
    f = _facts(filters, require_lines=row in LINE_DIMENSIONS or col in LINE_DIMENSIONS)
    measure = _measures(f)[metric]
    rows, total_rows = _axis(db, f, row, metric, row_limit)
    columns, total_columns, cells = [], 0, []
    if col is not None:
        columns, total_columns = _axis(db, f, col, metric, col_limit)
        if rows and columns:
            cell_query = select(
                f.c[row].label("row_key"), f.c[col].label("col_key"), measure.label("value")
            )
            cells = (
                db.execute(
                    cell_query.where(
                        _in_window(f.c[row], rows), _in_window(f.c[col], columns)
                    ).group_by(f.c[row], f.c[col])
                )
                .mappings()
                .all()
            )
    return {
        "rows": rows,
        "columns": columns,
        "cells": [dict(r) for r in cells],
        "total": db.scalar(select(measure)),
        "total_rows": total_rows,
        "total_columns": total_columns,
    }


def coverage(db: Session):
    default = Filters(None, None, (), None, None)
    f = _facts(default)
    effective = Document.status == "已生效"
    rows = db.execute(
        select(
            Document.source,
            func.min(Document.business_date).label("min_date"),
            func.max(Document.business_date).label("max_date"),
            func.count(func.distinct(Document.id)).filter(effective).label("documents"),
            func.count(Line.id).filter(effective).label("lines"),
        )
        .outerjoin(Line, Line.document_id == Document.id)
        .group_by(Document.source)
    ).mappings()
    by_source = {r["source"]: dict(r) for r in rows}
    sources = []
    for source in SOURCE_NAMES:
        entry = by_source.get(
            source,
            {"source": source, "min_date": None, "max_date": None, "documents": None, "lines": None},
        )
        sources.append({**entry, "available": source in by_source})
    options = {"sources": [source for source in SOURCE_NAMES if source in by_source]}
    for field, name in (("center", "centers"), ("unit", "units")):
        col = f.c[field]
        options[name] = list(db.scalars(select(col).where(col.is_not(None)).distinct().order_by(col)))
    return {"sources": sources, "options": options}


def rankings(db, filters, by, metric, page, page_size):
    _validate_metric(filters, metric)
    if by == "operator":
        filters = _operator_filters(filters)
    f = _facts(filters, require_lines=by == "pn")
    measures = _measures(f)
    if not filters.has_unit():
        measures["quantity"] = literal(None)
    key = f.c[by]
    grouped = select(key.label("key"), *(value.label(name) for name, value in measures.items()))
    if by == "pn":
        grouped = grouped.add_columns(func.min(f.c.description).label("description")).where(
            key.is_not(None)
        )
    g = grouped.group_by(key).subquery()
    value = g.c[metric]
    # Keep unattributed work discoverable without assigning it a personal rank.
    ranking_order = (g.c.key.is_(None), value.desc().nulls_last())
    ranked = select(g, func.rank().over(order_by=ranking_order).label("place"))
    found = (
        db.execute(
            ranked.order_by(*ranking_order, g.c.key.asc().nulls_last())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        .mappings()
        .all()
    )
    items = []
    for data in found:
        r = dict(data)
        place = r.pop("place")
        r.update(
            label=_label(by, r["key"]),
            rank=place if r["key"] is not None and r[metric] is not None else None,
        )
        items.append(r)
    result = {"items": items, "total": db.scalar(select(func.count()).select_from(g))}
    if by == "pn":
        # An independent line-weighted Top 10 and all matching PN are the numerator and denominator.
        top = select(g.c.lines).order_by(g.c.lines.desc(), g.c.key).limit(10).subquery()
        stats = db.execute(
            select(
                func.sum(g.c.lines),
                func.count().filter(g.c.documents >= 2),
                select(func.sum(top.c.lines)).scalar_subquery(),
            )
        ).one()
        result.update(
            top10_line_share=float(stats[2] / stats[0]) if stats[0] else None, repeat_pns=stats[1]
        )
    return result


def _line_fields():
    return (
        Line.pn,
        Line.description,
        Line.quantity,
        Line.unit,
        Line.category,
        Line.brand,
        Line.warehouse,
        Line.bin_name,
        Line.test_result,
    )


def documents(db, filters, page, page_size):
    f = _facts(filters)
    grouped = (
        select(f.c.doc_id, func.count(f.c.line_id).label("matched_lines"))
        .group_by(f.c.doc_id)
        .subquery()
    )
    total = db.scalar(select(func.count()).select_from(grouped))
    total_lines = (
        select(func.count(Line.id))
        .where(Line.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )
    page_rows = db.execute(
        select(Document, grouped.c.matched_lines, total_lines)
        .join(grouped, grouped.c.doc_id == Document.id)
        .order_by(Document.business_date.desc().nulls_last(), Document.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    ids = [doc.id for doc, _, _ in page_rows]
    samples = {doc_id: [] for doc_id in ids}
    if ids:
        evidence = (
            select(
                f.c.doc_id,
                f.c.pn,
                f.c.description,
                f.c.quantity,
                f.c.unit,
                func.row_number().over(partition_by=f.c.doc_id, order_by=f.c.line_id).label("position"),
            )
            .where(f.c.doc_id.in_(ids), f.c.line_id.is_not(None))
            .subquery()
        )
        for r in db.execute(
            select(evidence)
            .where(evidence.c.position <= 3)
            .order_by(evidence.c.doc_id, evidence.c.position)
        ).mappings():
            samples[r["doc_id"]].append({k: r[k] for k in ("pn", "description", "quantity", "unit")})
    return {
        "total": total,
        "items": [
            {
                "id": h.id,
                "number": h.number,
                "source": h.source,
                "date": h.business_date,
                "kind": h.kind,
                "center": h.center,
                "operator": h.operator,
                "whole_pn": h.whole_pn,
                "whole_description": h.whole_description,
                "whole_qty": h.whole_qty,
                "whole_unit": h.whole_unit,
                "needs_test": h.needs_test,
                "tested": h.tested,
                "tester": h.tester,
                "tested_date": h.tested_date,
                "matched_lines": n,
                "total_lines": total_count,
                "sample": samples[h.id],
            }
            for h, n, total_count in page_rows
        ],
    }


def document_lines(db, filters, document_id, page, page_size):
    f = _facts(filters, document_id=document_id)
    count, n = db.execute(select(func.count(), func.count(f.c.line_id)).select_from(f)).one()
    if not count:
        return None
    fields = [f.c[col.key] for col in _line_fields()]
    rows = db.execute(
        select(*fields)
        .where(f.c.line_id.is_not(None))
        .order_by(f.c.line_id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).mappings()
    return {"items": [dict(r) for r in rows], "total": n}


def equipment(db, filters, page, page_size):
    f = _facts(filters)
    heads = (
        select(
            f.c.doc_id,
            f.c.whole_pn,
            f.c.whole_unit,
            f.c.whole_qty,
            f.c.whole_description,
            f.c.business_date,
            func.count(f.c.line_id).label("lines"),
        )
        .where(f.c.whole_pn.is_not(None))
        .group_by(
            f.c.doc_id,
            f.c.whole_pn,
            f.c.whole_unit,
            f.c.whole_qty,
            f.c.whole_description,
            f.c.business_date,
        )
        .subquery()
    )
    grouped = (
        select(
            heads.c.whole_pn,
            heads.c.whole_unit,
            func.min(heads.c.whole_description).label("whole_description"),
            func.count().label("documents"),
            func.sum(heads.c.whole_qty).filter(heads.c.whole_unit.is_not(None)).label("whole_qty"),
            func.sum(heads.c.lines).label("matched_lines"),
            func.max(heads.c.business_date).label("last_date"),
        )
        .group_by(heads.c.whole_pn, heads.c.whole_unit)
        .subquery()
    )
    total = db.scalar(select(func.count()).select_from(grouped))
    rows = db.execute(
        select(grouped)
        .order_by(
            grouped.c.documents.desc(), grouped.c.whole_pn, grouped.c.whole_unit.asc().nulls_last()
        )
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).mappings()
    return {"items": [dict(r) for r in rows], "total": total}


def testing(db, filters, by):
    f = _facts(filters)
    matched = select(f.c.doc_id).distinct().subquery()
    counts = [
        func.count().filter(_test_condition(test)).label(test)
        for test in ("required", "completed", "pending", "unknown", "attributed")
    ]
    stmt = select(*counts).select_from(Document).join(matched, matched.c.doc_id == Document.id)
    totals = dict(db.execute(stmt).mappings().one())
    key = getattr(Document, by)
    rows = db.execute(
        stmt.add_columns(key.label("key"))
        .where(Document.needs_test.is_(True))
        .group_by(key)
        .order_by(counts[0].desc(), key.asc().nulls_last())
    ).mappings()
    return {**totals, "items": [{**r, "label": _label(by, r["key"])} for r in rows]}
