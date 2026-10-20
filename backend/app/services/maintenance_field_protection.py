"""D-34 demand fact protection shared by page, workbook and source import.

Callers own validation, locks, CAS, audit and transactions. This module neither
commits nor acquires locks. Cost evidence remains in its independent ledger.
Legacy non-wbdd rows are never converted by recording another manual edit.

Release baseline (Q1=A, 金臣 2026-10-20): returning to the round's first
snapshot — the effective value before this protection cycle's first manual
edit — releases the field; matching the latest source value alone does not.
"""
import hashlib
import json
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
    """Record actual changes; returning to the first snapshot releases protection.

    Q1=A: the release baseline is this cycle's first snapshot (the value before
    its first manual edit), never the latest refreshed source value. PN is
    recorded as one group even when only one member changes. The return value
    lists actual changes; the ledger also contains the protected peers.
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
        first_known, first_value = original_source_snapshot(old_entry)
        if (
            can_release
            and field not in PN_IDENTITY_FIELDS and first_known
            and normalized_value(field, after[field])
            == normalized_value(field, first_value)
        ):
            ledger.pop(field)
            continue
        entry = {
            **old_entry,
            "value": _json_value(after[field]),
            "updated_by": operated_by,
            "updated_at": now,
            "source": source,
        }
        if not was_recorded and (
            field not in PN_IDENTITY_FIELDS or not had_pn_protection
        ):
            # 新一轮保护周期建账：source_value 即本轮首次快照（解除比较基准），
            # cycle_started_at 标记周期起点，供旧工作簿漂移校验识别重建。
            entry["source_value"] = _json_value(before.get(field))
            entry["cycle_started_at"] = now
        ledger[field] = entry
        if field in PN_IDENTITY_FIELDS and part_source_known:
            if field == "part_id":
                entry["source_value"] = source_part_id
            else:
                entry["source_value_part_id"] = source_part_id
    # An old partial PN ledger cannot prove that an unrecorded peer is original.
    if can_release and changed & PN_IDENTITY_FIELDS and all(
        field in ledger
        and original_source_snapshot(ledger[field])[0]
        and normalized_value(field, after[field])
        == normalized_value(field, original_source_snapshot(ledger[field])[1])
        for field in PN_IDENTITY_FIELDS
    ):
        for field in PN_IDENTITY_FIELDS:
            ledger.pop(field)
    line.manual_override = ledger
    return changed


def manual_change_reason(reason: str, before: dict, after: dict) -> str:
    released = (before.keys() - after.keys()) & DEMAND_FACT_FIELDS
    if released:
        return (f"{reason}；改回本轮首次快照（原值），保护解除："
                f"{', '.join(sorted(released))}")
    return reason


def original_source_snapshot(entry: dict) -> tuple[bool, object]:
    """Explicit clear keeps its first-snapshot contract, including unknown peers."""
    if entry.get("original_source_value_known") is False:
        return False, None
    if "original_source_value" in entry:
        return True, entry["original_source_value"]
    return "source_value" in entry, entry.get("source_value")


def protection_baseline(*, raw_line_id: str, edited_source: str,
                        manual_override: dict | None) -> dict[str, dict]:
    """Q3=A：字段级解除基准指纹，签入 03 行基线令牌供旧工作簿漂移校验。

    只记录受保护的需求事实字段；value 是归一化后的本轮首次快照（解除比
    较基准），cycle 是保护周期起点标记——期间解除又重新保护、即使显示值
    与首次快照都恰好相同，cycle 也会变，漂移仍可检出。latest source 刷新
    只动 source_value，不进指纹：同一周期内仅来源变化不算漂移。
    """
    out: dict[str, dict] = {}
    for field in sorted(protected_fields(
        raw_line_id=raw_line_id, edited_source=edited_source,
        manual_override=manual_override,
    )):
        entry = (manual_override or {}).get(field) or {}
        known, value = original_source_snapshot(entry)
        out[field] = {
            "known": known,
            "value": (
                None if not known or value is None
                else _json_value(normalized_value(field, value))
            ),
            "cycle": entry.get("cycle_started_at"),
        }
    return out


def protection_baseline_changed(
    exported: dict | None, current: dict, fields,
) -> tuple[bool, str]:
    """用户编辑字段的漂移判定；返回 (是否漂移, 原因码)。

    exported 为 None 表示旧格式令牌（无保护证据）：当前受保护即阻断，要
    求重新下载；两边都无记录（都未保护）则放行。
    """
    for field in sorted(fields):
        was = (exported or {}).get(field)
        now = current.get(field)
        if was is None and now is None:
            continue
        if was is None:
            if exported is None:
                # 旧格式令牌（2.7.0，无 protect 证据）：无从校验基准，只能
                # 要求重新下载；与「导出后新建保护」分开提示。
                return True, "protection_evidence_missing"
            return True, "protection_started_since_export"
        if now is None:
            return True, "protection_released_since_export"
        if was != now:
            return True, "protection_cycle_changed"
    return False, ""


def protection_baseline_digest(baseline: dict[str, dict]) -> str:
    """锁内复核用的稳定摘要：validate 时刻与 apply 时刻各算一次比对。"""
    payload = json.dumps(
        baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
