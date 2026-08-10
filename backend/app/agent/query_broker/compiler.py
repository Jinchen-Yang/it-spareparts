"""Deterministic, parameterized compiler for authorized Query IR only."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.frozen import FrozenDict
from app.agent.query_broker.registry import AuthorizedQuery

COMPILER_VERSION = "query-compiler/1.0.0"
MAX_COMPILED_SQL_BYTES = 32 * 1024
_QUOTED_IDENTIFIER = re.compile(r'"([a-z][a-z0-9_]*)"')


class CompiledQuery(BaseModel):
    """Server-only SQL product.  This type is never accepted from an API/model."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    dataset_name: str
    view_schema: str
    view_name: str
    sql: str = Field(repr=False)
    params: FrozenDict = Field(repr=False)
    output_fields: tuple[str, ...]
    allowed_columns: tuple[str, ...]
    registry_fingerprint: str
    authz_fingerprint: str
    egress_fingerprint: str
    compiler_version: str = COMPILER_VERSION
    compiler_fingerprint: str

    @field_validator("params", mode="before")
    @classmethod
    def _freeze_params(cls, value: Mapping[str, Any] | FrozenDict) -> FrozenDict:
        return value if isinstance(value, FrozenDict) else FrozenDict(value)


def _quote(identifier: str) -> str:
    # All callers use identifiers resolved from the static registry.  Keep this
    # assertion anyway so future registry loaders fail closed.
    if not identifier or any(
        not ("a" <= char <= "z" or "0" <= char <= "9" or char == "_")
        for char in identifier
    ):
        raise QueryBrokerError("COMPILED_SQL_REJECTED")
    return f'"{identifier}"'


def _parameter_shape(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, date):
        return "date"
    if isinstance(value, str):
        return "string"
    return "invalid"


def compiler_fingerprint_payload(
    *,
    dataset_name: str,
    view_schema: str,
    view_name: str,
    sql: str,
    params: Mapping[str, Any],
    output_fields: tuple[str, ...],
    allowed_columns: tuple[str, ...],
    registry_fingerprint: str,
    authz_fingerprint: str,
    egress_fingerprint: str,
) -> dict[str, Any]:
    """Fingerprint compiler shape without hashing low-entropy filter values."""

    return {
        "compiler_version": COMPILER_VERSION,
        "dataset": dataset_name,
        "view": [view_schema, view_name],
        "sql": sql,
        "params": [[name, _parameter_shape(params[name])] for name in sorted(params)],
        "output_fields": list(output_fields),
        "allowed_columns": list(allowed_columns),
        "registry_fingerprint": registry_fingerprint,
        "authz_fingerprint": authz_fingerprint,
        "egress_fingerprint": egress_fingerprint,
    }


def compute_compiler_fingerprint(**kwargs: Any) -> str:
    payload = compiler_fingerprint_payload(**kwargs)
    if any(item[1] == "invalid" for item in payload["params"]):
        raise QueryBrokerError("COMPILED_SQL_REJECTED")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _bound_filter_value(value: Any, value_type: str) -> Any:
    if value_type == "date" and isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise QueryBrokerError("COMPILED_SQL_REJECTED") from None
        if parsed.isoformat() != value:
            raise QueryBrokerError("COMPILED_SQL_REJECTED")
        return parsed
    return value


def compile_query(authorized: AuthorizedQuery) -> CompiledQuery:
    """Compile a pre-authorized IR using registry-owned templates only."""

    ir = authorized.ir
    dataset = authorized.dataset
    params: dict[str, Any] = {}
    select_items: list[str] = []
    allowed_columns: set[str] = set()

    for name in ir.dimensions:
        field = dataset.fields[name]
        select_items.append(f"{_quote(field.source_column)} AS {_quote(name)}")
        allowed_columns.add(field.source_column)
    for name in ir.metrics:
        field = dataset.fields[name]
        expression = field.aggregate_expression
        if expression is None:
            raise QueryBrokerError("COMPILED_SQL_REJECTED")
        if ":metric_zero" in expression:
            zero_parameter = f"metric_zero_{name}"
            expression = expression.replace(":metric_zero", f":{zero_parameter}")
            params[zero_parameter] = Decimal(0)
        select_items.append(f"{expression} AS {_quote(name)}")
        allowed_columns.update(_QUOTED_IDENTIFIER.findall(expression))

    where: list[str] = []
    if ir.time_range is not None:
        if dataset.time_column is None:
            raise QueryBrokerError("COMPILED_SQL_REJECTED")
        start = ir.time_range.start
        end = ir.time_range.end
        if dataset.name == "sales_market_month_v1":
            start = _month_start(start)
            end = _month_start(end)
        params["time_start"] = start
        params["time_end"] = end
        allowed_columns.add(dataset.time_column)
        where.extend((
            f"{_quote(dataset.time_column)} >= :time_start",
            f"{_quote(dataset.time_column)} <= :time_end",
        ))

    binary_operators = {
        "eq": "=",
        "ne": "<>",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }
    for index, item in enumerate(ir.filters):
        field = dataset.fields[item.field]
        column = field.source_column
        allowed_columns.add(column)
        if item.operator == "in":
            placeholders = []
            for item_index, value in enumerate(item.value):
                parameter = f"filter_{index}_{item_index}"
                params[parameter] = _bound_filter_value(value, field.value_type)
                placeholders.append(f":{parameter}")
            where.append(f"{_quote(column)} IN ({', '.join(placeholders)})")
        else:
            parameter = f"filter_{index}"
            params[parameter] = _bound_filter_value(item.value, field.value_type)
            where.append(f"{_quote(column)} {binary_operators[item.operator]} :{parameter}")

    if authorized.k_anonymity_threshold is not None:
        params["scope_k_min"] = authorized.k_anonymity_threshold
        allowed_columns.add("sales_order_count")
        where.append('"sales_order_count" >= :scope_k_min')

    lines = [
        f"SELECT {', '.join(select_items)}",
        f"FROM {_quote(dataset.view_schema)}.{_quote(dataset.view_name)}",
    ]
    if where:
        lines.append(f"WHERE {' AND '.join(where)}")
    if dataset.mode == "aggregate" and ir.dimensions:
        group_columns = [
            _quote(dataset.fields[name].source_column) for name in ir.dimensions
        ]
        lines.append(f"GROUP BY {', '.join(group_columns)}")
    if ir.order_by:
        order_items = [
            f"{_quote(item.field)} {item.direction.upper()}" for item in ir.order_by
        ]
        lines.append(f"ORDER BY {', '.join(order_items)}")
    params["result_limit"] = ir.limit + 1
    lines.append("LIMIT :result_limit")
    sql = "\n".join(lines)
    if len(sql.encode()) > MAX_COMPILED_SQL_BYTES:
        raise QueryBrokerError("COMPILED_SQL_REJECTED")

    output_fields = ir.dimensions + ir.metrics
    allowed = tuple(sorted(allowed_columns | set(output_fields)))
    fingerprint_args = {
        "dataset_name": dataset.name,
        "view_schema": dataset.view_schema,
        "view_name": dataset.view_name,
        "sql": sql,
        "params": params,
        "output_fields": output_fields,
        "allowed_columns": allowed,
        "registry_fingerprint": authorized.registry_fingerprint,
        "authz_fingerprint": authorized.authz_fingerprint,
        "egress_fingerprint": authorized.egress_fingerprint,
    }
    return CompiledQuery(
        **fingerprint_args,
        compiler_fingerprint=compute_compiler_fingerprint(**fingerprint_args),
    )
