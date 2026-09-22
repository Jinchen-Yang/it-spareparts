"""Read-only return/actual-issue facts shared by maintenance metrics.

Demand quantities, cost evidence, delivery identities and return obligations do
not define actual issued quantities. Return condition is an independent fact:
callers choose either all received parts or the separately reported bad parts.
"""

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.business_time import BUSINESS_TZ
from app.config import RKD_RETURN_TEST_RESULTS
from app.models.dimensions import DimPart
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)

_ZERO = Decimal("0")


class Totals(TypedDict):
    issued_qty: Decimal
    returned_qty: Decimal
    bad_returned_qty: Decimal


class PnTotals(Totals):
    part_id: int | None
    pn: str
    description: str | None
    project_ids: list[str]


@dataclass(frozen=True)
class _Fact:
    project_id: str
    part_id: int | None
    pn: str
    description: str | None
    issued_qty: Decimal = _ZERO
    returned_qty: Decimal = _ZERO
    bad_returned_qty: Decimal = _ZERO


def _empty_totals() -> Totals:
    return {"issued_qty": _ZERO, "returned_qty": _ZERO, "bad_returned_qty": _ZERO}


def _add(target: Totals, fact: _Fact) -> None:
    target["issued_qty"] += fact.issued_qty
    target["returned_qty"] += fact.returned_qty
    target["bad_returned_qty"] += fact.bad_returned_qty


