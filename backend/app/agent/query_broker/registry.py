"""Versioned semantic registry and structural authorization.

Physical view/column expressions stay server-side.  ``visible_registry``
projects only logical names and operations after current RBAC filtering.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.ir import QueryFilter, QueryIR
from app.business_time import business_today

REGISTRY_VERSION = "semantic-registry/v1"
REGISTRY_IMPLEMENTATION_VERSION = "query-registry/1.0.0"
K_ANONYMITY_THRESHOLD = 3

FieldKind = Literal["dimension", "metric"]
ValueType = Literal["string", "integer", "decimal", "date"]
DatasetMode = Literal["direct", "aggregate"]


class AuthorizationSnapshot(BaseModel):
    """Immutable server-derived identity and row/data permission snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=128, repr=False)
    tenant_id: str = Field(min_length=1, max_length=128, repr=False)
    role: str = Field(min_length=1, max_length=64)
    permissions: frozenset[str] = Field(repr=False)
    authz_version: int = Field(ge=0)
    own_customers_only: bool = False
    row_subject: str | None = Field(default=None, max_length=128, repr=False)

    @field_validator("subject", "tenant_id", "role", "row_subject")
    @classmethod
    def _no_control_or_surrogate(cls, value: str | None) -> str | None:
        if value is not None and any(
            unicodedata.category(char).startswith("C") for char in value
        ):
            raise ValueError("control, format, and surrogate characters are forbidden")
        return value

    @field_validator("permissions")
    @classmethod
    def _permission_keys_are_canonical(cls, values: frozenset[str]) -> frozenset[str]:
        for value in values:
            if not value or len(value) > 64 or any(
                not ("a" <= char <= "z" or "0" <= char <= "9" or char == "_")
                for char in value
            ):
                raise ValueError("permission key is not canonical")
        return values

    def fingerprint(self) -> str:
        payload = {
            "subject": self.subject,
            "tenant_id": self.tenant_id,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "authz_version": self.authz_version,
            "own_customers_only": self.own_customers_only,
            "row_subject": self.row_subject,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    kind: FieldKind
    value_type: ValueType
    source_column: str
    allowed_operators: frozenset[str] = frozenset()
    required_permission: str | None = None
    aggregate_expression: str | None = None
    required_dimensions: frozenset[str] = frozenset()
    sensitivity: str = "internal"
    caveat: str | None = None


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    name: str
    semantic_version: str
    implementation_version: str
    view_schema: str
    view_name: str
    mode: DatasetMode
    required_permissions: frozenset[str]
    fields: Mapping[str, FieldSpec]
    time_column: str | None = None
    time_range_required: bool = False
    max_days: int | None = None
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_permissions", frozenset(self.required_permissions))
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "caveats", tuple(self.caveats))

    @property
    def allowed_internal_columns(self) -> frozenset[str]:
        return frozenset(field.source_column for field in self.fields.values())


def _dimension(
    name: str,
    value_type: ValueType = "string",
    *,
    operators: tuple[str, ...] = ("eq", "ne", "in"),
    permission: str | None = None,
    sensitivity: str = "internal",
) -> FieldSpec:
    return FieldSpec(
        name=name,
        kind="dimension",
        value_type=value_type,
        source_column=name,
        allowed_operators=frozenset(operators),
        required_permission=permission,
        sensitivity=sensitivity,
    )


def _metric(
    name: str,
    value_type: ValueType,
    expression: str,
    *,
    permission: str | None = None,
    required_dimensions: tuple[str, ...] = (),
    caveat: str | None = None,
) -> FieldSpec:
    return FieldSpec(
        name=name,
        kind="metric",
        value_type=value_type,
        source_column=name,
        required_permission=permission,
        aggregate_expression=expression,
        required_dimensions=frozenset(required_dimensions),
        caveat=caveat,
    )


_PART_FIELDS = {
    field.name: field
    for field in (
        _dimension("part_id", "integer", operators=("eq", "ne", "in")),
        _dimension("pn_std"),
        _dimension("description", operators=("eq", "in")),
        _dimension("brand"),
        _dimension("category"),
        _dimension("machine_or_part"),
        _dimension("unit"),
        _dimension("pool_group_id", "integer"),
        _dimension("pool_name"),
        _dimension("pool_member_count", "integer"),
    )
}

