"""Permission-scoped maintenance exploration from one SQL statement snapshot.

The three fact streams are UNIONed, never joined to each other. SQL reduces
facts to identity/project/attribution cells; Decimal and distinct order sets
then serve rankings, drilldowns and the matrix from exactly that same result.
"""
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import Date, Integer, Numeric, String, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine as L, FMaintenanceOrder as O, MaintenanceManualCostOverride as M
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine as R
from app.models.maintenance_project import MaintenanceProject as P
from app.models.maintenance_project_operations import MaintenanceSiteIssue as I, MaintenanceSiteIssueLine as IL
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as A
from app.services import maintenance_analytics as legacy, maintenance_cost_quality, query_filters
from app.services.maintenance_boss_board import BUSINESS_TYPE_LABELS, _business_type_clause

DIMENSIONS = ("pn", "project", "customer", "salesperson", "business")
METRICS = ("issued", "cost", "effective", "orders", "missing")
ZERO = Decimal("0")
UNKNOWN = "__unknown__"
UNASSIGNED = "__unassigned__"
BUSINESS_LABELS = {**BUSINESS_TYPE_LABELS, "other": "非维保", "unlabeled": "未标注", "unassigned": "未归属"}


def _business():
    trimmed = func.btrim(P.business_type)
    return case(
        (P.project_id.is_(None), "unassigned"),
        (or_(P.business_type.is_(None), trimmed == ""), "unlabeled"),
        *[(trimmed == label, code) for code, label in BUSINESS_TYPE_LABELS.items()],
        else_="other",
    )


def _cost():
    return maintenance_cost_quality.sql_normalized_line_cost(
        source_column=L.cost_source, tax_basis_column=L.cost_tax_basis,
        legacy_amount_column=L.cost_amount, normalized_amount_column=L.cost_amount_inc_tax,
        normalized_basis="inc", anomaly_flags_column=L.anomaly_flags,
        qty_column=L.qty, return_qty_column=L.return_qty,
        manual_unit_cost_column=M.unit_cost_inc_tax, manual_active_column=M.active,
    )


def _number(value):
    return cast(literal(value), Numeric)


def _raw_columns(*, kind, project_id, part_id, pn, description, business_date,
                 source_order_id, order_id=None, issued=ZERO, received=ZERO,
                 effective=ZERO, cost=None, missing=0, customer_visible=True):
    """All branches have identical typed columns, including nullable identities."""
    return [
        literal(kind).label("kind"), project_id.label("project_id"),
        part_id.label("part_id"), pn.label("raw_pn"), description.label("raw_description"),
        P.project_code.label("project_code"), P.display_name.label("project_name"),
        _business().label("business"),
        (func.nullif(func.btrim(O.end_customer), "") if customer_visible else cast(literal(None), String)).label("customer"),
        legacy._effective_salesperson().label("salesperson"),
        business_date.label("business_date"), source_order_id.label("source_order_id"),
        (order_id if order_id is not None else cast(literal(None), Integer)).label("order_id"),
        (issued if kind == "issue" else _number(0)).label("issued_qty"),
        (received if kind == "receipt" else _number(0)).label("receipt_qty"),
        (effective if kind == "demand" else _number(0)).label("effective_qty"),
        (cost if kind == "demand" else cast(literal(None), Numeric)).label("cost_inc"),
        (case((missing, 1), else_=0) if kind == "demand" else literal(0)).label("missing_lines"),
    ]


def _scope(stmt, project_column, *, allowed, project_ids, business_clause):
    if allowed is not None:
        stmt = stmt.where(project_column.in_(allowed))
    if project_ids is not None:
        stmt = stmt.where(project_column.in_(project_ids))
    if business_clause is not None:
        stmt = stmt.where(P.project_id.is_not(None), business_clause)
    return stmt


