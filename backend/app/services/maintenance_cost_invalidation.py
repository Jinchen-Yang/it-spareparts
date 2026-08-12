"""Single boundary for invalidating derived WBDD cost evidence.

Imports own source facts; ``maintenance_cost.recompute`` owns all derived cost
columns.  Any workflow that makes an existing derivation stale must clear the
same complete column set before the WBDD can become effective again.
"""

from __future__ import annotations

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceLine


IMPORT_ANOMALY_FLAGS = frozenset({"future_date"})
COST_RECOMPUTE_PENDING_FLAG = "cost_recompute_pending"

_KEEP_FLAGS_SQL = (
    "ARRAY(SELECT f FROM unnest(anomaly_flags) AS f "
    "WHERE f = ANY(:keep_flags))"
)
_KEEP_FLAGS_AND_PENDING_SQL = (
    "ARRAY(SELECT DISTINCT f FROM unnest("
    "anomaly_flags || ARRAY['cost_recompute_pending']::text[]"
    ") AS f WHERE f = ANY(:keep_flags))"
)
_DERIVED_COST_NULLS = {
    "unit_cost": None,
    "cost_amount": None,
    "cost_source": None,
    "cost_tax_basis": None,
    "unit_cost_inc_tax": None,
    "unit_cost_ex_tax": None,
    "cost_amount_inc_tax": None,
    "cost_amount_ex_tax": None,
    "price_month": None,
    "trace_months": None,
    "linked_purchase_order_no": None,
    "price_distance_days": None,
    "confidence": None,
    "reference_side": None,
    "reference_pool_group_id": None,
    "reference_pool_version": None,
    "reference_sample_count": None,
    "reference_from_date": None,
    "reference_to_date": None,
    "reference_latest_date": None,
}


def invalidate_line_costs(
    db: Session,
    *,
    condition,
    pending_recompute: bool,
) -> int:
    """Clear every derived cost field for matching lines in the transaction.

    Import-time flags survive.  A restore additionally receives an explicit
    pending marker, which a later full recompute removes before rebuilding its
    current cost evidence.
    """

    keep_flags = list(IMPORT_ANOMALY_FLAGS)
    if pending_recompute:
        keep_flags.append(COST_RECOMPUTE_PENDING_FLAG)
    values = {
        **_DERIVED_COST_NULLS,
        "anomaly_flags": text(
            _KEEP_FLAGS_AND_PENDING_SQL if pending_recompute else _KEEP_FLAGS_SQL
        ),
    }
    result = db.execute(
        update(FMaintenanceLine)
        .where(condition)
        .values(**values)
        .execution_options(synchronize_session=False),
        {"keep_flags": keep_flags},
    )
    return max(int(result.rowcount or 0), 0)
