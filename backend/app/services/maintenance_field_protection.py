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
    """Record actual changes; returning to the source releases field protection.

    PN is recorded as one group even when only one member changes. The return
    value lists actual changes; the ledger also contains the protected peers.
    """
    after = snapshot_fields(line)
    changed = changed_fields(before, after)
    if not changed:
        return set()
    fields = changed | (PN_IDENTITY_FIELDS if changed & PN_IDENTITY_FIELDS else set())
    ledger = dict(line.manual_override or {})
    had_pn_protection = bool(ledger.keys() & PN_IDENTITY_FIELDS)
    part_source_known = not had_pn_protection
    source_part_id = before.get("part_id")
    for name in ("pn_std", "pn_raw"):
        entry = ledger.get(name) or {}
        if "source_value_part_id" in entry:
            part_source_known = True
            source_part_id = entry["source_value_part_id"]
            break
    if "source_value" in (ledger.get("part_id") or {}):
        part_source_known = True
        source_part_id = ledger["part_id"]["source_value"]
    can_release = line.edited_source == "wbdd" and not is_manual_created(line.raw_line_id)
    now = datetime.now(UTC).isoformat()
    for field in sorted(fields):
        was_recorded = field in ledger
        old_entry = ledger.get(field) or {}
        if (
            can_release
            and field not in PN_IDENTITY_FIELDS and "source_value" in old_entry
            and normalized_value(field, after[field])
            == normalized_value(field, old_entry["source_value"])
        ):
            ledger.pop(field)
            continue
        ledger[field] = {
            **old_entry,
            "value": _json_value(after[field]),
            "updated_by": operated_by,
            "updated_at": now,
            "source": source,
        }
        if not was_recorded and (
            field not in PN_IDENTITY_FIELDS or not had_pn_protection
        ):
            ledger[field]["source_value"] = _json_value(before.get(field))
        if field in PN_IDENTITY_FIELDS and part_source_known:
            if field == "part_id":
                ledger[field]["source_value"] = source_part_id
            else:
                ledger[field]["source_value_part_id"] = source_part_id
    # An old partial PN ledger cannot prove that an unrecorded peer is original.
    if can_release and changed & PN_IDENTITY_FIELDS and all(
        "source_value" in ledger[field]
        and normalized_value(field, after[field])
        == normalized_value(field, ledger[field]["source_value"])
        for field in PN_IDENTITY_FIELDS
    ):
        for field in PN_IDENTITY_FIELDS:
            ledger.pop(field)
    line.manual_override = ledger
    return changed


def manual_change_reason(reason: str, before: dict, after: dict) -> str:
    released = (before.keys() - after.keys()) & DEMAND_FACT_FIELDS
    if released:
        return f"{reason}；改回来源值，保护解除：{', '.join(sorted(released))}"
    return reason


def original_source_snapshot(entry: dict) -> tuple[bool, object]:
    """Explicit clear keeps its first-snapshot contract, including unknown peers."""
    if entry.get("original_source_value_known") is False:
        return False, None
    if "original_source_value" in entry:
        return True, entry["original_source_value"]
    return "source_value" in entry, entry.get("source_value")


def refresh_source_values(*, raw_line_id: str, edited_source: str,
                          manual_override: dict, before: dict, source: dict) -> dict:
    """Refresh protected source snapshots without applying or releasing them.

    The caller supplies the final legal upsert row and owns persistence/audit.
    Missing historical PN peers become known only from this source evidence.
    """
    if edited_source != "wbdd" or is_manual_created(raw_line_id):
        return manual_override
    fields = set(manual_override) & DEMAND_FACT_FIELDS
    if fields & PN_IDENTITY_FIELDS:
        fields |= PN_IDENTITY_FIELDS
    ledger = dict(manual_override)
    for field in sorted(fields & source.keys()):
        old = manual_override.get(field) or {}
        value = _json_value(normalized_value(field, source[field]))
        if (
            "source_value" in old
            and normalized_value(field, old["source_value"]) == normalized_value(field, value)
        ):
            continue
        entry = {**old}
        if "original_source_value" not in entry and "original_source_value_known" not in entry:
            known, original = original_source_snapshot(old)
            if not known and field == "part_id":
                for name in ("pn_std", "pn_raw"):
                    peer = manual_override.get(name) or {}
                    if "source_value_part_id" in peer:
                        known, original = True, peer["source_value_part_id"]
                        break
            if known:
                entry["original_source_value"] = original
            else:
                entry["original_source_value_known"] = False
        entry.setdefault("value", _json_value(before.get(field)))
        entry["source_value"] = value
        ledger[field] = entry
    return ledger


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
