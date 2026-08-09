"""Pure, deterministic rules for maintenance cutover previews.

This module deliberately has no database writes.  A persisted migration run may
store its result, but the business calculation is a reproducible function of a
hash-bound source snapshot.  Legacy demand quantities and return offsets are not
accepted as cost inputs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


RULE_VERSION = "maintenance-cutover-v1"
_MONEY_QUANTUM = Decimal("0.01")
_MONEY_MAX_EXCLUSIVE = Decimal("1000000000000")
_QTY_MAX_EXCLUSIVE = Decimal("1000000000000")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOCKER_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_COST_STATUSES = {"confirmed", "corrected"}
_INVENTORY_MOVEMENT_TYPES = {
    "delivery",
    "available_receipt",
    "site_issue",
    "return_registration",
}


class MigrationControlError(ValueError):
    """A migration preview or state transition is not safe to continue."""


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required_text(value: Any, label: str, *, max_length: int = 128) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > max_length:
        raise MigrationControlError(f"{label}无效")
    return clean


def _parse_date(value: Any, label: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise MigrationControlError(f"{label}无效") from exc


def _decimal(
    value: Any,
    label: str,
    *,
    quantum: Decimal | None = None,
    upper: Decimal = _MONEY_MAX_EXCLUSIVE,
) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MigrationControlError(f"{label}无效") from exc
    if not number.is_finite() or number < 0 or number >= upper:
        raise MigrationControlError(f"{label}超出允许范围")
    if quantum is not None:
        try:
            number = number.quantize(quantum, rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise MigrationControlError(f"{label}超出允许范围") from exc
    return number


def _money(value: Any, label: str) -> Decimal:
    return _decimal(value, label, quantum=_MONEY_QUANTUM)


def _qty(value: Any, label: str) -> Decimal:
    return _decimal(value, label, upper=_QTY_MAX_EXCLUSIVE)


def _money_text(value: Decimal) -> str:
    return format(value.quantize(_MONEY_QUANTUM), "f")


def _qty_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized else "0"


def _hash(value: Any, label: str) -> str:
    clean = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean):
        raise MigrationControlError(f"{label}必须是 64 位 SHA-256")
    return clean


def _blocker(
    blockers: list[dict[str, Any]],
    code: str,
    *,
    entity_id: str | None = None,
    detail: str,
) -> None:
    blockers.append(
        {
            "code": code,
            "entity_id": entity_id,
            "detail": detail,
        }
    )


def _source_blockers(
    payload: Mapping[str, Any], blockers: list[dict[str, Any]]
) -> None:
    for row in payload.get("source_blockers") or []:
        if not isinstance(row, Mapping):
            raise MigrationControlError("来源阻塞项无效")
        code = _required_text(row.get("code"), "来源阻塞项代码", max_length=64)
        if not _BLOCKER_CODE_RE.fullmatch(code):
            raise MigrationControlError("来源阻塞项代码无效")
        entity_value = row.get("entity_id")
        entity_id = None
        if entity_value is not None:
            entity_id = _required_text(entity_value, "来源阻塞项实体", max_length=128)
        detail = _required_text(row.get("detail"), "来源阻塞项说明", max_length=1000)
        _blocker(blockers, code, entity_id=entity_id, detail=detail)


def _cost_pair(
    row: Mapping[str, Any], *, prefix: str
) -> tuple[Decimal, Decimal] | None:
    ex_value = row.get("cost_amount_ex_tax")
    inc_value = row.get("cost_amount_inc_tax")
    if ex_value is None or inc_value is None:
        return None
    return _money(ex_value, f"{prefix}未税成本"), _money(inc_value, f"{prefix}含税成本")


def _historical_cost(
    payload: Mapping[str, Any],
    *,
    cutover_date: date,
    blockers: list[dict[str, Any]],
) -> tuple[Decimal, Decimal]:
    mode = payload.get("historical_mode")
    baseline = payload.get("historical_baseline")
    historical_rows = list(payload.get("historical_site_issues") or [])
    if mode not in {"approved_cost_baseline", "stable_site_issues"}:
        raise MigrationControlError("历史成本模式无效")
    if mode == "stable_site_issues" and baseline is not None:
        raise MigrationControlError("历史稳定领用与历史成本基线不能同时使用")

    if mode == "approved_cost_baseline":
        for row in historical_rows:
            if not isinstance(row, Mapping):
                raise MigrationControlError("历史领用来源无效")
            if (
                row.get("stable_identity") is True
                and row.get("workflow_status") in _COST_STATUSES
                and _parse_date(row.get("issue_date"), "历史领用日期") < cutover_date
            ):
                raise MigrationControlError(
                    "检测到经确认且有稳定身份的可靠历史领用，不能改用历史成本基线"
                )
        if not isinstance(baseline, Mapping):
            _blocker(
                blockers,
                "missing_historical_baseline",
                detail="项目缺少已审批的切换日前历史成本基线",
            )
            return Decimal("0"), Decimal("0")
        amount_ex = _money(baseline.get("amount_ex_tax"), "历史基线未税金额")
        amount_inc = _money(baseline.get("amount_inc_tax"), "历史基线含税金额")
        _hash(baseline.get("evidence_hash"), "历史基线证据哈希")
        if baseline.get("approved") is not True:
            _blocker(
                blockers,
                "historical_baseline_not_approved",
                detail="历史成本基线尚未实名审批",
            )
            return Decimal("0"), Decimal("0")
        return amount_ex, amount_inc

    total_ex = Decimal("0")
    total_inc = Decimal("0")
    seen: set[str] = set()
    if not historical_rows:
        _blocker(
            blockers,
            "missing_historical_site_issues",
            detail="没有可靠历史领用，必须改用已审批成本基线",
        )
    for row in historical_rows:
        row_id = _required_text(row.get("issue_line_id"), "历史领用稳定编号")
        if row_id in seen:
            raise MigrationControlError("历史领用稳定编号重复")
        seen.add(row_id)
        row_date = _parse_date(row.get("issue_date"), "历史领用日期")
        workflow_status = row.get("workflow_status")
        if workflow_status == "void":
            continue
        eligible = True
        if row.get("stable_identity") is not True:
            eligible = False
            _blocker(
                blockers,
                "historical_issue_missing_identity",
                entity_id=row_id,
                detail="历史领用缺少稳定来源身份",
            )
        if row_date >= cutover_date:
            eligible = False
            _blocker(
                blockers,
                "historical_issue_date_overlap",
                entity_id=row_id,
                detail="历史领用日期必须早于切换日",
            )
        if workflow_status not in _COST_STATUSES:
            eligible = False
            _blocker(
                blockers,
                "historical_issue_not_confirmed",
                entity_id=row_id,
                detail="历史领用尚未确认或已作废",
            )
        pair = _cost_pair(row, prefix="历史领用") if eligible else None
        if pair is None:
            if eligible:
                eligible = False
                _blocker(
                    blockers,
                    "historical_issue_missing_cost",
                    entity_id=row_id,
                    detail="历史领用成本未就绪",
                )
        if eligible and pair is not None:
            total_ex += pair[0]
            total_inc += pair[1]
    return total_ex, total_inc


def _post_cutover_cost(
    payload: Mapping[str, Any],
    *,
    cutover_date: date,
    blockers: list[dict[str, Any]],
) -> tuple[Decimal, Decimal]:
    total_ex = Decimal("0")
    total_inc = Decimal("0")
    seen: set[str] = set()
    for row in payload.get("post_cutover_site_issues") or []:
        row_id = _required_text(row.get("issue_line_id"), "现场领用稳定编号")
        if row_id in seen:
            raise MigrationControlError("现场领用稳定编号重复")
        seen.add(row_id)
        row_date = _parse_date(row.get("issue_date"), "现场领用日期")
        workflow_status = row.get("workflow_status")
        if workflow_status == "void":
            continue
        eligible = True
        if row_date < cutover_date:
            eligible = False
            _blocker(
                blockers,
                "post_cutover_issue_date_overlap",
                entity_id=row_id,
                detail="切换后领用日期早于切换日",
            )
        if workflow_status not in _COST_STATUSES:
            eligible = False
            _blocker(
                blockers,
                "unapproved_site_issue",
                entity_id=row_id,
                detail="仅已确认或已更正的现场领用计入成本",
            )
        pair = _cost_pair(row, prefix="现场领用") if eligible else None
        if pair is None:
            if eligible:
                eligible = False
                _blocker(
                    blockers,
                    "missing_site_issue_cost",
                    entity_id=row_id,
                    detail="现场领用成本未就绪，不能静默按零计价",
                )
        if eligible and pair is not None:
            total_ex += pair[0]
            total_inc += pair[1]
    return total_ex, total_inc


def _expense_cost(
    payload: Mapping[str, Any],
    *,
    blockers: list[dict[str, Any]],
) -> tuple[Decimal, Decimal]:
    total_ex = Decimal("0")
    total_inc = Decimal("0")
    seen: set[str] = set()
    for row in payload.get("approved_expenses") or []:
        row_id = _required_text(row.get("expense_id"), "报销稳定编号")
        if row_id in seen:
            raise MigrationControlError("报销稳定编号重复")
        seen.add(row_id)
        _parse_date(row.get("expense_date"), "报销日期")
        normalized_status = row.get("normalized_status")
        if normalized_status in {"rejected", "void"}:
            continue
        if normalized_status != "approved":
            _blocker(
                blockers,
                "expense_not_approved",
                entity_id=row_id,
                detail="仅已审批报销计入项目成本",
            )
            continue
        amount_ex = row.get("amount_ex_tax")
        amount_inc = row.get("amount_inc_tax")
        if amount_ex is None or amount_inc is None:
            _blocker(
                blockers,
                "missing_expense_amount",
                entity_id=row_id,
                detail="已审批报销金额不完整",
            )
            continue
        total_ex += _money(amount_ex, "报销未税金额")
        total_inc += _money(amount_inc, "报销含税金额")
    return total_ex, total_inc


def _inventory_preview(
    payload: Mapping[str, Any],
    *,
    cutover_date: date,
    blockers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    quantities: dict[str, dict[str, Decimal]] = {}
    for row in payload.get("opening_balances") or []:
        key = _required_text(row.get("balance_key"), "库存期初稳定键", max_length=256)
        if key in quantities:
            raise MigrationControlError("库存期初稳定键重复")
        opening = _qty(row.get("quantity"), "库存期初数量")
        _hash(row.get("evidence_hash"), "库存期初证据哈希")
        if row.get("approved") is not True:
            _blocker(
                blockers,
                "opening_balance_not_approved",
                entity_id=key,
                detail="库存期初数量尚未实名审批",
            )
        quantities[key] = {
            "opening": opening,
            "delivery": Decimal("0"),
            "available_receipt": Decimal("0"),
            "site_issue": Decimal("0"),
            "return_registration": Decimal("0"),
        }
    if not quantities:
        _blocker(
            blockers,
            "missing_opening_balance",
            detail="项目缺少切换日库存 opening balance",
        )

    seen_movement_ids: set[str] = set()
    for row in payload.get("inventory_movements") or []:
        movement_id = _required_text(row.get("movement_id"), "库存变动稳定编号")
        if movement_id in seen_movement_ids:
            raise MigrationControlError("库存变动稳定编号重复")
        seen_movement_ids.add(movement_id)
        key = _required_text(row.get("balance_key"), "库存期初稳定键", max_length=256)
        movement_type = str(row.get("movement_type") or "")
        if movement_type not in _INVENTORY_MOVEMENT_TYPES:
            _blocker(
                blockers,
                "unknown_inventory_movement",
                entity_id=movement_id,
                detail="库存变动类型未映射",
            )
            continue
        try:
            movement_date = _parse_date(row.get("document_date"), "库存变动单据日期")
        except MigrationControlError:
            _blocker(
                blockers,
                "inventory_movement_date_overlap",
                entity_id=movement_id,
                detail="库存变动缺少有效单据日期，不能证明发生在切换日后",
            )
            continue
        if movement_date < cutover_date:
            _blocker(
                blockers,
                "inventory_movement_date_overlap",
                entity_id=movement_id,
                detail="库存变动单据日期早于切换日，已从切换后库存重算中排除",
            )
            continue
        if key not in quantities:
            _blocker(
                blockers,
                "movement_without_opening_balance",
                entity_id=movement_id,
                detail="库存变动找不到对应期初稳定键",
            )
            continue
        quantities[key][movement_type] += _qty(row.get("quantity"), "库存变动数量")

    result: list[dict[str, Any]] = []
    for key in sorted(quantities):
        values = quantities[key]
        closing = values["opening"] - values["delivery"] + values["available_receipt"]
        if closing < 0:
            _blocker(
                blockers,
                "negative_inventory",
                entity_id=key,
                detail="按权威库存变动重算后出现负库存",
            )
        result.append(
            {
                "balance_key": key,
                "opening_quantity": _qty_text(values["opening"]),
                "delivery_quantity": _qty_text(values["delivery"]),
                "available_receipt_quantity": _qty_text(values["available_receipt"]),
                "closing_quantity": _qty_text(closing),
                "ignored_site_issue_quantity": _qty_text(values["site_issue"]),
                "ignored_return_registration_quantity": _qty_text(
                    values["return_registration"]
                ),
            }
        )
    return result


def _project_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable, whitelisted audit view without copying arbitrary source fields."""

    def rows(
        source: Any, *, identity: str, fields: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        output = [
            {field: _canonical(row.get(field)) for field in fields if field in row}
            for row in (source or [])
            if isinstance(row, Mapping)
        ]
        return sorted(output, key=lambda row: str(row.get(identity) or ""))

    baseline = payload.get("historical_baseline")
    baseline_evidence = None
    if isinstance(baseline, Mapping):
        baseline_evidence = {
            field: _canonical(baseline.get(field))
            for field in (
                "amount_ex_tax",
                "amount_inc_tax",
                "evidence_hash",
                "approved",
            )
            if field in baseline
        }
    site_issue_fields = (
        "issue_line_id",
        "issue_id",
        "issue_no",
        "issue_date",
        "pn",
        "sn",
        "quantity",
        "workflow_status",
        "stable_identity",
        "cost_amount_ex_tax",
        "cost_amount_inc_tax",
    )
    return {
        "historical_baseline": baseline_evidence,
        "historical_site_issues": rows(
            payload.get("historical_site_issues"),
            identity="issue_line_id",
            fields=site_issue_fields,
        ),
        "post_cutover_site_issues": rows(
            payload.get("post_cutover_site_issues"),
            identity="issue_line_id",
            fields=site_issue_fields,
        ),
        "expenses": rows(
            payload.get("approved_expenses"),
            identity="expense_id",
            fields=(
                "expense_id",
                "expense_ref",
                "expense_date",
                "normalized_status",
                "amount_ex_tax",
                "amount_inc_tax",
            ),
        ),
        "opening_balances": rows(
            payload.get("opening_balances"),
            identity="balance_key",
            fields=("balance_key", "pn", "quantity", "evidence_hash", "approved"),
        ),
        "inventory_movements": rows(
            payload.get("inventory_movements"),
            identity="movement_id",
            fields=(
                "movement_id",
                "document_id",
                "document_no",
                "document_date",
                "movement_type",
                "balance_key",
                "pn",
                "sn",
                "quantity",
            ),
        ),
        "source_coverage": _canonical(payload.get("source_coverage") or {}),
    }


def build_project_preview(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build one project preview without mutating operational facts."""

    project_id = _required_text(payload.get("project_id"), "项目稳定编号")
    cutover_date = _parse_date(payload.get("cutover_date"), "切换日期")
    source_snapshot_hash = _hash(payload.get("source_snapshot_hash"), "来源快照哈希")
    blockers: list[dict[str, Any]] = []
    _source_blockers(payload, blockers)
    historical_ex, historical_inc = _historical_cost(
        payload,
        cutover_date=cutover_date,
        blockers=blockers,
    )
    consumption_ex, consumption_inc = _post_cutover_cost(
        payload,
        cutover_date=cutover_date,
        blockers=blockers,
    )
    expense_ex, expense_inc = _expense_cost(payload, blockers=blockers)
    inventory = _inventory_preview(
        payload,
        cutover_date=cutover_date,
        blockers=blockers,
    )
    blockers.sort(key=lambda row: (row["code"], row.get("entity_id") or ""))
    project_input_fingerprint = canonical_hash(payload)
    return {
        "project_id": project_id,
        "cutover_date": cutover_date.isoformat(),
        "historical_mode": payload.get("historical_mode"),
        "source_snapshot_hash": source_snapshot_hash,
        "project_input_fingerprint": project_input_fingerprint,
        "cost": {
            "historical_baseline_ex_tax": _money_text(historical_ex),
            "historical_baseline_inc_tax": _money_text(historical_inc),
            "post_cutover_consumption_ex_tax": _money_text(consumption_ex),
            "post_cutover_consumption_inc_tax": _money_text(consumption_inc),
            "approved_expense_ex_tax": _money_text(expense_ex),
            "approved_expense_inc_tax": _money_text(expense_inc),
            "total_ex_tax": _money_text(historical_ex + consumption_ex + expense_ex),
            "total_inc_tax": _money_text(
                historical_inc + consumption_inc + expense_inc
            ),
        },
        "inventory": inventory,
        "evidence": _project_evidence(payload),
        "ignored_return_offset_count": len(payload.get("return_offsets") or []),
        "approval_blockers": blockers,
        "can_approve": not blockers,
    }


def build_migration_preview(
    *,
    rule_version: str,
    projects: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if rule_version != RULE_VERSION:
        raise MigrationControlError("迁移规则版本不一致")
    if not projects:
        raise MigrationControlError("迁移项目清单不能为空")
    project_ids = [
        _required_text(project.get("project_id"), "项目稳定编号")
        for project in projects
    ]
    if len(project_ids) != len(set(project_ids)):
        raise MigrationControlError("迁移项目重复")

    ordered_inputs = sorted(projects, key=lambda row: str(row.get("project_id")))
    previews = [build_project_preview(project) for project in ordered_inputs]
    input_fingerprint = canonical_hash(
        {"rule_version": rule_version, "projects": ordered_inputs}
    )
    source_snapshot_hash = canonical_hash(
        [
            {
                "project_id": row["project_id"],
                "source_snapshot_hash": row["source_snapshot_hash"],
            }
            for row in previews
        ]
    )
    blocker_count = sum(len(row["approval_blockers"]) for row in previews)
    return {
        "rule_version": rule_version,
        "input_fingerprint": input_fingerprint,
        "source_snapshot_hash": source_snapshot_hash,
        "projects": previews,
        "approval_blocker_count": blocker_count,
        "can_approve": blocker_count == 0,
        "production_activation_included": False,
    }


def validate_approval(
    preview: Mapping[str, Any],
    *,
    supplied_fingerprint: str,
    current_fingerprint: str,
) -> None:
    expected = str(preview.get("input_fingerprint") or "")
    if supplied_fingerprint != expected:
        raise MigrationControlError("提交的预览指纹无效")
    if current_fingerprint != expected:
        raise MigrationControlError("来源输入已经变化，请重新生成 dry-run")
    if preview.get("rule_version") != RULE_VERSION:
        raise MigrationControlError("迁移规则版本不一致")
    if preview.get("approval_blocker_count") or preview.get("can_approve") is not True:
        raise MigrationControlError("迁移仍有未解决差异，不能审批")
