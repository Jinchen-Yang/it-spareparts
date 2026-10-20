"""D-34 demand fact protection shared by page, workbook and source import.

Callers own validation, locks, CAS, audit and transactions. This module neither
commits nor acquires locks. Cost evidence remains in its independent ledger.
Legacy non-wbdd rows are never converted by recording another manual edit.
"""
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, or_

from app.models.maintenance import FMaintenanceLine


PN_IDENTITY_FIELDS = frozenset({"pn_raw", "pn_std", "part_id"})
DEMAND_FACT_FIELDS = frozenset({
    "qty", "return_qty", "serial_numbers", "description", "line_note",
}) | PN_IDENTITY_FIELDS
MANUAL_LINE_PREFIXES = ("manual-line:", "page-manual-", "workbook-create-")


def normalized_value(field, value):
    """Compare stored precision and normalized text, including explicit blanks."""
    if value is None:
        return None
    if field in {"qty", "return_qty"}:
        return Decimal(str(value)).quantize(Decimal("0.001"))
    if field == "part_id":
        return value
    value = str(value).strip() or None
    return value.upper() if value and field == "pn_std" else value


def snapshot_fields(line) -> dict:
    return {field: getattr(line, field) for field in sorted(DEMAND_FACT_FIELDS)}


def changed_fields(before: dict, after: dict) -> set[str]:
    return {field for field in DEMAND_FACT_FIELDS & after.keys()
            if normalized_value(field, before.get(field))
            != normalized_value(field, after[field])}


def _json_value(value):
    return str(value) if isinstance(value, Decimal) else value


def record_manual_changes(line, *, before: dict, operated_by: str,
                          source: str) -> set[str]:
    """Append actual changes, preserving the earliest source snapshot per field.

    PN is recorded as one group even when only one member changes. The return
    value lists actual changes; the ledger also contains the protected peers.
    """
    after = snapshot_fields(line)
    changed = changed_fields(before, after)
    if not changed:
        return set()
    fields = changed | (PN_IDENTITY_FIELDS if changed & PN_IDENTITY_FIELDS else set())
    ledger = dict(line.manual_override or {})
    original_part_id = before.get("part_id")
    for name in ("pn_std", "pn_raw"):
        entry = ledger.get(name) or {}
        if "source_value_part_id" in entry:
            original_part_id = entry["source_value_part_id"]
            break
    original_part_id = (ledger.get("part_id") or {}).get("source_value", original_part_id)
    now = datetime.now(UTC).isoformat()
    for field in sorted(fields):
        old_entry = ledger.get(field) or {}
        ledger[field] = {
            "value": _json_value(after[field]),
            "source_value": old_entry.get("source_value", _json_value(before.get(field))),
            "updated_by": operated_by,
            "updated_at": now,
            "source": source,
        }
        if field in PN_IDENTITY_FIELDS:
            if field == "part_id":
                ledger[field]["source_value"] = original_part_id
            else:
                ledger[field]["source_value_part_id"] = original_part_id
    line.manual_override = ledger
    return changed


def is_manual_created(raw_line_id: str) -> bool:
    return raw_line_id.startswith(MANUAL_LINE_PREFIXES)


def is_legacy_review(*, raw_line_id: str, edited_source: str) -> bool:
    return edited_source != "wbdd" and not is_manual_created(raw_line_id)


def protected_fields(*, raw_line_id: str, edited_source: str,
                     manual_override: dict | None) -> frozenset[str]:
    if is_manual_created(raw_line_id) or edited_source != "wbdd":
        return DEMAND_FACT_FIELDS
    fields = set(manual_override or {}) & DEMAND_FACT_FIELDS
    if fields & PN_IDENTITY_FIELDS:
        fields |= PN_IDENTITY_FIELDS
    return frozenset(fields)


def is_field_protected(field: str, *, raw_line_id: str, edited_source: str,
                       manual_override: dict | None) -> bool:
    # A synthetic identity cannot be moved to a source parent by a matching ID.
    return is_manual_created(raw_line_id) or field in protected_fields(
        raw_line_id=raw_line_id, edited_source=edited_source,
        manual_override=manual_override)


def manual_created_condition(model=FMaintenanceLine):
    return or_(*(model.raw_line_id.startswith(prefix) for prefix in MANUAL_LINE_PREFIXES))


def legacy_review_condition(model=FMaintenanceLine):
    return and_(model.edited_source != "wbdd", ~manual_created_condition(model))


def protection_condition(field: str, model=FMaintenanceLine):
    """SQL counterpart of is_field_protected, using the same field groups."""
    manual = manual_created_condition(model)
    if field not in DEMAND_FACT_FIELDS:
        return manual
    group = PN_IDENTITY_FIELDS if field in PN_IDENTITY_FIELDS else {field}
    return or_(manual, model.edited_source != "wbdd",
               *(model.manual_override.op("?")(name) for name in sorted(group)))