_PURCHASE_FIELDS = {
    field.name: field
    for field in (
        _dimension("day", "date", operators=("eq", "gt", "gte", "lt", "lte", "in")),
        _dimension("month", "date", operators=("eq", "gt", "gte", "lt", "lte", "in")),
        _dimension("part_id", "integer"),
        _dimension("pn_std"),
        _dimension("brand"),
        _dimension("category"),
        _dimension("source_type"),
        _dimension("supplier_name", permission="data_supplier", sensitivity="confidential"),
        _dimension("source_channel", permission="data_supplier", sensitivity="confidential"),
        _metric(
            "purchase_order_count",
            "integer",
            'SUM("purchase_order_count")',
            required_dimensions=("part_id",),
            caveat="去重采购单数仅在正式 part_id 粒度可加总",
        ),
        _metric("purchase_line_count", "integer", 'SUM("purchase_line_count")'),
        _metric("qty", "decimal", 'SUM("qty")'),
        _metric(
            "amount_inc_tax",
            "decimal",
            'SUM("amount_inc_tax")',
            permission="data_purchase_cost",
        ),
        _metric(
            "amount_ex_tax",
            "decimal",
            'SUM("amount_ex_tax")',
            permission="data_purchase_cost",
        ),
        _metric(
            "weighted_unit_price_inc_tax",
            "decimal",
            'SUM("amount_inc_tax") / NULLIF(SUM("qty"), :metric_zero)',
            permission="data_purchase_cost",
        ),
        _metric(
            "weighted_unit_price_ex_tax",
            "decimal",
            'SUM("amount_ex_tax") / NULLIF(SUM("qty"), :metric_zero)',
            permission="data_purchase_cost",
        ),
        _metric(
            "min_unit_price_inc_tax",
            "decimal",
            'MIN("min_unit_price_inc_tax")',
            permission="data_purchase_cost",
        ),
        _metric(
            "max_unit_price_inc_tax",
            "decimal",
            'MAX("max_unit_price_inc_tax")',
            permission="data_purchase_cost",
        ),
        _metric(
            "latest_purchase_date",
            "date",
            'MAX("latest_purchase_date")',
        ),
    )
}

_SALES_FIELDS = {
    field.name: field
    for field in (
        _dimension("month", "date", operators=("eq", "gt", "gte", "lt", "lte", "in")),
        _dimension("part_id", "integer"),
        _dimension("pn_std"),
        _dimension("brand"),
        _dimension("category"),
        _metric("sales_qty", "decimal", '"sales_qty"'),
        _metric("sales_amount_inc_tax", "decimal", '"sales_amount_inc_tax"'),
        _metric("sales_amount_ex_tax", "decimal", '"sales_amount_ex_tax"'),
        _metric("weighted_sale_price_inc_tax", "decimal", '"weighted_sale_price_inc_tax"'),
        _metric("weighted_sale_price_ex_tax", "decimal", '"weighted_sale_price_ex_tax"'),
        _metric(
            "sales_order_count",
            "integer",
            '"sales_order_count"',
            required_dimensions=("month", "part_id"),
            caveat="own-only 固定按 month×part_id 做 k>=3 抑制",
        ),
    )
}


DATASETS: Mapping[str, DatasetSpec] = MappingProxyType({
    "part_catalog_v1": DatasetSpec(
        name="part_catalog_v1",
        semantic_version="part-catalog/1",
        implementation_version="part-catalog-view/1",
        view_schema="agent_semantic",
        view_name="part_catalog_v1",
        mode="direct",
        required_permissions=frozenset({"page_chat", "page_parts"}),
        fields=_PART_FIELDS,
        caveats=("仅 active 正式型号与 active 互通池身份",),
    ),
    "purchase_activity_v1": DatasetSpec(
        name="purchase_activity_v1",
        semantic_version="purchase-activity/1",
        implementation_version="purchase-activity-view/1",
        view_schema="agent_semantic",
        view_name="purchase_activity_v1",
        mode="aggregate",
        required_permissions=frozenset({"page_chat", "page_purchases"}),
        fields=_PURCHASE_FIELDS,
        time_column="day",
        time_range_required=True,
        max_days=366,
        caveats=(
            "仅 data_status=已生效",
            "采购含税单价按是否含税字段，含税转未税固定除以1.13",
        ),
    ),
    "sales_market_month_v1": DatasetSpec(
        name="sales_market_month_v1",
        semantic_version="sales-market-month/1",
        implementation_version="sales-market-month-view/1",
        view_schema="agent_semantic",
        view_name="sales_market_month_v1",
        mode="direct",
        required_permissions=frozenset({"page_chat", "page_parts"}),
        fields=_SALES_FIELDS,
        time_column="month",
        time_range_required=True,
        max_days=366,
        caveats=(
            "固定 month×part_id 市场聚合，不含客户、销售员、订单、SN、成本和利润",
            "销售含税价转未税固定除以1.13",
            "仅允许查询已经结束的完整自然月；当前未结束月份不进入该数据集",
        ),
    ),
})


