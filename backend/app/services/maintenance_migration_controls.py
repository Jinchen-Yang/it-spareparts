"""Pure, deterministic rules for maintenance cutover previews.

This module deliberately has no database writes.  A persisted migration run may
store its result, but the business calculation is a reproducible function of a
hash-bound source snapshot.  Legacy demand quantities and return offsets are not
accepted as cost inputs.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from app import tax_policy
from app.services import maintenance_consumption_cost


RULE_VERSION = "maintenance-cutover-v1"
HISTORICAL_BASELINE_SCOPE = "site_issue_parts_only"
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
_WAREHOUSE_MOVEMENT_MAP = {
    "shipment": "delivery",
    "receipt": "available_receipt",
    "return": "return_registration",
}
MAX_COST_REFERENCE_SAMPLES_PER_LINE = 1_000
_COST_SOURCES = {
    "maint_demand",
    "direct_purchase",
    "purchase_window",
    "sales_window",
    "manual",
}
_WINDOW_COST_SOURCES = {"purchase_window", "sales_window"}
SITE_ISSUE_COST_RESOLUTION_FIELDS = (
    "cost_amount_ex_tax",
    "cost_amount_inc_tax",
    "cost_source",
    "cost_evidence_kind",
    "cost_is_estimate",
    "cost_source_label",
    "price_basis",
    "linked_purchase_line_id",
    "manual_unit_cost",
    "manual_unit_cost_inc_tax",
    "manual_evidence",
    "unit_cost_ex_tax",
    "unit_cost_inc_tax",
    "tax_rate_used",
    "reference_side",
    "reference_sample_ids",
    "reference_sample_count",
    "reference_samples",
    "reference_window_from",
    "reference_window_to",
    "algorithm_version",
)


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


def historical_baseline_aggregation_payload(
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """Canonical, machine-verifiable summary bound to a baseline artifact."""

    amount_ex = _money(baseline.get("amount_ex_tax"), "历史基线未税金额")
    amount_inc = _money(baseline.get("amount_inc_tax"), "历史基线含税金额")
    coverage_from = _parse_date(baseline.get("coverage_from"), "历史基线覆盖起点")
    coverage_through = _parse_date(
        baseline.get("coverage_through"), "历史基线覆盖截止日"
    )
    scope = _required_text(baseline.get("scope"), "历史基线范围", max_length=32)
    locator = _required_text(
        baseline.get("source_artifact_locator"),
        "历史基线来源工件定位",
        max_length=512,
    )
    try:
        source_row_count = int(baseline.get("source_row_count"))
    except (TypeError, ValueError) as exc:
        raise MigrationControlError("历史基线来源行数无效") from exc
    if (
        isinstance(baseline.get("source_row_count"), bool)
        or source_row_count < 0
        or source_row_count > 10_000_000
    ):
        raise MigrationControlError("历史基线来源行数无效")
    return {
        "amount_ex_tax": _money_text(amount_ex),
        "amount_inc_tax": _money_text(amount_inc),
        "coverage_from": coverage_from.isoformat(),
        "coverage_through": coverage_through.isoformat(),
        "scope": scope,
        "excludes_expenses": baseline.get("excludes_expenses") is True,
        "source_artifact_locator": locator,
        "source_row_count": source_row_count,
        "evidence_hash": _hash(baseline.get("evidence_hash"), "历史基线证据哈希"),
    }


def historical_baseline_aggregation_fingerprint(
    baseline: Mapping[str, Any],
) -> str:
    return canonical_hash(historical_baseline_aggregation_payload(baseline))


def validate_historical_baseline_contract(
    baseline: Mapping[str, Any], *, cutover_date: date
) -> dict[str, Any]:
    normalized = historical_baseline_aggregation_payload(baseline)
    amount_ex = _money(normalized["amount_ex_tax"], "历史基线未税金额")
    amount_inc = _money(normalized["amount_inc_tax"], "历史基线含税金额")
    if amount_inc != _money(amount_ex * tax_policy.TAX_FACTOR, "历史基线含税金额"):
        raise MigrationControlError("历史基线含税金额无法由固定 13% 未税金额复算")
    coverage_from = _parse_date(normalized["coverage_from"], "历史基线覆盖起点")
    coverage_through = _parse_date(normalized["coverage_through"], "历史基线覆盖截止日")
    if coverage_from > coverage_through:
        raise MigrationControlError("历史基线覆盖区间不能为空或倒置")
    if coverage_through != cutover_date - timedelta(days=1):
        raise MigrationControlError("历史基线覆盖截止日必须精确为切换日前一日")
    if normalized["scope"] != HISTORICAL_BASELINE_SCOPE:
        raise MigrationControlError("历史基线只能覆盖现场领用备件成本")
    if normalized["excludes_expenses"] is not True:
        raise MigrationControlError("历史基线必须明确排除报销费用")
    supplied_fingerprint = _hash(
        baseline.get("aggregation_fingerprint"), "历史基线聚合指纹"
    )
    expected_fingerprint = canonical_hash(normalized)
    if supplied_fingerprint != expected_fingerprint:
        raise MigrationControlError("历史基线聚合指纹与金额及覆盖范围不一致")
    return {**normalized, "aggregation_fingerprint": supplied_fingerprint}


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


def _signed_money_text(value: Decimal) -> str:
    if not value.is_finite() or abs(value) >= _MONEY_MAX_EXCLUSIVE:
        raise MigrationControlError("新旧口径差额超出允许范围")
    return format(value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _aggregate_money(value: Decimal, label: str) -> Decimal:
    if not value.is_finite() or value < 0 or value >= _MONEY_MAX_EXCLUSIVE:
        raise MigrationControlError(f"{label}汇总超出 Numeric(14,2) 安全范围")
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _qty_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized else "0"


def _hash(value: Any, label: str) -> str:
    clean = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean):
        raise MigrationControlError(f"{label}必须是 64 位 SHA-256")
    return clean


def _part_id_from_balance_key(value: Any, *, project_id: str) -> tuple[str, int]:
    key = _required_text(value, "库存期初稳定键", max_length=256)
    prefix, separator, raw_part_id = key.partition(":")
    try:
        part_id = int(raw_part_id)
    except (TypeError, ValueError) as exc:
        raise MigrationControlError("库存稳定键必须为 project_id:part_id") from exc
    if (
        separator != ":"
        or prefix != project_id
        or raw_part_id != str(part_id)
        or part_id <= 0
        or key != f"{project_id}:{part_id}"
    ):
        raise MigrationControlError("库存稳定键必须为 project_id:part_id")
    return key, part_id


def _validate_inventory_movement_identity(
    row: Mapping[str, Any], *, project_id: str
) -> tuple[str, str]:
    document_id = _required_text(row.get("document_id"), "库存变动单据稳定编号")
    line_id = _required_text(row.get("line_id"), "库存变动明细稳定编号")
    movement_id = _required_text(row.get("movement_id"), "库存变动稳定编号")
    if movement_id != f"{document_id}:{line_id}":
        raise MigrationControlError("库存变动稳定编号必须由 document_id:line_id 生成")
    key, part_id = _part_id_from_balance_key(
        row.get("balance_key"), project_id=project_id
    )
    try:
        supplied_part_id = int(row.get("part_id"))
    except (TypeError, ValueError) as exc:
        raise MigrationControlError("库存变动 part_id 无效") from exc
    if supplied_part_id != part_id or str(row.get("project_id") or "") != project_id:
        raise MigrationControlError("库存变动项目或配件与 balance_key 不一致")

    movement_type = str(row.get("movement_type") or "")
    source = str(row.get("source") or "")
    source_type = str(row.get("source_document_type") or "")
    source_status = str(row.get("source_status") or "")
    if source == "maintenance_warehouse_v1":
        expected = _WAREHOUSE_MOVEMENT_MAP.get(source_type)
        if source_status != "confirmed" or expected != movement_type:
            raise MigrationControlError("仓库单据状态或库存变动映射无效")
        expected_formal_available = source_type == "receipt"
        if row.get("formal_available") is not expected_formal_available:
            raise MigrationControlError("正式可用标记与仓库单据类型不一致")
    elif source == "site_issue_v2":
        if (
            source_type != "site_issue"
            or source_status not in _COST_STATUSES
            or movement_type != "site_issue"
        ):
            raise MigrationControlError("现场领用库存证据状态或来源无效")
    else:
        raise MigrationControlError("库存变动来源契约无效")
    return movement_id, key


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


def _positive_decimal(value: Any, label: str, *, upper: Decimal) -> Decimal:
    number = _decimal(value, label, upper=upper)
    if number <= 0:
        raise MigrationControlError(f"{label}必须大于 0")
    return number


def _optional_date(value: Any, label: str) -> date | None:
    if value is None or value == "":
        return None
    return _parse_date(value, label)


def validate_site_issue_cost_evidence(row: Mapping[str, Any]) -> None:
    """Validate that a priced site-issue line is independently reproducible."""

    source = row.get("cost_source")
    has_ex = row.get("cost_amount_ex_tax") is not None
    has_inc = row.get("cost_amount_inc_tax") is not None
    if source is None:
        if has_ex or has_inc:
            raise MigrationControlError("缺少成本来源但存在成本金额")
        return
    if source not in _COST_SOURCES:
        raise MigrationControlError("成本来源不在允许集合内")
    if not (has_ex and has_inc):
        raise MigrationControlError("已取价成本缺少未税或含税金额")
    cost_amount_ex = _money(row.get("cost_amount_ex_tax"), "现场领用未税成本")
    cost_amount_inc = _money(row.get("cost_amount_inc_tax"), "现场领用含税成本")
    issue_quantity = _positive_decimal(
        row.get("quantity"), "现场领用数量", upper=_QTY_MAX_EXCLUSIVE
    )
    unit_cost_ex = _money(row.get("unit_cost_ex_tax"), "现场领用未税成本单价")
    unit_cost_inc = _money(row.get("unit_cost_inc_tax"), "现场领用含税成本单价")
    tax_rate = _decimal(
        row.get("tax_rate_used"), "现场领用成本税率", upper=Decimal("1")
    )
    if tax_rate != tax_policy.TAX_RATE:
        raise MigrationControlError("现场领用成本税率与固定税务口径不一致")
    if row.get("price_basis") != "ex_tax":
        raise MigrationControlError("成本价格口径必须为 ex_tax")
    algorithm_version = _required_text(
        row.get("algorithm_version"), "成本算法版本", max_length=64
    )
    if algorithm_version != maintenance_consumption_cost.ALGORITHM_VERSION:
        raise MigrationControlError("成本算法版本不是当前可复算版本")
    issue_date = _parse_date(row.get("issue_date"), "现场领用日期")

    raw_samples = row.get("reference_samples")
    raw_ids = row.get("reference_sample_ids")
    if not isinstance(raw_samples, list) or not isinstance(raw_ids, list):
        raise MigrationControlError("成本样本证据必须为数组")
    if len(raw_samples) > MAX_COST_REFERENCE_SAMPLES_PER_LINE:
        raise MigrationControlError("单条现场领用成本样本超过安全上限")
    try:
        sample_count = int(row.get("reference_sample_count"))
    except (TypeError, ValueError) as exc:
        raise MigrationControlError("成本样本数量无效") from exc
    if isinstance(row.get("reference_sample_count"), bool) or sample_count < 0:
        raise MigrationControlError("成本样本数量无效")
    sample_ids = [str(value or "").strip() for value in raw_ids]
    evidence_ids: list[str] = []
    normalized_samples: list[tuple[Decimal, Decimal]] = []
    if (
        sample_count != len(raw_samples)
        or sample_count != len(sample_ids)
        or any(not value for value in sample_ids)
        or len(set(sample_ids)) != len(sample_ids)
    ):
        raise MigrationControlError("成本样本数量或稳定编号不一致")

    expected_side = (
        "maint"
        if source == "maint_demand"
        else "sales" if source == "sales_window" else "purchase"
    )
    for index, sample in enumerate(raw_samples, start=1):
        if not isinstance(sample, Mapping):
            raise MigrationControlError(f"成本样本 {index} 结构无效")
        sample_id = _required_text(
            sample.get("sample_id"), f"成本样本 {index} 稳定编号", max_length=128
        )
        evidence_ids.append(sample_id)
        if source == "maint_demand":
            source_line_id = _required_text(
                sample.get("source_line_id"),
                f"成本样本 {index} 需求明细编号",
                max_length=80,
            )
            if sample_id != f"maintenance-demand:{source_line_id}":
                raise MigrationControlError(
                    f"成本样本 {index} 需求明细稳定编号不一致"
                )
        elif not sample_id.startswith(f"{expected_side}:"):
            raise MigrationControlError(f"成本样本 {index} 来源侧不一致")
        _required_text(
            sample.get("document_no"), f"成本样本 {index} 单据号", max_length=128
        )
        sample_quantity = _positive_decimal(
            sample.get("quantity"), f"成本样本 {index} 数量", upper=_QTY_MAX_EXCLUSIVE
        )
        sample_unit_ex = _positive_decimal(
            sample.get("unit_price_ex_tax"),
            f"成本样本 {index} 未税单价",
            upper=_MONEY_MAX_EXCLUSIVE,
        )
        tax_conversion = sample.get("tax_conversion")
        if tax_conversion not in {"none", "divide_1.13"}:
            raise MigrationControlError(f"成本样本 {index} 税额换算无效")
        if source == "maint_demand" and tax_conversion != "none":
            raise MigrationControlError(f"成本样本 {index} 需求单价不得税额换算")
        if expected_side == "sales" and tax_conversion != "divide_1.13":
            raise MigrationControlError(f"成本样本 {index} 销售价格必须按含税价换算")
        if source == "maint_demand":
            if sample.get("unit_price_raw") not in (None, ""):
                raise MigrationControlError(
                    f"成本样本 {index} 需求单价不得伪装成采购/销售原价"
                )
            expected_sample_unit_ex = sample_unit_ex
        else:
            raw_unit_price = _positive_decimal(
                sample.get("unit_price_raw"),
                f"成本样本 {index} 原始单价",
                upper=_MONEY_MAX_EXCLUSIVE,
            )
            expected_sample_unit_ex = (
                raw_unit_price / tax_policy.TAX_FACTOR
                if tax_conversion == "divide_1.13"
                else _money(raw_unit_price, f"成本样本 {index} 原始未税单价")
            )
            if source == "direct_purchase":
                expected_sample_unit_ex = _money(
                    expected_sample_unit_ex,
                    f"成本样本 {index} 直连采购未税单价",
                )
        if sample_unit_ex != expected_sample_unit_ex:
            raise MigrationControlError(f"成本样本 {index} 未税单价无法由原始单价复算")
        normalized_samples.append((sample_quantity, sample_unit_ex))
        sample_date = _optional_date(
            sample.get("document_date"), f"成本样本 {index} 单据日期"
        )
        raw_distance = sample.get("distance_days")
        if source == "maint_demand":
            if raw_distance not in (None, ""):
                raise MigrationControlError("维保需求样本不得携带价格窗口距离")
        elif source in _WINDOW_COST_SOURCES:
            if sample_date is None:
                raise MigrationControlError(f"成本样本 {index} 缺少单据日期")
            try:
                distance = int(raw_distance)
            except (TypeError, ValueError) as exc:
                raise MigrationControlError(f"成本样本 {index} 日期距离无效") from exc
            if (
                isinstance(raw_distance, bool)
                or distance != abs((sample_date - issue_date).days)
                or distance > 7
            ):
                raise MigrationControlError(f"成本样本 {index} 不在前后 7 天窗口内")
        elif sample_date is None:
            if raw_distance is not None:
                raise MigrationControlError("直连采购无日期时距离也必须留空")
        else:
            try:
                distance = int(raw_distance)
            except (TypeError, ValueError) as exc:
                raise MigrationControlError("直连采购日期距离无效") from exc
            if isinstance(raw_distance, bool) or distance != abs(
                (sample_date - issue_date).days
            ):
                raise MigrationControlError("直连采购日期距离无效")
    if evidence_ids != sample_ids:
        raise MigrationControlError("成本样本数组与稳定编号数组不一致")

    window_from = _optional_date(row.get("reference_window_from"), "成本样本窗口起点")
    window_to = _optional_date(row.get("reference_window_to"), "成本样本窗口终点")
    expected_unit_cost_ex: Decimal
    if source == "maint_demand":
        if sample_count == 0:
            raise MigrationControlError("维保需求取价缺少可复算样本")
        if row.get("reference_side") != "maint":
            raise MigrationControlError("维保需求取价 reference_side 无效")
        if window_from is not None or window_to is not None:
            raise MigrationControlError("维保需求取价不得携带采购/销售价格窗口")
        if row.get("linked_purchase_line_id") is not None:
            raise MigrationControlError("维保需求取价不得绑定采购明细")
        total_sample_quantity = sum(
            (quantity for quantity, _unit_price in normalized_samples),
            start=Decimal("0"),
        )
        expected_unit_cost_ex = _money(
            sum(
                (
                    quantity * unit_price
                    for quantity, unit_price in normalized_samples
                ),
                start=Decimal("0"),
            )
            / total_sample_quantity,
            "维保需求数量加权未税成本单价",
        )
    elif source == "direct_purchase":
        linked_id = row.get("linked_purchase_line_id")
        try:
            normalized_linked_id = int(linked_id)
        except (TypeError, ValueError) as exc:
            raise MigrationControlError("直连采购缺少关联采购明细") from exc
        if normalized_linked_id <= 0 or sample_count != 1:
            raise MigrationControlError("直连采购必须绑定唯一采购样本")
        if sample_ids[0] != f"purchase:{normalized_linked_id}":
            raise MigrationControlError("直连采购样本与关联采购明细不一致")
        if row.get("reference_side") != "purchase":
            raise MigrationControlError("直连采购 reference_side 无效")
        if window_from is not None or window_to is not None:
            raise MigrationControlError("直连采购不得伪装成窗口取价")
        expected_unit_cost_ex = _money(normalized_samples[0][1], "直连采购未税成本单价")
    elif source in _WINDOW_COST_SOURCES:
        if sample_count == 0:
            raise MigrationControlError("窗口取价缺少可复算样本")
        if row.get("reference_side") != expected_side:
            raise MigrationControlError("窗口取价 reference_side 无效")
        if window_from != issue_date - timedelta(
            days=7
        ) or window_to != issue_date + timedelta(days=7):
            raise MigrationControlError("窗口取价必须精确绑定领用日前后 7 天")
        total_sample_quantity = sum(
            (quantity for quantity, _unit_price in normalized_samples),
            start=Decimal("0"),
        )
        expected_unit_cost_ex = _money(
            sum(
                (quantity * unit_price for quantity, unit_price in normalized_samples),
                start=Decimal("0"),
            )
            / total_sample_quantity,
            "窗口数量加权未税成本单价",
        )
    else:
        _required_text(row.get("manual_evidence"), "人工成本证据", max_length=1000)
        if row.get("reference_side") != "manual":
            raise MigrationControlError("人工取价 reference_side 无效")
        if sample_count != 0 or window_from is not None or window_to is not None:
            raise MigrationControlError("人工取价不得携带窗口样本")
        expected_unit_cost_ex = _money(row.get("manual_unit_cost"), "人工未税成本单价")
        manual_unit_cost_inc = _money(
            row.get("manual_unit_cost_inc_tax"), "人工含税成本单价"
        )
        if manual_unit_cost_inc != _money(
            expected_unit_cost_ex * tax_policy.TAX_FACTOR, "人工含税成本单价"
        ):
            raise MigrationControlError("人工含税成本单价无法由未税单价复算")

    expected_unit_cost_inc = _money(
        expected_unit_cost_ex * tax_policy.TAX_FACTOR, "现场领用含税成本单价"
    )
    expected_cost_amount_ex = _money(
        issue_quantity * expected_unit_cost_ex, "现场领用未税成本"
    )
    expected_cost_amount_inc = _money(
        issue_quantity * expected_unit_cost_inc, "现场领用含税成本"
    )
    if unit_cost_ex != expected_unit_cost_ex:
        raise MigrationControlError("现场领用未税成本单价与证据重算结果不一致")
    if unit_cost_inc != expected_unit_cost_inc:
        raise MigrationControlError("现场领用含税成本单价与证据重算结果不一致")
    if cost_amount_ex != expected_cost_amount_ex:
        raise MigrationControlError("现场领用未税成本金额与数量乘单价不一致")
    if cost_amount_inc != expected_cost_amount_inc:
        raise MigrationControlError("现场领用含税成本金额与数量乘单价不一致")


def _cost_resolution_is_current(
    row: Mapping[str, Any], *, row_id: str, blockers: list[dict[str, Any]]
) -> bool:
    current = row.get("current_cost_resolution")
    if not isinstance(current, Mapping):
        _blocker(
            blockers,
            "site_issue_cost_not_revalidated",
            entity_id=row_id,
            detail="现场领用成本未按当前采购/销售事实重新运行取价瀑布",
        )
        return False
    stored_resolution = {
        field: _canonical(row.get(field)) for field in SITE_ISSUE_COST_RESOLUTION_FIELDS
    }
    current_resolution = {
        field: _canonical(current.get(field))
        for field in SITE_ISSUE_COST_RESOLUTION_FIELDS
    }
    stored_hash = _hash(row.get("stored_cost_resolution_hash"), "已存成本解析哈希")
    current_hash = _hash(row.get("current_cost_resolution_hash"), "当前成本解析哈希")
    if stored_hash != canonical_hash(stored_resolution):
        raise MigrationControlError("已存成本解析哈希与白名单证据不一致")
    if current_hash != canonical_hash(current_resolution):
        raise MigrationControlError("当前成本解析哈希与白名单证据不一致")
    current_row = {**dict(row), **current_resolution}
    try:
        validate_site_issue_cost_evidence(current_row)
    except MigrationControlError as exc:
        _blocker(
            blockers,
            "site_issue_invalid_cost_evidence",
            entity_id=row_id,
            detail=f"当前取价瀑布结果不可复算：{exc}",
        )
        return False
    matches = row.get("cost_resolution_matches_current")
    if not isinstance(matches, bool) or matches != (stored_hash == current_hash):
        raise MigrationControlError("成本解析匹配标记与哈希比较结果不一致")
    if not matches:
        _blocker(
            blockers,
            "site_issue_cost_resolution_stale",
            entity_id=row_id,
            detail="已存现场领用成本不再匹配当前采购/销售取价瀑布",
        )
    return matches


def _historical_cost(
    payload: Mapping[str, Any],
    *,
    cutover_date: date,
    as_of: date,
    blockers: list[dict[str, Any]],
) -> tuple[Decimal, Decimal, Decimal, Decimal, int]:
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
            return Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 0
        try:
            normalized_baseline = validate_historical_baseline_contract(
                baseline, cutover_date=cutover_date
            )
        except MigrationControlError as exc:
            _blocker(
                blockers,
                "historical_baseline_contract_invalid",
                detail=f"历史成本基线范围或聚合证据无效：{exc}",
            )
            return Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 0
        amount_ex = _money(normalized_baseline["amount_ex_tax"], "历史基线未税金额")
        amount_inc = _money(normalized_baseline["amount_inc_tax"], "历史基线含税金额")
        if baseline.get("approved") is not True:
            _blocker(
                blockers,
                "historical_baseline_not_approved",
                detail="历史成本基线尚未实名审批",
            )
            return Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 0
        return amount_ex, amount_inc, Decimal("0"), Decimal("0"), 0

    total_ex = Decimal("0")
    total_inc = Decimal("0")
    estimate_ex = Decimal("0")
    estimate_inc = Decimal("0")
    estimate_lines = 0
    seen: set[str] = set()
    counted_rows = 0
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
        if row_date > as_of:
            eligible = False
            _blocker(
                blockers,
                "historical_issue_after_as_of",
                entity_id=row_id,
                detail="历史领用日期晚于本次迁移业务截止日",
            )
        if workflow_status not in _COST_STATUSES:
            eligible = False
            _blocker(
                blockers,
                "historical_issue_not_confirmed",
                entity_id=row_id,
                detail="历史领用尚未确认或已作废",
            )
        if eligible and not _cost_resolution_is_current(
            row, row_id=row_id, blockers=blockers
        ):
            eligible = False
        if eligible:
            try:
                validate_site_issue_cost_evidence(row)
            except MigrationControlError as exc:
                eligible = False
                _blocker(
                    blockers,
                    "historical_issue_invalid_cost_evidence",
                    entity_id=row_id,
                    detail=f"历史领用成本证据不可复算：{exc}",
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
            if row.get("cost_source") == "sales_window":
                estimate_ex += pair[0]
                estimate_inc += pair[1]
                estimate_lines += 1
            counted_rows += 1
    if counted_rows == 0:
        _blocker(
            blockers,
            "missing_historical_site_issues",
            detail="没有可靠历史领用，必须改用已审批成本基线",
        )
    return total_ex, total_inc, estimate_ex, estimate_inc, estimate_lines


def _proposed_historical_cost(
    payload: Mapping[str, Any], *, cutover_date: date
) -> tuple[Decimal, Decimal] | None:
    """Return the candidate-applied historical value used for named signoff.

    The operational total stays fail-closed until approval.  The comparison shown
    to the reconciler must still contain the exact candidate value that approval
    will apply; otherwise the signed truth would change underneath the signoff.
    """

    if payload.get("historical_mode") != "approved_cost_baseline":
        return None
    baseline = payload.get("historical_baseline")
    if not isinstance(baseline, Mapping):
        return Decimal("0"), Decimal("0")
    try:
        normalized = validate_historical_baseline_contract(
            baseline, cutover_date=cutover_date
        )
    except MigrationControlError:
        return Decimal("0"), Decimal("0")
    return (
        _money(normalized["amount_ex_tax"], "历史基线未税金额"),
        _money(normalized["amount_inc_tax"], "历史基线含税金额"),
    )


def _post_cutover_cost(
    payload: Mapping[str, Any],
    *,
    cutover_date: date,
    as_of: date,
    blockers: list[dict[str, Any]],
) -> tuple[Decimal, Decimal, Decimal, Decimal, int]:
    total_ex = Decimal("0")
    total_inc = Decimal("0")
    estimate_ex = Decimal("0")
    estimate_inc = Decimal("0")
    estimate_lines = 0
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
        if row.get("stable_identity") is not True:
            eligible = False
            _blocker(
                blockers,
                "site_issue_missing_identity",
                entity_id=row_id,
                detail="切换后现场领用缺少当前有效的稳定来源归属",
            )
        if row_date < cutover_date:
            eligible = False
            _blocker(
                blockers,
                "post_cutover_issue_date_overlap",
                entity_id=row_id,
                detail="切换后领用日期早于切换日",
            )
        if row_date > as_of:
            eligible = False
            _blocker(
                blockers,
                "site_issue_after_as_of",
                entity_id=row_id,
                detail="现场领用日期晚于本次迁移业务截止日",
            )
        if workflow_status not in _COST_STATUSES:
            eligible = False
            _blocker(
                blockers,
                "unapproved_site_issue",
                entity_id=row_id,
                detail="仅已确认或已更正的现场领用计入成本",
            )
        if eligible and not _cost_resolution_is_current(
            row, row_id=row_id, blockers=blockers
        ):
            eligible = False
        if eligible:
            try:
                validate_site_issue_cost_evidence(row)
            except MigrationControlError as exc:
                eligible = False
                _blocker(
                    blockers,
                    "site_issue_invalid_cost_evidence",
                    entity_id=row_id,
                    detail=f"现场领用成本证据不可复算：{exc}",
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
            if row.get("cost_source") == "sales_window":
                estimate_ex += pair[0]
                estimate_inc += pair[1]
                estimate_lines += 1
    return total_ex, total_inc, estimate_ex, estimate_inc, estimate_lines


def _expense_cost(
    payload: Mapping[str, Any],
    *,
    as_of: date,
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
        expense_date = _parse_date(row.get("expense_date"), "报销日期")
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
        if expense_date > as_of:
            _blocker(
                blockers,
                "expense_after_as_of",
                entity_id=row_id,
                detail="报销日期晚于本次迁移业务截止日",
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
        normalized_ex = _money(amount_ex, "报销未税金额")
        normalized_inc = _money(amount_inc, "报销含税金额")
        if normalized_inc != _money(
            normalized_ex * tax_policy.TAX_FACTOR, "报销含税金额"
        ):
            _blocker(
                blockers,
                "expense_tax_mismatch",
                entity_id=row_id,
                detail="已审批报销含税金额无法按固定 13% 由未税金额复算",
            )
            continue
        total_ex += normalized_ex
        total_inc += normalized_inc
    return total_ex, total_inc


def _legacy_truth(
    payload: Mapping[str, Any],
    *,
    as_of: date,
    blockers: list[dict[str, Any]],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    parts_ex = Decimal("0")
    parts_inc = Decimal("0")
    expenses_ex = Decimal("0")
    expenses_inc = Decimal("0")
    seen_lines: set[str] = set()
    for raw in payload.get("legacy_cost_lines") or []:
        if not isinstance(raw, Mapping):
            raise MigrationControlError("旧口径 WBDD 明细结构无效")
        entity_id = str(raw.get("source_line_id") or "").strip() or None
        try:
            line_id = _required_text(entity_id, "旧口径 WBDD 明细稳定编号")
            if line_id in seen_lines:
                raise MigrationControlError("旧口径 WBDD 明细稳定编号重复")
            seen_lines.add(line_id)
            _required_text(raw.get("source_order_id"), "旧口径 WBDD 单据稳定编号")
            _required_text(raw.get("order_no"), "旧口径 WBDD 单据号")
            _required_text(raw.get("pn"), "旧口径 WBDD PN", max_length=256)
            order_date = _parse_date(raw.get("order_date"), "旧口径 WBDD 日期")
            if order_date > as_of:
                raise MigrationControlError("旧口径 WBDD 日期晚于冻结截止日")
            demand = _qty(raw.get("demand_quantity"), "旧口径需求数量")
            returned = _qty(raw.get("return_quantity"), "旧口径退货数量")
            effective = _qty(raw.get("effective_quantity"), "旧口径有效数量")
            if returned > demand or effective != demand - returned:
                raise MigrationControlError("旧口径有效数量无法由需求减退货复算")
            unit_ex = _money(raw.get("unit_cost_ex_tax"), "旧口径未税单价")
            unit_inc = _money(raw.get("unit_cost_inc_tax"), "旧口径含税单价")
            cost_tax_basis = _required_text(
                raw.get("cost_tax_basis"), "旧口径成本税价基准", max_length=4
            )
            amount_ex = _money(raw.get("cost_amount_ex_tax"), "旧口径未税成本")
            amount_inc = _money(raw.get("cost_amount_inc_tax"), "旧口径含税成本")
            if cost_tax_basis == "ex":
                tax_matches = unit_inc == _money(
                    unit_ex * tax_policy.TAX_FACTOR,
                    "旧口径含税单价",
                )
            elif cost_tax_basis == "inc":
                tax_matches = unit_ex == _money(
                    unit_inc / tax_policy.TAX_FACTOR,
                    "旧口径未税单价",
                )
            else:
                raise MigrationControlError("旧口径成本税价基准无效")
            if not tax_matches:
                raise MigrationControlError("旧口径成本单价不符合固定 13% 口径")
            if amount_ex != _money(effective * unit_ex, "旧口径未税成本"):
                raise MigrationControlError("旧口径未税成本无法由有效数量乘单价复算")
            if amount_inc != _money(effective * unit_inc, "旧口径含税成本"):
                raise MigrationControlError("旧口径含税成本无法由有效数量乘单价复算")
        except MigrationControlError as exc:
            _blocker(
                blockers,
                "legacy_cost_fact_invalid",
                entity_id=entity_id,
                detail=f"旧口径 WBDD 事实不可复算：{exc}",
            )
            continue
        parts_ex += amount_ex
        parts_inc += amount_inc

    seen_expenses: set[str] = set()
    for raw in payload.get("legacy_expenses") or []:
        if not isinstance(raw, Mapping):
            raise MigrationControlError("旧口径 BXD 明细结构无效")
        entity_id = str(raw.get("expense_id") or "").strip() or None
        try:
            expense_id = _required_text(entity_id, "旧口径 BXD 稳定编号")
            if expense_id in seen_expenses:
                raise MigrationControlError("旧口径 BXD 稳定编号重复")
            seen_expenses.add(expense_id)
            expense_date = _parse_date(raw.get("expense_date"), "旧口径 BXD 日期")
            if expense_date > as_of:
                raise MigrationControlError("旧口径 BXD 日期晚于冻结截止日")
            if raw.get("normalized_status") != "approved":
                raise MigrationControlError("旧口径 BXD 不是已审批状态")
            tax_basis = _required_text(
                raw.get("tax_basis"), "旧口径 BXD 税价基准", max_length=16
            )
            amount_ex = _money(raw.get("amount_ex_tax"), "旧口径 BXD 未税金额")
            amount_inc = _money(raw.get("amount_inc_tax"), "旧口径 BXD 含税金额")
            if tax_basis in {"default_ex", "ex"}:
                tax_matches = amount_inc == _money(
                    amount_ex * tax_policy.TAX_FACTOR,
                    "旧口径 BXD 含税金额",
                )
            elif tax_basis == "inc":
                tax_matches = amount_ex == _money(
                    amount_inc / tax_policy.TAX_FACTOR,
                    "旧口径 BXD 未税金额",
                )
            else:
                raise MigrationControlError("旧口径 BXD 税价基准无效")
            if not tax_matches:
                raise MigrationControlError("旧口径 BXD 不符合固定 13% 口径")
        except MigrationControlError as exc:
            _blocker(
                blockers,
                "legacy_expense_fact_invalid",
                entity_id=entity_id,
                detail=f"旧口径 BXD 事实不可复算：{exc}",
            )
            continue
        expenses_ex += amount_ex
        expenses_inc += amount_inc
    return (
        _aggregate_money(parts_ex, "旧口径备件成本"),
        _aggregate_money(parts_inc, "旧口径含税备件成本"),
        _aggregate_money(expenses_ex, "旧口径报销"),
        _aggregate_money(expenses_inc, "旧口径含税报销"),
    )


def _truth_quantity_differences(
    payload: Mapping[str, Any], *, as_of: date
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for raw in payload.get("legacy_cost_lines") or []:
        if not isinstance(raw, Mapping):
            continue
        source_order_id = str(raw.get("source_order_id") or "")
        source_line_id = str(raw.get("source_line_id") or "")
        try:
            if _parse_date(raw.get("order_date"), "旧口径 WBDD 日期") > as_of:
                continue
            before_quantity = _qty(raw.get("effective_quantity"), "旧口径有效数量")
        except MigrationControlError:
            # The source fact has already produced a fail-closed blocker in
            # _legacy_truth; keep the comparison inspectable instead of aborting it.
            continue
        key = f"legacy:{source_order_id}:{source_line_id}"
        rows[key] = {
            "comparison_key": key,
            "source_order_id": source_order_id or None,
            "source_line_id": source_line_id or None,
            "document_no": raw.get("order_no"),
            "pn": raw.get("pn"),
            "sn": raw.get("sn"),
            "before_quantity": before_quantity,
            "after_quantity": Decimal("0"),
        }
    for raw in [
        *(payload.get("historical_site_issues") or []),
        *(payload.get("post_cutover_site_issues") or []),
    ]:
        if not isinstance(raw, Mapping):
            continue
        if (
            raw.get("stable_identity") is not True
            or raw.get("workflow_status") not in _COST_STATUSES
        ):
            continue
        try:
            if _parse_date(raw.get("issue_date"), "现场领用日期") > as_of:
                continue
            issue_quantity = _qty(raw.get("quantity"), "现场领用数量")
        except MigrationControlError:
            # Cost validation records the underlying data-quality blocker.
            continue
        source_order_id = str(raw.get("source_order_id") or "")
        source_line_id = str(raw.get("source_line_id") or "")
        if source_order_id and source_line_id:
            key = f"legacy:{source_order_id}:{source_line_id}"
        else:
            key = f"site_issue:{_required_text(raw.get('issue_line_id'), '现场领用稳定编号')}"
        item = rows.setdefault(
            key,
            {
                "comparison_key": key,
                "source_order_id": source_order_id or None,
                "source_line_id": source_line_id or None,
                "document_no": raw.get("issue_no"),
                "pn": raw.get("pn"),
                "sn": raw.get("sn"),
                "before_quantity": Decimal("0"),
                "after_quantity": Decimal("0"),
            },
        )
        item["after_quantity"] += issue_quantity
    output: list[dict[str, Any]] = []
    for key in sorted(rows):
        item = rows[key]
        before = item.pop("before_quantity")
        after = item.pop("after_quantity")
        output.append(
            {
                **item,
                "before_quantity": _qty_text(before),
                "after_quantity": _qty_text(after),
                "delta_quantity": _qty_text(after - before)
                if after >= before
                else f"-{_qty_text(before - after)}",
            }
        )
    return output


def _inventory_preview(
    payload: Mapping[str, Any],
    *,
    project_id: str,
    cutover_date: date,
    as_of: date,
    blockers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    quantities: dict[str, dict[str, Decimal]] = {}
    for row in payload.get("opening_balances") or []:
        key, part_id = _part_id_from_balance_key(
            row.get("balance_key"), project_id=project_id
        )
        if row.get("part_id") is not None:
            try:
                supplied_part_id = int(row.get("part_id"))
            except (TypeError, ValueError) as exc:
                raise MigrationControlError("库存期初 part_id 无效") from exc
            if supplied_part_id != part_id:
                raise MigrationControlError("库存期初 part_id 与稳定键不一致")
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
        movement_id, key = _validate_inventory_movement_identity(
            row, project_id=project_id
        )
        if movement_id in seen_movement_ids:
            raise MigrationControlError("库存变动稳定编号重复")
        seen_movement_ids.add(movement_id)
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
        if movement_date > as_of:
            _blocker(
                blockers,
                "inventory_movement_after_as_of",
                entity_id=movement_id,
                detail="库存变动日期晚于本次迁移业务截止日",
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


def _project_evidence(
    payload: Mapping[str, Any],
    *,
    truth_quantity_differences: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return a stable, whitelisted audit view without copying arbitrary source fields."""

    def rows(
        source: Any, *, identity: str, fields: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        output = [
            {field: _canonical(row.get(field)) for field in fields}
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
                "coverage_from",
                "coverage_through",
                "scope",
                "excludes_expenses",
                "source_artifact_locator",
                "source_row_count",
                "aggregation_fingerprint",
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
        "link_state",
        "delivery_line_id",
        "source_order_id",
        "source_line_id",
        "source_assignment_id",
        "source_assignment_version",
        "delivery_mapping_version",
        "cost_amount_ex_tax",
        "cost_amount_inc_tax",
        "cost_source",
        "cost_evidence_kind",
        "cost_is_estimate",
        "cost_source_label",
        "price_basis",
        "linked_purchase_line_id",
        "manual_unit_cost",
        "manual_unit_cost_inc_tax",
        "manual_evidence",
        "unit_cost_ex_tax",
        "unit_cost_inc_tax",
        "tax_rate_used",
        "reference_side",
        "reference_sample_ids",
        "reference_sample_count",
        "reference_samples",
        "reference_window_from",
        "reference_window_to",
        "algorithm_version",
        "stored_cost_resolution_hash",
        "current_cost_resolution_hash",
        "cost_resolution_matches_current",
        "current_cost_resolution",
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
        "legacy_cost_lines": rows(
            payload.get("legacy_cost_lines"),
            identity="source_line_id",
            fields=(
                "source_order_id",
                "source_line_id",
                "order_no",
                "order_date",
                "pn",
                "sn",
                "part_id",
                "demand_quantity",
                "return_quantity",
                "effective_quantity",
                "unit_cost_ex_tax",
                "unit_cost_inc_tax",
                "cost_tax_basis",
                "cost_amount_ex_tax",
                "cost_amount_inc_tax",
                "stored_cost_amount_ex_tax",
                "stored_cost_amount_inc_tax",
                "assignment_id",
                "assignment_version",
                "order_import_batch_id",
                "line_import_batch_id",
            ),
        ),
        "legacy_expenses": rows(
            payload.get("legacy_expenses"),
            identity="expense_id",
            fields=(
                "expense_id",
                "expense_ref",
                "expense_date",
                "normalized_status",
                "raw_status",
                "contract_no",
                "project_contract_id",
                "contract_id",
                "contract_relation_version",
                "contract_effective_from",
                "contract_effective_to",
                "tax_basis",
                "amount_ex_tax",
                "amount_inc_tax",
                "import_batch_id",
            ),
        ),
        "truth_quantity_differences": rows(
            truth_quantity_differences,
            identity="comparison_key",
            fields=(
                "comparison_key",
                "source_order_id",
                "source_line_id",
                "document_no",
                "pn",
                "sn",
                "before_quantity",
                "after_quantity",
                "delta_quantity",
            ),
        ),
        "opening_balances": rows(
            payload.get("opening_balances"),
            identity="balance_key",
            fields=(
                "balance_key",
                "part_id",
                "pn",
                "quantity",
                "evidence_hash",
                "approved",
            ),
        ),
        "inventory_movements": rows(
            payload.get("inventory_movements"),
            identity="movement_id",
            fields=(
                "movement_id",
                "document_id",
                "line_id",
                "document_no",
                "document_date",
                "movement_type",
                "source",
                "source_document_type",
                "source_status",
                "formal_available",
                "project_id",
                "part_id",
                "balance_key",
                "pn",
                "source_pn",
                "sn",
                "quantity",
                "source_order_id",
                "source_assignment_id",
                "source_assignment_version",
                "project_link_id",
                "project_link_version",
                "part_link_id",
                "part_link_version",
                "bad_return_id",
                "bad_return_status",
                "bad_return_version",
                "warehouse_import_id",
                "warehouse_source_file_hash",
                "warehouse_adapter_version",
                "warehouse_header_signature",
            ),
        ),
        "warehouse_ambiguities": rows(
            payload.get("warehouse_ambiguities"),
            identity="ambiguity_id",
            fields=(
                "ambiguity_id",
                "import_id",
                "document_id",
                "line_id",
                "document_no",
                "document_date",
                "ambiguity_type",
                "field_code",
                "source_row",
                "value_hash",
                "candidates",
                "fingerprint",
                "status",
                "version",
                "scope",
                "scope_project_ids",
                "scope_reason",
            ),
        ),
        "source_coverage": _canonical(payload.get("source_coverage") or {}),
    }


def build_project_preview(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build one project preview without mutating operational facts."""

    project_id = _required_text(payload.get("project_id"), "项目稳定编号")
    cutover_date = _parse_date(payload.get("cutover_date"), "切换日期")
    as_of = _parse_date(payload.get("as_of"), "迁移业务截止日")
    source_snapshot_hash = _hash(payload.get("source_snapshot_hash"), "来源快照哈希")
    blockers: list[dict[str, Any]] = []
    _source_blockers(payload, blockers)
    if cutover_date > as_of:
        _blocker(
            blockers,
            "cutover_date_after_as_of",
            detail="切换日期晚于本次迁移业务截止日",
        )
    (
        historical_ex,
        historical_inc,
        historical_estimate_ex,
        historical_estimate_inc,
        historical_estimate_lines,
    ) = _historical_cost(
        payload,
        cutover_date=cutover_date,
        as_of=as_of,
        blockers=blockers,
    )
    (
        consumption_ex,
        consumption_inc,
        consumption_estimate_ex,
        consumption_estimate_inc,
        consumption_estimate_lines,
    ) = _post_cutover_cost(
        payload,
        cutover_date=cutover_date,
        as_of=as_of,
        blockers=blockers,
    )
    expense_ex, expense_inc = _expense_cost(payload, as_of=as_of, blockers=blockers)
    (
        legacy_parts_ex,
        legacy_parts_inc,
        legacy_expense_ex,
        legacy_expense_inc,
    ) = _legacy_truth(payload, as_of=as_of, blockers=blockers)
    inventory = _inventory_preview(
        payload,
        project_id=project_id,
        cutover_date=cutover_date,
        as_of=as_of,
        blockers=blockers,
    )
    blockers.sort(key=lambda row: (row["code"], row.get("entity_id") or ""))
    project_input_fingerprint = canonical_hash(payload)
    sales_estimate_ex = historical_estimate_ex + consumption_estimate_ex
    sales_estimate_inc = historical_estimate_inc + consumption_estimate_inc
    sales_estimate_lines = historical_estimate_lines + consumption_estimate_lines
    cost_after_parts_ex = _aggregate_money(
        historical_ex + consumption_ex, "新口径备件成本"
    )
    cost_after_parts_inc = _aggregate_money(
        historical_inc + consumption_inc, "新口径含税备件成本"
    )
    cost_after_total_ex = _aggregate_money(
        cost_after_parts_ex + expense_ex, "新口径总成本"
    )
    cost_after_total_inc = _aggregate_money(
        cost_after_parts_inc + expense_inc, "新口径含税总成本"
    )
    proposed_historical = _proposed_historical_cost(payload, cutover_date=cutover_date)
    truth_historical_ex = (
        proposed_historical[0] if proposed_historical is not None else historical_ex
    )
    truth_historical_inc = (
        proposed_historical[1] if proposed_historical is not None else historical_inc
    )
    truth_after_parts_ex = _aggregate_money(
        truth_historical_ex + consumption_ex,
        "候选应用后新口径备件成本",
    )
    truth_after_parts_inc = _aggregate_money(
        truth_historical_inc + consumption_inc,
        "候选应用后新口径含税备件成本",
    )
    truth_after_total_ex = _aggregate_money(
        truth_after_parts_ex + expense_ex,
        "候选应用后新口径总成本",
    )
    truth_after_total_inc = _aggregate_money(
        truth_after_parts_inc + expense_inc,
        "候选应用后新口径含税总成本",
    )
    before_total_ex = _aggregate_money(
        legacy_parts_ex + legacy_expense_ex, "旧口径总成本"
    )
    before_total_inc = _aggregate_money(
        legacy_parts_inc + legacy_expense_inc, "旧口径含税总成本"
    )
    truth_quantity_differences = _truth_quantity_differences(payload, as_of=as_of)
    before = {
        "parts_cost_ex_tax": _money_text(legacy_parts_ex),
        "parts_cost_inc_tax": _money_text(legacy_parts_inc),
        "approved_expense_ex_tax": _money_text(legacy_expense_ex),
        "approved_expense_inc_tax": _money_text(legacy_expense_inc),
        "total_ex_tax": _money_text(before_total_ex),
        "total_inc_tax": _money_text(before_total_inc),
    }
    after = {
        "parts_cost_ex_tax": _money_text(truth_after_parts_ex),
        "parts_cost_inc_tax": _money_text(truth_after_parts_inc),
        "approved_expense_ex_tax": _money_text(expense_ex),
        "approved_expense_inc_tax": _money_text(expense_inc),
        "total_ex_tax": _money_text(truth_after_total_ex),
        "total_inc_tax": _money_text(truth_after_total_inc),
    }
    delta = {
        key: _signed_money_text(Decimal(after[key]) - Decimal(before[key]))
        for key in before
    }
    truth_hash_payload = {
        "project_id": project_id,
        "as_of": as_of.isoformat(),
        "cutover_date": cutover_date.isoformat(),
        "before": before,
        "after": after,
        "delta": delta,
        "quantity_differences": truth_quantity_differences,
        "opening_balance": inventory,
        "after_candidate_values_applied": True,
        "legacy_source_hash": (payload.get("source_coverage") or {}).get(
            "legacy_source_hash"
        ),
    }
    truth_comparison = {
        "before": before,
        "after": after,
        "delta": delta,
        "after_candidate_values_applied": True,
        "truth_comparison_hash": canonical_hash(truth_hash_payload),
    }
    return {
        "project_id": project_id,
        "cutover_date": cutover_date.isoformat(),
        "as_of": as_of.isoformat(),
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
            "sales_estimate_cost_ex_tax": _money_text(sales_estimate_ex),
            "sales_estimate_cost_inc_tax": _money_text(sales_estimate_inc),
            "sales_estimate_lines": sales_estimate_lines,
            "cost_progress_includes_sales_estimate": sales_estimate_lines > 0,
            "cost_progress_label": (
                "priced_cost_including_sales_estimate"
                if sales_estimate_lines
                else "priced_cost_without_sales_estimate"
            ),
            "total_ex_tax": _money_text(cost_after_total_ex),
            "total_inc_tax": _money_text(cost_after_total_inc),
        },
        "truth_comparison": truth_comparison,
        "inventory": inventory,
        "evidence": _project_evidence(
            payload, truth_quantity_differences=truth_quantity_differences
        ),
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
