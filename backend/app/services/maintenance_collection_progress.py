"""Read-only instalment progress for the frozen receipt-import preview (D-28).

Receipt dates select reporting months; they never bind a receipt to a planned
instalment.  Each candidate below describes the latest confirmed cumulative
amount after that monthly operation.  Selecting an older month cannot replace
a later existing snapshot, and cumulative amounts must never be added together.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from app.services.maintenance_collection_reminders import derive_collection_payment_state


_CANDIDATE_ACTIONS = frozenset({
    "upsert_collection_snapshot", "update_collection_snapshot", "record_receipts",
})
_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(_CENT), ".2f")


def _candidate_amount(row: dict) -> Decimal | None:
    value = (row.get("after") or {}).get("cumulative_amount")
    if value is None:
        value = (row.get("canonical") or {}).get("cumulative_received_inc_tax")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _progress(
    milestones: list[MaintenanceCollectionMilestone],
    *,
    report_month: date | None,
    received: Decimal | None,
    as_of: date,
) -> dict:
    cumulative = _ZERO
    plan_complete = True
    expected_sequence = 1
    states: list[dict] = []
    snapshot = None if received is None else {"cumulative_amount": received}
    for milestone in milestones:
        amount = milestone.planned_amount
        previous = cumulative
        # A missing earlier target cannot be treated as zero when assessing a
        # later instalment.  A sequence gap is missing plan evidence as well.
        plan_complete = (
            plan_complete
            and milestone.sequence == expected_sequence
            and milestone.completeness_state == "complete"
            and milestone.planned_date is not None
            and amount is not None
        )
        expected_sequence = milestone.sequence + 1
        if amount is not None:
            cumulative += amount
        state = "incomplete"
        period_received = None
        remaining = None
        if plan_complete:
            state = derive_collection_payment_state(
                milestone,
                cumulative_planned=cumulative,
                previous_cumulative_planned=previous,
                latest_confirmed_snapshot=snapshot,
                as_of=as_of,
            )
            if received is not None:
                period_received = min(amount, max(_ZERO, received - previous))
                remaining = amount - period_received
        states.append({
            "milestone_id": milestone.milestone_id,
            "sequence": milestone.sequence,
            "planned_date": (
                milestone.planned_date.isoformat()
                if milestone.planned_date is not None else None
            ),
            "planned_amount": _money(amount),
            "payment_state": state,
            "received_for_period": _money(period_received),
            "remaining_amount": _money(remaining),
        })
    return {
        "report_month": report_month.isoformat() if report_month is not None else None,
        "cumulative_received": _money(received),
        "milestones": states,
    }


def build_collection_progress(
    db: Session,
    rows: list[dict],
    *,
    as_of: date | None = None,
) -> list[dict]:
    """Project only contracts already exposed by the caller's scoped preview.

    This function does not write, load extra contracts by name, or recalculate
    receipt totals.  Candidate amounts come from server-frozen monthly actions.
    Two bounded queries load all plans and latest confirmed snapshots together.
    """
    grouped_rows: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("matched_contract_id"):
            grouped_rows[str(row["matched_contract_id"])].append(row)
    if not grouped_rows:
        return []
    contract_ids = sorted(grouped_rows)
    plans: dict[str, list[MaintenanceCollectionMilestone]] = defaultdict(list)
    for milestone in db.scalars(
        select(MaintenanceCollectionMilestone)
        .where(
            MaintenanceCollectionMilestone.project_contract_id.in_(contract_ids),
            MaintenanceCollectionMilestone.is_active.is_(True),
        )
        .order_by(
            MaintenanceCollectionMilestone.project_contract_id,
            MaintenanceCollectionMilestone.sequence,
        )
    ):
        plans[milestone.project_contract_id].append(milestone)
    snapshots = {
        snapshot.project_contract_id: snapshot
        for snapshot in db.scalars(
            select(MaintenanceCollectionSnapshot)
            .where(
                MaintenanceCollectionSnapshot.project_contract_id.in_(contract_ids),
                MaintenanceCollectionSnapshot.status == "confirmed",
            )
            .distinct(MaintenanceCollectionSnapshot.project_contract_id)
            .order_by(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.report_month.desc(),
            )
        )
    }
    today = as_of if as_of is not None else business_today()
    result = []
    for contract_id, sources in sorted(grouped_rows.items()):
        snapshot = snapshots.get(contract_id)
        before_month = snapshot.report_month if snapshot is not None else None
        before_received = snapshot.cumulative_amount if snapshot is not None else None
        milestones = plans.get(contract_id, [])
        before = _progress(
            milestones, report_month=before_month, received=before_received, as_of=today,
        )
        candidates = []
        for row in sources:
            if (
                row.get("row_status") != "ready"
                or row.get("match_state") != "matched"
                or row.get("action") not in _CANDIDATE_ACTIONS
                or row.get("errors")
            ):
                continue
            canonical = row.get("canonical") or {}
            try:
                month = date.fromisoformat(str(canonical.get("report_month") or ""))
            except ValueError:
                continue
            amount = _candidate_amount(row)
            if amount is None or not row.get("row_key"):
                continue
            after = before if before_month is not None and month < before_month else _progress(
                milestones, report_month=month, received=amount, as_of=today,
            )
            candidates.append({
                "row_key": row["row_key"],
                "report_month": month.isoformat(),
                "after": after,
            })
        source = sources[0]
        result.append({
            "contract_id": contract_id,
            "project_id": source.get("matched_project_id"),
            "contract_no": (source.get("canonical") or {}).get("sales_order_no"),
            "before": before,
            "candidates": sorted(candidates, key=lambda item: (item["report_month"], item["row_key"])),
        })
    return result