class AuthorizedQuery(BaseModel):
    """Server-only authorized IR; physical DatasetSpec is resolved separately."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    ir: QueryIR = Field(repr=False)
    dataset_name: str
    authz_fingerprint: str
    registry_fingerprint: str
    k_anonymity_threshold: int | None = None
    caveats: tuple[str, ...]

    @property
    def dataset(self) -> DatasetSpec:
        return DATASETS[self.dataset_name]


def _registry_fingerprint(dataset: DatasetSpec) -> str:
    fields = []
    for name in sorted(dataset.fields):
        field = dataset.fields[name]
        fields.append({
            "name": field.name,
            "kind": field.kind,
            "type": field.value_type,
            "source": field.source_column,
            "operators": sorted(field.allowed_operators),
            "permission": field.required_permission,
            "expression": field.aggregate_expression,
            "required_dimensions": sorted(field.required_dimensions),
            "sensitivity": field.sensitivity,
            "caveat": field.caveat,
        })
    payload = {
        "registry": REGISTRY_VERSION,
        "implementation": REGISTRY_IMPLEMENTATION_VERSION,
        "dataset": dataset.name,
        "semantic": dataset.semantic_version,
        "view": [dataset.view_schema, dataset.view_name],
        "mode": dataset.mode,
        "permissions": sorted(dataset.required_permissions),
        "time_column": dataset.time_column,
        "time_range_required": dataset.time_range_required,
        "max_days": dataset.max_days,
        "caveats": list(dataset.caveats),
        "fields": fields,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def dataset_registry_fingerprint(dataset_name: str) -> str:
    """Return the current server-owned fingerprint for an allowlisted dataset."""

    dataset = DATASETS.get(dataset_name)
    if dataset is None:
        raise QueryBrokerError("COMPILED_SQL_REJECTED")
    return _registry_fingerprint(dataset)


def _validate_registry_at_import() -> None:
    def safe_identifier(value: str) -> bool:
        return bool(value) and len(value) <= 64 and all(
            "a" <= char <= "z" or "0" <= char <= "9" or char == "_" for char in value
        )

    for name, dataset in DATASETS.items():
        if name != dataset.name or not safe_identifier(name):
            raise RuntimeError("invalid Query Broker dataset registry")
        if dataset.view_schema != "agent_semantic" or dataset.view_name != name:
            raise RuntimeError("invalid Query Broker physical view registry")
        for field_name, field in dataset.fields.items():
            if (
                field_name != field.name
                or not safe_identifier(field_name)
                or not safe_identifier(field.source_column)
                or (field.kind == "dimension" and field.aggregate_expression is not None)
                or (field.kind == "metric" and field.aggregate_expression is None)
            ):
                raise RuntimeError("invalid Query Broker field registry")
            expression = field.aggregate_expression or ""
            if any(token in expression for token in (";", "--", "/*", "*/", "\x00")):
                raise RuntimeError("invalid Query Broker metric expression")


_validate_registry_at_import()
REGISTRY_POLICY_FINGERPRINT = hashlib.sha256(
    json.dumps(
        [[name, dataset_registry_fingerprint(name)] for name in sorted(DATASETS)],
        separators=(",", ":"),
    ).encode()
).hexdigest()


def _field_visible(field: FieldSpec, authz: AuthorizationSnapshot) -> bool:
    return field.required_permission is None or field.required_permission in authz.permissions


def _validate_time_range(ir: QueryIR, dataset: DatasetSpec, today: date) -> None:
    time_range = ir.time_range
    if dataset.time_range_required and time_range is None:
        raise QueryBrokerError("TIME_RANGE_REQUIRED")
    if time_range is None:
        return
    if dataset.time_column is None:
        raise QueryBrokerError("TIME_RANGE_INVALID")
    if time_range.start > time_range.end:
        raise QueryBrokerError("TIME_RANGE_INVALID")
    if time_range.end > today:
        raise QueryBrokerError("TIME_RANGE_IN_FUTURE")
    if dataset.max_days is not None:
        inclusive_days = (time_range.end - time_range.start).days + 1
        if inclusive_days > dataset.max_days:
            raise QueryBrokerError("TIME_RANGE_TOO_WIDE")


def _value_matches(value: Any, value_type: ValueType) -> bool:
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 2**63 - 1
        )
    if value_type == "decimal":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and abs(value) <= 1_000_000_000_000
        )
    if value_type == "date":
        if isinstance(value, date):
            return True
        if isinstance(value, str):
            try:
                return date.fromisoformat(value).isoformat() == value
            except ValueError:
                return False
        return False
    return False


def _validate_filter(item: QueryFilter, field: FieldSpec) -> None:
    if field.kind != "dimension":
        raise QueryBrokerError("FIELD_KIND_MISMATCH")
    if item.operator not in field.allowed_operators:
        raise QueryBrokerError("FILTER_OPERATOR_NOT_ALLOWED")
    values = item.value if isinstance(item.value, tuple) else (item.value,)
    if not all(_value_matches(value, field.value_type) for value in values):
        raise QueryBrokerError("FILTER_TYPE_INVALID")


def authorize_query(
    ir: QueryIR,
    authz: AuthorizationSnapshot,
    *,
    today: date | None = None,
) -> AuthorizedQuery:
    """Authorize every field use before compiler or database access."""

    dataset = DATASETS[ir.dataset]
    if not dataset.required_permissions.issubset(authz.permissions):
        raise QueryBrokerError("DATASET_NOT_VISIBLE")
    effective_today = today or business_today()
    _validate_time_range(ir, dataset, effective_today)
    if dataset.name == "sales_market_month_v1" and ir.time_range is not None:
        start = ir.time_range.start
        end = ir.time_range.end
        end_of_month = calendar.monthrange(end.year, end.month)[1]
        current_month_start = date(effective_today.year, effective_today.month, 1)
        if (
            start.day != 1
            or end.day != end_of_month
            or end >= current_month_start
        ):
            raise QueryBrokerError("TIME_RANGE_INVALID")
        calendar_months = (end.year - start.year) * 12 + end.month - start.month + 1
        if calendar_months > 12:
            raise QueryBrokerError("TIME_RANGE_TOO_WIDE")

    selected_dimensions = set(ir.dimensions)
    selected_metrics = set(ir.metrics)
    all_uses = list(ir.dimensions) + list(ir.metrics)
    all_uses += [item.field for item in ir.filters]
    all_uses += [item.field for item in ir.order_by]
    for name in all_uses:
        field = dataset.fields.get(name)
        if field is None:
            raise QueryBrokerError("UNKNOWN_FIELD")
        if not _field_visible(field, authz):
            raise QueryBrokerError("FIELD_NOT_VISIBLE")

    for name in ir.dimensions:
        if dataset.fields[name].kind != "dimension":
            raise QueryBrokerError("FIELD_KIND_MISMATCH")
    for name in ir.metrics:
        field = dataset.fields[name]
        if field.kind != "metric":
            raise QueryBrokerError("FIELD_KIND_MISMATCH")
        if not field.required_dimensions.issubset(selected_dimensions):
            raise QueryBrokerError("REQUIRED_DIMENSION_MISSING")
    for item in ir.filters:
        _validate_filter(item, dataset.fields[item.field])

    if dataset.name == "purchase_activity_v1" and {"day", "month"}.issubset(selected_dimensions):
        raise QueryBrokerError("FIELD_KIND_MISMATCH")

    threshold = None
    if dataset.name == "sales_market_month_v1":
        if not {"month", "part_id"}.issubset(selected_dimensions):
            raise QueryBrokerError("REQUIRED_DIMENSION_MISSING")
        if "sales_order_count" in selected_metrics:
            allowed_grain = {"month", "part_id", "pn_std"}
            if not selected_dimensions.issubset(allowed_grain):
                raise QueryBrokerError("SALES_ORDER_COUNT_GRAIN_INVALID")
        if authz.own_customers_only:
            if not authz.row_subject:
                raise QueryBrokerError("ROW_SUBJECT_REQUIRED")
            threshold = K_ANONYMITY_THRESHOLD

    caveats = list(dataset.caveats)
    caveats.extend(
        dataset.fields[name].caveat
        for name in ir.metrics
        if dataset.fields[name].caveat is not None
    )
    if threshold is not None:
        caveats.append(f"own-only 结果固定应用 k>={threshold} 低样本抑制")

    return AuthorizedQuery(
        ir=ir,
        dataset_name=dataset.name,
        authz_fingerprint=authz.fingerprint(),
        registry_fingerprint=_registry_fingerprint(dataset),
        k_anonymity_threshold=threshold,
        caveats=tuple(caveats),
    )


def visible_registry(authz: AuthorizationSnapshot) -> tuple[dict[str, Any], ...]:
    """Model-safe logical registry projection; never contains physical schema."""

    datasets: list[dict[str, Any]] = []
    for dataset in DATASETS.values():
        if not dataset.required_permissions.issubset(authz.permissions):
            continue
        fields = []
        for field in dataset.fields.values():
            if not _field_visible(field, authz):
                continue
            fields.append({
                "name": field.name,
                "kind": field.kind,
                "type": field.value_type,
                "operators": sorted(field.allowed_operators),
                "required_dimensions": sorted(field.required_dimensions),
                "caveat": field.caveat,
            })
        datasets.append({
            "dataset": dataset.name,
            "semantic_version": dataset.semantic_version,
            "fields": fields,
            "caveats": list(dataset.caveats),
        })
    return tuple(datasets)
