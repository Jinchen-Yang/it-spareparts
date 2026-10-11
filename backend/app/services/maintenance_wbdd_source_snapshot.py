"""Immutable, non-sensitive WBDD source membership (D-27).

Capture runs on the reader's parent-filled frame, before business validation.
It records identities even when a row cannot be imported; a rejected row must
never look like an absent row. This module neither reads nor changes facts.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from numbers import Integral
import re
from typing import Iterable, Mapping

import pandas as pd

from app.etl import mapping

_ORDER_NUMBER = re.compile(r"WBDD-([0-9]{8})-([0-9]{4})", re.ASCII)
_NON_BLOCKING_ERRORS = frozenset({"maintenance_address_conflict"})


def _text(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text if text and text.lower() != "nan" else None


def _order_sequence(order_no: str | None) -> tuple[str, int] | None:
    match = _ORDER_NUMBER.fullmatch(order_no or "")
    if match is None:
        return None
    try:
        day = datetime.strptime(match[1], "%Y%m%d").date()
    except ValueError:
        return None
    return day.isoformat(), int(match[2])


def capture_source_snapshot(
    df: pd.DataFrame, *, sheet_name: str, ignored_wbdd_sheets: Iterable[str] = (),
) -> dict:
    """Return JSON-serializable source membership, independent of import success.

    ``row_numbers`` use the same one-based data-row indices as TransformResult
    errors, not Excel's header-inclusive row numbers. ``line_ids`` at the root
    includes orphan and cross-parent identities, so a later comparison cannot
    mistake a transferred or invalid source row for a deletion.

    Global ``blocked_reasons`` prohibit absence inference for the file. Order
    reasons prohibit it for that order. Missing optional business columns do
    not affect membership; business errors are added with block_snapshot_errors.
    """
    head_columns = {value: key for key, value in mapping.MAINTENANCE_HEAD.items()}
    line_columns = {value: key for key, value in mapping.MAINTENANCE_LINE.items()}
    required = {
        "raw_order_id": head_columns["raw_order_id"],
        "order_no": head_columns["order_no"],
        "raw_line_id": line_columns["raw_line_id"],
    }
    positions = {}
    global_reasons: set[str] = set()
    for field, column in required.items():
        indices = [index for index, name in enumerate(df.columns) if name == column]
        if not indices:
            global_reasons.add("missing_identity_columns")
        elif len(indices) > 1:
            global_reasons.add("duplicate_identity_columns")
        positions[field] = indices[0] if indices else None

    ignored = sorted({str(name) for name in ignored_wbdd_sheets})
    if ignored:
        global_reasons.add("ignored_wbdd_sheets")
    orders: dict[str, dict] = {}
    order_numbers: dict[str, set[str]] = defaultdict(set)
    number_owners: dict[str, set[str]] = defaultdict(set)
    line_owners: dict[str, set[str | None]] = defaultdict(set)
    all_line_ids: set[str] = set()

    for ordinal, row in enumerate(df.itertuples(index=True, name=None), start=1):
        index, *values = row
        row_no = int(index) + 1 if isinstance(index, Integral) and index >= 0 else ordinal
        identities = {
            field: _text(values[position]) if position is not None else None
            for field, position in positions.items()
        }
        raw_id = identities["raw_order_id"]
        order_no = identities["order_no"]
        line_id = identities["raw_line_id"]
        if line_id:
            all_line_ids.add(line_id)
            line_owners[line_id].add(raw_id)
        if not any(identities.values()):
            # Completely empty rows convey no identity. If they contain invalid
            # business data, their transform errors will block the file below.
            continue
        if not raw_id:
            global_reasons.add("missing_order_id")
            continue
        order = orders.setdefault(raw_id, {
            "raw_order_id": raw_id, "row_numbers": set(), "line_ids": set(),
            "blocked_reasons": set(),
        })
        order["row_numbers"].add(row_no)
        if line_id:
            order["line_ids"].add(line_id)
        if order_no:
            order_numbers[raw_id].add(order_no)
            number_owners[order_no].add(raw_id)
            if _order_sequence(order_no) is None:
                order["blocked_reasons"].add("invalid_order_no")
        else:
            order["blocked_reasons"].add("missing_order_no")

    for raw_id, numbers in order_numbers.items():
        if len(numbers) > 1:
            orders[raw_id]["blocked_reasons"].add("order_number_conflict")
    for owners in number_owners.values():
        if len(owners) > 1:
            for raw_id in owners:
                orders[raw_id]["blocked_reasons"].add("duplicate_order_number")
    for owners in line_owners.values():
        if len(owners) > 1:
            for raw_id in owners:
                if raw_id is not None:
                    orders[raw_id]["blocked_reasons"].add("line_parent_conflict")

    serialized_orders = []
    for raw_id in sorted(orders):
        order = orders[raw_id]
        numbers = order_numbers[raw_id]
        serialized_orders.append({
            "raw_order_id": raw_id,
            "order_no": next(iter(numbers)) if len(numbers) == 1 else None,
            "row_numbers": sorted(order["row_numbers"]),
            "line_ids": sorted(order["line_ids"]),
            "blocked_reasons": sorted(order["blocked_reasons"]),
        })
    return {
        "version": 1, "sheet_name": str(sheet_name), "row_count": len(df),
        "orders": serialized_orders, "line_ids": sorted(all_line_ids),
        "blocked_reasons": sorted(global_reasons), "ignored_wbdd_sheets": ignored,
    }


def block_snapshot_errors(snapshot: dict, errors: Iterable) -> dict:
    """Copy and mark rejected rows without retaining raw values/error details.

    Accept TransformResult ErrorRec objects or mappings with ``row_no`` and
    ``error_type``. Even soft empty-PN errors affect membership completeness;
    address conflicts alone do not. Unlocatable errors block the whole file.
    """
    result = deepcopy(snapshot)
    row_orders: dict[int, list[dict]] = defaultdict(list)
    order_reasons: dict[str, set[str]] = {}
    for order in result["orders"]:
        order_reasons[order["raw_order_id"]] = set(order["blocked_reasons"])
        for row_no in order["row_numbers"]:
            row_orders[row_no].append(order)
    global_reasons = set(result["blocked_reasons"])
    for error in errors:
        if isinstance(error, Mapping):
            row_no, error_type = error.get("row_no"), error.get("error_type")
        else:
            row_no = getattr(error, "row_no", None)
            error_type = getattr(error, "error_type", None)
        if error_type in _NON_BLOCKING_ERRORS:
            continue
        reason = f"parse_error:{error_type}" if isinstance(error_type, str) else "parse_error:unknown"
        owners = row_orders.get(row_no, []) if isinstance(row_no, Integral) else []
        if not owners:
            global_reasons.add(reason)
        for order in owners:
            order_reasons[order["raw_order_id"]].add(reason)
    for order in result["orders"]:
        order["blocked_reasons"] = sorted(order_reasons[order["raw_order_id"]])
    result["blocked_reasons"] = sorted(global_reasons)
    return result


def daily_order_windows(snapshot: dict) -> list[dict]:
    """Bound each observed day separately; never infer absent days or endpoints.

    Only unblocked identities supply bounds. A singleton is a singleton range,
    not evidence of a whole day. Global incomplete coverage supplies no bounds.
    """
    if snapshot.get("blocked_reasons"):
        return []
    sequences: dict[str, list[int]] = defaultdict(list)
    for order in snapshot.get("orders", []):
        if order.get("blocked_reasons"):
            continue
        parsed = _order_sequence(order.get("order_no"))
        if parsed:
            day, sequence = parsed
            sequences[day].append(sequence)
    return [
        {"date": day, "from_sequence": min(sequences[day]),
         "to_sequence": max(sequences[day])}
        for day in sorted(sequences)
    ]