def build_statement(*, start, end, q, business_type, project_ids, customer,
                    salesperson, order_no, demand_types, warehouses, cost_sources,
                    allowed_project_ids, can_cost, can_customer):
    """Public for EXPLAIN/performance checks; execution is exactly one SELECT."""
    ci, _, _, missing = _cost()
    business_clause = _business_type_clause(business_type)
    scope = dict(allowed=allowed_project_ids, project_ids=project_ids, business_clause=business_clause)
    demand = select(*_raw_columns(
        kind="demand", project_id=A.project_id, part_id=L.part_id, pn=L.pn_std,
        description=L.description, business_date=O.order_date,
        source_order_id=O.raw_order_id, order_id=O.id,
        effective=func.coalesce(L.qty, ZERO) - func.coalesce(L.return_qty, ZERO),
        cost=ci if can_cost else cast(literal(None), Numeric), missing=missing,
        customer_visible=can_customer,
    )).select_from(L).join(O, O.id == L.order_id).outerjoin(
        M, and_(M.line_id == L.id, M.active.is_(True)),
    ).outerjoin(A, and_(A.source_order_id == O.raw_order_id, A.is_active.is_(True))).outerjoin(
        P, P.project_id == A.project_id,
    ).where(L.is_active.is_(True))
    demand = _scope(query_filters.active_orders(demand, O), A.project_id, **scope)
    if customer:
        demand = demand.where(O.end_customer.icontains(customer, autoescape=True))
    if salesperson:
        demand = demand.where(legacy._effective_salesperson().icontains(salesperson, autoescape=True))
    if order_no:
        demand = demand.where(O.order_no.icontains(order_no, autoescape=True))
    if demand_types:
        demand = demand.where(O.demand_type.in_(demand_types))
    if warehouses:
        demand = demand.where(O.warehouse.in_(warehouses))
    cost_clause = legacy._cost_source_clause(cost_sources)
    if cost_clause is not None:
        demand = demand.where(cost_clause)
    # This CTE deliberately has no date restriction: linked issues/receipts use
    # their own business date even when the source demand is outside the window.
    matched_demand = demand.cte("explorer_demand")
    valid_sources = query_filters.active_orders(
        select(O.raw_order_id.label("source_order_id"), A.project_id).select_from(O).join(
            A, and_(A.source_order_id == O.raw_order_id, A.is_active.is_(True)),
        ), O,
    ).cte("explorer_valid_sources")

    def valid_source(source_column, project_column):
        # A transferred or void source must not reveal another project's
        # customer/salesperson through an older issue or receipt.
        return select(1).select_from(valid_sources).where(
            valid_sources.c.source_order_id == source_column,
            valid_sources.c.project_id == project_column,
        ).correlate(I, IL, R).exists()

    issue = select(*_raw_columns(
        kind="issue", project_id=I.project_id, part_id=IL.part_id, pn=IL.pn,
        description=cast(literal(None), String), business_date=I.issue_date,
        source_order_id=IL.source_order_id, issued=IL.quantity,
        customer_visible=can_customer,
    )).select_from(IL).join(I, I.issue_id == IL.issue_id).outerjoin(
        P, P.project_id == I.project_id,
    ).outerjoin(O, and_(O.raw_order_id == IL.source_order_id, valid_source(IL.source_order_id, I.project_id))).where(
        I.status_mapping_state == "mapped", I.normalized_status.in_(("confirmed", "corrected")),
        IL.is_active.is_(True),
    )
    issue = _scope(issue, I.project_id, **scope)
    receipt = select(*_raw_columns(
        kind="receipt", project_id=R.project_id, part_id=R.part_id, pn=R.pn,
        description=R.description, business_date=cast(func.timezone("Asia/Shanghai", R.occurred_at), Date),
        source_order_id=R.source_order_id, received=R.qty,
        customer_visible=can_customer,
    )).select_from(R).outerjoin(P, P.project_id == R.project_id).outerjoin(
        O, and_(O.raw_order_id == R.source_order_id, valid_source(R.source_order_id, R.project_id)),
    ).where(R.line_status == "active")
    receipt = _scope(receipt, R.project_id, **scope)
    raw = union_all(select(matched_demand), issue, receipt).cte("explorer_raw")

    normalized = func.upper(func.btrim(DimPart.pn_std))
    unique_pn = select(normalized.label("pn"), func.min(DimPart.id).label("part_id")).group_by(
        normalized,
    ).having(func.count(DimPart.id) == 1).cte("explorer_unique_pn")
    raw_pn = func.upper(func.btrim(func.coalesce(raw.c.raw_pn, "")))
    identity = func.coalesce(raw.c.part_id, unique_pn.c.part_id)
    pn = func.coalesce(DimPart.pn_std, raw_pn)
    desc = func.coalesce(DimPart.description, raw.c.raw_description)
    conditions = []
    if start is not None:
        conditions.append(raw.c.business_date >= start)
    if end is not None:
        conditions.append(raw.c.business_date <= end)
    if q and q.strip():
        term = q.strip()
        conditions.append(or_(pn.icontains(term, autoescape=True), raw.c.raw_pn.icontains(term, autoescape=True),
                              desc.icontains(term, autoescape=True), raw.c.project_name.icontains(term, autoescape=True)))
    if customer or salesperson or order_no or demand_types or warehouses or cost_clause is not None:
        source_conditions = [matched_demand.c.source_order_id == raw.c.source_order_id,
                             matched_demand.c.project_id == raw.c.project_id]
        if cost_clause is not None:
            source_conditions.extend((matched_demand.c.project_id == raw.c.project_id,
                                      matched_demand.c.part_id == identity))
        source_match = select(1).select_from(matched_demand).where(*source_conditions).correlate(raw, unique_pn).exists()
        conditions.append(or_(raw.c.kind == "demand", source_match))
    identity_fields = [identity.label("part_id"), pn.label("pn"), raw.c.project_id,
                       raw.c.customer, raw.c.salesperson, raw.c.business]
    stmt = select(
        *identity_fields, func.max(desc).label("description"),
        func.max(raw.c.project_code).label("project_code"),
        func.max(raw.c.project_name).label("project_name"),
        func.sum(raw.c.issued_qty).label("issued_qty"),
        func.sum(raw.c.receipt_qty).label("receipt_qty"),
        func.sum(raw.c.effective_qty).label("effective_qty"),
        func.sum(raw.c.cost_inc).label("cost_inc"),
        func.sum(raw.c.missing_lines).label("missing_lines"),
        func.count().filter(raw.c.kind == "demand").label("demand_count"),
        func.count().filter(raw.c.kind == "issue").label("issue_count"),
        func.count(raw.c.cost_inc).label("known_count"),
        func.count().filter(and_(raw.c.kind == "demand", raw.c.effective_qty < ZERO)).label("negative_effective_count"),
        func.array_agg(func.distinct(raw.c.order_id)).filter(raw.c.order_id.is_not(None)).label("orders"),
        func.statement_timestamp().label("as_of"),
    ).select_from(raw).outerjoin(
        unique_pn, and_(raw.c.part_id.is_(None), unique_pn.c.pn == raw_pn),
    ).outerjoin(DimPart, DimPart.id == identity).where(*conditions).group_by(*identity_fields)
    return stmt


