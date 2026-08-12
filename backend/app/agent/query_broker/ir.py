"""Strict model-facing Query IR v1.

There is deliberately no ``sql``, expression, alias, offset, HAVING, CTE, or
free-form query field.  String filter values are data and are never parsed as
SQL; only server-owned identifiers reach the compiler.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DatasetName = Literal[
    "part_catalog_v1",
    "purchase_activity_v1",
    "sales_market_month_v1",
]
FilterOperator = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in"]
OrderDirection = Literal["asc", "desc"]


def _has_forbidden_unicode(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def _validate_identifier(value: str) -> str:
    if not value or len(value) > 64:
        raise ValueError("identifier length is invalid")
    if not ("a" <= value[0] <= "z"):
        raise ValueError("identifier must start with lowercase ASCII")
    if any(not ("a" <= char <= "z" or "0" <= char <= "9" or char == "_") for char in value):
        raise ValueError("identifier must be lowercase ASCII")
    return value


def _validate_scalar(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > 128:
            raise ValueError("string filter value is too long")
        if _has_forbidden_unicode(value):
            raise ValueError("control, format, and surrogate characters are forbidden")
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError("integer filter value is outside signed-64 range")
        return value
    if isinstance(value, float):
        # Current business numerics are Numeric(14,*).  Keep a conservative
        # generic envelope here, then apply field-specific bounds in Registry.
        if not math.isfinite(value) or abs(value) > 1_000_000_000_000:
            raise ValueError("non-finite numbers are forbidden")
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    raise ValueError("filter value must be a JSON scalar or date")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeRange(_FrozenModel):
    start: date
    end: date


class QueryFilter(_FrozenModel):
    field: str
    operator: FilterOperator
    value: Any = Field(repr=False)

    _field_identifier = field_validator("field")(_validate_identifier)

    @field_validator("value")
    @classmethod
    def _bounded_value(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            if not 1 <= len(value) <= 50:
                raise ValueError("IN list must contain 1..50 values")
            return tuple(_validate_scalar(item) for item in value)
        return _validate_scalar(value)

    @model_validator(mode="after")
    def _operator_shape(self):
        if self.operator == "in" and not isinstance(self.value, tuple):
            raise ValueError("IN requires an array value")
        if self.operator != "in" and isinstance(self.value, tuple):
            raise ValueError("only IN accepts an array value")
        return self


class QueryOrder(_FrozenModel):
    field: str
    direction: OrderDirection = "asc"

    _field_identifier = field_validator("field")(_validate_identifier)


class QueryIR(_FrozenModel):
    version: Literal["query-ir/v1"]
    dataset: DatasetName
    time_range: TimeRange | None = None
    dimensions: tuple[str, ...] = Field(default=(), max_length=4)
    metrics: tuple[str, ...] = Field(default=(), max_length=8)
    filters: tuple[QueryFilter, ...] = Field(default=(), max_length=8, repr=False)
    order_by: tuple[QueryOrder, ...] = Field(default=(), max_length=2)
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("dimensions", "metrics")
    @classmethod
    def _identifiers_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_validate_identifier(value) for value in values)
        if len(set(checked)) != len(checked):
            raise ValueError("duplicate selected fields are forbidden")
        return checked

    @field_validator("limit", mode="before")
    @classmethod
    def _limit_is_real_integer(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("limit must be an integer")
        return value

    @model_validator(mode="after")
    def _selected_and_ordered_fields(self):
        selected = self.dimensions + self.metrics
        if not selected:
            raise ValueError("at least one field must be selected")
        if len(set(selected)) != len(selected):
            raise ValueError("a field cannot be both dimension and metric")
        selected_set = set(selected)
        if any(item.field not in selected_set for item in self.order_by):
            raise ValueError("order_by may only reference selected fields")
        return self