def _facts(
    db: Session,
    *,
    project_ids: Collection[str] | None,
    date_from: date | None,
    date_to: date | None,
    source_order_ids: Collection[str] | None,
    demand_part_scope: Collection[tuple[str, str, int]] | None,
) -> list[_Fact]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("date_from must not be after date_to")
    if project_ids is not None and not project_ids:
        return []
    if source_order_ids is not None and not source_order_ids:
        return []
    if demand_part_scope is not None and not demand_part_scope:
        return []

    issue_filters = [
        MaintenanceSiteIssue.status_mapping_state == "mapped",
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
        MaintenanceSiteIssueLine.is_active.is_(True),
    ]
    receipt_filters = [MaintenanceRkdReturnLine.line_status == "active"]
    if project_ids is not None:
        issue_filters.append(MaintenanceSiteIssue.project_id.in_(project_ids))
        receipt_filters.append(MaintenanceRkdReturnLine.project_id.in_(project_ids))
    if source_order_ids is not None:
        issue_filters.append(MaintenanceSiteIssueLine.source_order_id.in_(source_order_ids))
        receipt_filters.append(MaintenanceRkdReturnLine.source_order_id.in_(source_order_ids))
    if demand_part_scope is not None:
        # A demand-line attribute (such as cost source) must not widen to every
        # PN in its order, or to the same PN in another matched order/project.
        issue_filters.append(tuple_(
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssueLine.source_order_id,
            MaintenanceSiteIssueLine.part_id,
        ).in_(demand_part_scope))
        # Receipt identities have priority over text. A raw PN is usable only
        # when the master contains exactly one matching canonical identity.
        # This correlated SQL expression does not issue per-receipt queries.
        fallback_part_id = (
            select(func.min(DimPart.id))
            .where(func.upper(func.btrim(DimPart.pn_std))
                   == func.upper(func.btrim(MaintenanceRkdReturnLine.pn)))
            .having(func.count(DimPart.id) == 1)
            .correlate(MaintenanceRkdReturnLine)
            .scalar_subquery()
        )
        receipt_part_id = case(
            (MaintenanceRkdReturnLine.part_id.is_not(None), MaintenanceRkdReturnLine.part_id),
            else_=fallback_part_id,
        )
        receipt_filters.append(tuple_(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.source_order_id,
            receipt_part_id,
        ).in_(demand_part_scope))
    # Card lifetime windows use date.min. Shanghai's positive offset would
    # otherwise create an unrepresentable year-zero UTC instant in the driver.
    if date_from is not None and date_from > date.min:
        issue_filters.append(MaintenanceSiteIssue.issue_date >= date_from)
        receipt_filters.append(
            MaintenanceRkdReturnLine.occurred_at
            >= datetime.combine(date_from, time.min, tzinfo=BUSINESS_TZ)
        )
    if date_to is not None:
        issue_filters.append(MaintenanceSiteIssue.issue_date <= date_to)
        if date_to < date.max:
            receipt_filters.append(
                MaintenanceRkdReturnLine.occurred_at
                < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=BUSINESS_TZ)
            )
        else:
            # No following Python date exists; still exclude undated receipts
            # when the caller explicitly requests a bounded period.
            receipt_filters.append(MaintenanceRkdReturnLine.occurred_at.is_not(None))

    facts = [
        _Fact(project_id, part_id, pn, None, issued_qty=Decimal(quantity))
        for project_id, part_id, pn, quantity in db.execute(
            select(
                MaintenanceSiteIssue.project_id,
                MaintenanceSiteIssueLine.part_id,
                MaintenanceSiteIssueLine.pn,
                func.sum(MaintenanceSiteIssueLine.quantity),
            )
            .join(MaintenanceSiteIssue, MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
            .where(*issue_filters)
            .group_by(
                MaintenanceSiteIssue.project_id,
                MaintenanceSiteIssueLine.part_id,
                MaintenanceSiteIssueLine.pn,
            )
        )
    ]
    facts.extend(
        _Fact(
            project_id, part_id, pn, description,
            returned_qty=Decimal(quantity), bad_returned_qty=Decimal(bad_quantity),
        )
        for project_id, part_id, pn, description, quantity, bad_quantity in db.execute(
            select(
                MaintenanceRkdReturnLine.project_id,
                MaintenanceRkdReturnLine.part_id,
                MaintenanceRkdReturnLine.pn,
                func.max(MaintenanceRkdReturnLine.description),
                func.sum(MaintenanceRkdReturnLine.qty),
                func.sum(case(
                    (MaintenanceRkdReturnLine.test_result.in_(RKD_RETURN_TEST_RESULTS),
                     MaintenanceRkdReturnLine.qty),
                    else_=_ZERO,
                )),
            )
            .where(*receipt_filters)
            .group_by(
                MaintenanceRkdReturnLine.project_id,
                MaintenanceRkdReturnLine.part_id,
                MaintenanceRkdReturnLine.pn,
            )
        )
    )
    return facts


def project_totals(
    db: Session,
    *,
    project_ids: Collection[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    source_order_ids: Collection[str] | None = None,
    demand_part_scope: Collection[tuple[str, str, int]] | None = None,
) -> dict[str, Totals]:
    """Both sides use the same scope/period; explicit projects include zero rows.

    ``None`` means no filter. An empty collection means no matching facts.
    ``demand_part_scope`` optionally limits linked facts to matching demand
    (project, order, part) identities; it does not split same-PN quantities by
    demand-line cost source or change any demand/cost facts.
    Undated returns participate only without a real bound; date.min is the
    lifetime lower-bound sentinel and imposes no lower date constraint.
    """
    result = {project_id: _empty_totals() for project_id in (project_ids or ())}
    for fact in _facts(
        db, project_ids=project_ids, date_from=date_from, date_to=date_to,
        source_order_ids=source_order_ids, demand_part_scope=demand_part_scope,
    ):
        _add(result.setdefault(fact.project_id, _empty_totals()), fact)
    return result


def pn_totals(
    db: Session,
    *,
    project_ids: Collection[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    source_order_ids: Collection[str] | None = None,
    demand_part_scope: Collection[tuple[str, str, int]] | None = None,
) -> list[PnTotals]:
    """Return union of actual-issue/receipt PNs, independent of demand presence.

    A recorded part identity wins over stale PN text. Missing identities fall
    back to a unique canonical PN (trimmed/case-insensitive), never fuzzy or
    substring matching. Ambiguous canonical PNs stay separate without guessing.
    Every fact contributes exactly once, including mixed identified/raw PNs.
    """
    facts = _facts(
        db, project_ids=project_ids, date_from=date_from, date_to=date_to,
        source_order_ids=source_order_ids, demand_part_scope=demand_part_scope,
    )
    if not facts:
        return []
    part_ids = {fact.part_id for fact in facts if fact.part_id is not None}
    fallback_pns = {fact.pn.strip().upper() for fact in facts if fact.part_id is None}
    parts = {
        row.id: row
        for row in db.execute(
            select(DimPart.id, DimPart.pn_std, DimPart.description).where(or_(
                DimPart.id.in_(part_ids),
                func.upper(func.btrim(DimPart.pn_std)).in_(fallback_pns),
            ))
        )
    }
    candidates: dict[str, set[int]] = defaultdict(set)
    for part in parts.values():
        candidates[part.pn_std.strip().upper()].add(part.id)

    result: dict[tuple[str, int | str], PnTotals] = {}
    projects: dict[tuple[str, int | str], set[str]] = defaultdict(set)
    for fact in facts:
        part_id = fact.part_id
        normalized_pn = fact.pn.strip().upper()
        if part_id is None and len(candidates[normalized_pn]) == 1:
            part_id = next(iter(candidates[normalized_pn]))
        part = parts.get(part_id)
        key = ("part", part_id) if part_id is not None else ("pn", normalized_pn)
        if key not in result:
            result[key] = {
                **_empty_totals(),
                "part_id": part_id,
                "pn": part.pn_std if part is not None else normalized_pn,
                "description": part.description if part is not None else fact.description,
                "project_ids": [],
            }
        _add(result[key], fact)
        if not result[key]["description"] and fact.description:
            result[key]["description"] = fact.description
        projects[key].add(fact.project_id)
    for key, value in result.items():
        value["project_ids"] = sorted(projects[key])
    return sorted(result.values(), key=lambda row: (row["pn"].upper(), row["part_id"] or 0))


def rate_pct(returned_qty: Decimal, issued_qty: Decimal) -> float | None:
    """Percentage at one decimal; an absent denominator is not a zero rate."""
    if issued_qty <= 0:
        return None
    return float((returned_qty / issued_qty * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