def _key(row, dimension):
    if dimension == "pn":
        return f"part:{row['part_id']}" if row["part_id"] is not None else f"pn:{row['pn']}"
    if dimension == "project":
        return row["project_id"] or UNASSIGNED
    return row[dimension] if row[dimension] is not None else UNKNOWN


def _has_metric(row, metric):
    return row["issue_count"] > 0 if metric == "issued" else row["demand_count"] > 0


def _negative(records, metric):
    return metric == "effective" and any(row["negative_effective_count"] for row in records)


def _decimal(value):
    return format(value, "f") if value is not None else None


def _stats(records, metric, can_cost):
    issued = effective = received = cost = ZERO
    missing = known = 0
    orders, projects, parts = set(), set(), set()
    for record in records:
        issued += record["issued_qty"] or ZERO
        received += record["receipt_qty"] or ZERO
        effective += record["effective_qty"] or ZERO
        cost += record["cost_inc"] or ZERO
        missing += record["missing_lines"] or 0
        known += record["known_count"]
        orders.update(record["orders"] or ())
        if _has_metric(record, metric):
            if record["project_id"]:
                projects.add(record["project_id"])
            if record["pn"]:
                parts.add(_key(record, "pn"))
    amount = cost if known and can_cost else None
    value = {"issued": issued, "effective": effective, "cost": amount,
             "orders": Decimal(len(orders)), "missing": Decimal(missing)}[metric]
    return {
        "value": _decimal(value), "issued_qty": _decimal(issued), "effective_qty": _decimal(effective),
        "receipt_qty": _decimal(received), "cost_inc": _decimal(amount),
        "cost_state": "restricted" if not can_cost else "unknown" if not known else "partial" if missing else "known",
        "order_count": len(orders), "missing_lines": missing, "project_count": len(projects), "pn_count": len(parts),
    }


