"""Immutable evidence of WBDD header statuses actually applied by an upload.

Source status and source membership are independent evidence: a rejected child
does not invalidate its successfully imported header, and a skip upload never
authorizes cancellation. This module records evidence; it does not void facts.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.etl import cleaner, mapping
from app.models.maintenance import FMaintenanceOrder

SNAPSHOT_KEY = "_wbdd_source_status_snapshot"
_CHUNK_SIZE = 1000


def _text(value) -> str | None:
    return None if value is None or pd.isna(value) else cleaner.clean_str(value)


def capture_source_status_snapshot(df: pd.DataFrame) -> dict:
    """Capture every header status from the reader's parent-filled frame.

    Multiple different values for one raw identity cannot establish an explicit
    final status. Missing/duplicate status columns likewise remain evidence gaps.
    Unlike membership reconciliation, PN/quantity errors do not block a header.
    """
    columns = {value: key for key, value in mapping.MAINTENANCE_HEAD.items()}
    identity_positions = [i for i, name in enumerate(df.columns)
                          if name == columns["raw_order_id"]]
    status_positions = [i for i, name in enumerate(df.columns)
                        if name == columns["data_status"]]
    captured = {"version": 1, "captured_at": datetime.now(UTC).isoformat(),
                "orders": []}
    # Without one reliable raw identity column there is no candidate identity.
    if len(identity_positions) != 1:
        return captured
    common_reasons = []
    if not status_positions:
        common_reasons.append("missing_status_column")
    elif len(status_positions) != 1:
        common_reasons.append("duplicate_status_columns")
    status_by_identity: dict[str, set[str | None]] = defaultdict(set)
    for values in df.itertuples(index=False, name=None):
        raw_id = _text(values[identity_positions[0]])
        if raw_id is None:
            continue
        value = _text(values[status_positions[0]]) if len(status_positions) == 1 else None
        status_by_identity[raw_id].add(value)
    for raw_id, values in sorted(status_by_identity.items()):
        reasons = list(common_reasons)
        if len(values) != 1:
            reasons.append("conflicting_header_status")
        captured["orders"].append({
            "raw_order_id": raw_id,
            "data_status": next(iter(values)) if len(values) == 1 else None,
            "applied": False,
            "blocked_reasons": reasons,
        })
    return captured


def finalize_source_status_snapshot(
    db: Session, captured: dict, *, batch_id: int, mode: str, file_hash: str,
) -> dict:
    """Bind captured statuses to the successfully loaded batch in its transaction.

    Select columns rather than ORM entities: loader uses Core upserts and an
    existing identity-map object may still carry its pre-upload status. Return a
    fresh value; later uploads cannot mutate previous batch evidence in memory.
    """
    result = deepcopy(captured)
    result.update(batch_id=batch_id, mode=mode, file_hash=file_hash)
    if mode != "upsert":
        return result
    identities = [item["raw_order_id"] for item in result["orders"]]
    current = {}
    for offset in range(0, len(identities), _CHUNK_SIZE):
        rows = db.execute(select(
            FMaintenanceOrder.raw_order_id,
            FMaintenanceOrder.data_status,
            FMaintenanceOrder.import_batch_id,
        ).where(FMaintenanceOrder.raw_order_id.in_(identities[offset:offset + _CHUNK_SIZE])))
        current.update({raw_id: (status, source_batch) for raw_id, status, source_batch in rows})
    for item in result["orders"]:
        if item["blocked_reasons"]:
            continue
        if current.get(item["raw_order_id"]) == (item["data_status"], batch_id):
            item["applied"] = True
        else:
            item["blocked_reasons"].append("source_status_not_applied")
    return result