def _identity(first, key, dimension):
    result = {"key": key, "part_id": None, "project_id": None, "pn": None}
    if dimension == "pn":
        result.update(label=first["pn"] or "未识别 PN", subtitle=first["description"] or "暂无规格描述",
                      part_id=first["part_id"], pn=first["pn"] or None)
    elif dimension == "project":
        result.update(label=first["project_name"] or "未归属项目",
                      subtitle=first["project_code"] or "来源需求尚未关联稳定项目", project_id=first["project_id"])
    elif dimension == "business":
        result.update(label=BUSINESS_LABELS.get(key, key), subtitle="按当前项目业务类型汇总")
    else:
        label = ("未明确客户" if dimension == "customer" else "未标注销售") if key == UNKNOWN else key
        result.update(label=label, subtitle="按来源需求单客户汇总" if dimension == "customer" else "按有效销售归属汇总")
    return result


def _rank(records, dimension, metric, can_cost):
    groups = defaultdict(list)
    for row in records:
        groups[_key(row, dimension)].append(row)
    ranked = []
    for key, group in groups.items():
        if any(_has_metric(row, metric) for row in group):
            ranked.append({**_identity(group[0], key, dimension), **_stats(group, metric, can_cost),
                           "share_pct": None, "cumulative_share_pct": None})
    ranked.sort(key=lambda row: (row["value"] is None, -Decimal(row["value"] or "0"), row["label"], row["key"]))
    additive = metric != "orders"
    negative = _negative(records, metric)
    total = sum((Decimal(row["value"] or "0") for row in ranked), ZERO)
    if additive and not negative and total > ZERO:
        cumulative = ZERO
        for row in ranked:
            if row["value"] is not None:
                cumulative += Decimal(row["value"])
                row["share_pct"] = _decimal((Decimal(row["value"]) / total * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
                row["cumulative_share_pct"] = _decimal((cumulative / total * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return ranked


def _page(rows, page, size):
    page = min(page, max(1, (len(rows) + size - 1) // size))
    return rows[(page - 1) * size:page * size], page


def explorer(db: Session, *, dimension="pn", metric="issued", focus=None,
             page=1, page_size=20, top_n=20, focus_page=1, focus_page_size=30,
             range_="ytd", date_from: date | None = None, date_to: date | None = None,
             q=None, business_type="all", project_ids=None, customer=None, salesperson=None,
             order_no=None, demand_types=None, warehouses=None, cost_sources=None,
             can_cost: bool, can_customer: bool = True, allowed_project_ids=None):
    if dimension not in DIMENSIONS or metric not in METRICS:
        raise legacy.AnalyticsValidationError("不支持的排名维度或指标")
    if not can_cost and metric == "cost":
        raise legacy.AnalyticsValidationError("查看成本排名需要成本查看权限（data_purchase_cost）")
    if not can_customer and (dimension == "customer" or customer):
        raise legacy.AnalyticsValidationError("查看客户维度或按客户筛选需要客户信息权限（data_customer）")
    if min(page, page_size, top_n, focus_page, focus_page_size) < 1 or max(page_size, focus_page_size) > 100 or top_n > 30:
        raise legacy.AnalyticsValidationError("分页或图表数量超出允许范围")
    start, end = legacy.resolve_window(range_, date_from, date_to)
    statement = build_statement(
        start=start, end=end, q=q, business_type=business_type, project_ids=project_ids,
        customer=customer, salesperson=salesperson, order_no=order_no, demand_types=demand_types,
        warehouses=warehouses, cost_sources=cost_sources, allowed_project_ids=allowed_project_ids,
        can_cost=can_cost, can_customer=can_customer,
    )
    records = list(db.execute(statement).mappings())
    ranks = _rank(records, dimension, metric, can_cost)
    summary = _stats(records, metric, can_cost)
    # Fixed Top 10 KPI: unknown-cost groups at the tail do not erase the known
    # cumulative contribution. No shares exist for orders/negative/zero totals.
    summary["top_share_pct"] = next((row["cumulative_share_pct"] for row in reversed(ranks[:10])
                                     if row["cumulative_share_pct"] is not None), None)
    selected = next((row for row in ranks if row["key"] == focus), None) if focus is not None else next(iter(ranks), None)
    detail_dimension = "pn" if dimension == "project" else "project"
    selected_records = [row for row in records if selected and _key(row, dimension) == selected["key"]]
    detail = _rank(selected_records, detail_dimension, metric, can_cost)
    detail_page, actual_focus_page = _page(detail, focus_page, focus_page_size)
    paged_rows, actual_page = _page(ranks, page, page_size)
    chart_rows = ranks[:top_n]
    columns = [row for row in _rank(records, detail_dimension, metric, can_cost) if row["key"] != UNASSIGNED][:8]
    row_keys = {row["key"] for row in chart_rows}
    column_keys = {row["key"] for row in columns}
    cell_records = defaultdict(list)
    for row in records:
        a, b = _key(row, dimension), _key(row, detail_dimension)
        if a in row_keys and b in column_keys:
            cell_records[(a, b)].append(row)
    cells = []
    for (a, b), group in cell_records.items():
        if any(_has_metric(row, metric) for row in group):
            stats = _stats(group, metric, can_cost)
            cells.append({"row_key": a, "column_key": b, "value": stats["value"], "cost_state": stats["cost_state"]})
    values = [Decimal(cell["value"]) for cell in cells if cell["value"] is not None]
    return {
        "window": {"range": range_, "date_from": start.isoformat() if start else None, "date_to": end.isoformat() if end else None},
        "dimension": dimension, "metric": metric, "total": len(ranks), "page": actual_page, "page_size": page_size,
        "summary": summary, "rows": paged_rows, "chart_rows": chart_rows,
        "focus": {"row": selected, "dimension": detail_dimension, "rows": detail_page,
                  "total": len(detail), "page": actual_focus_page, "page_size": focus_page_size},
        "matrix": {"row_dimension": dimension, "column_dimension": detail_dimension,
                   "rows": chart_rows, "columns": columns, "cells": cells,
                   "scale_max": _decimal(max(values, default=ZERO)), "scale_min": _decimal(min(values, default=ZERO))},
        "meta": {"as_of": (records[0]["as_of"] if records else datetime.now(timezone.utc)).isoformat(),
                 "quantity_scale": 3, "cost_basis": "inc", "additive": metric != "orders",
                 "has_negative": _negative(records, metric),
                 "cost_visibility": "visible" if can_cost else "restricted",
                 "customer_visibility": "visible" if can_customer else "restricted",
                 "unknown_count": sum(row["value"] is None for row in ranks)},
    }
