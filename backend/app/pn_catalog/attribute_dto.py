"""One managed JSON field group, with explicit null versus deletion semantics."""
import json
import math
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .json_types import JsonValue
from .dto import StrictDTO

MAX_KEYS = 256
MAX_BYTES = 48 * 1024
MAX_NODES = 4096
MAX_DEPTH = 8


def validate_attributes(values: dict) -> dict:
    if type(values) is not dict or len(values) > MAX_KEYS:
        raise ValueError("attributes must contain at most 256 keys")
    nodes = 0

    def visit(value, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("attributes exceed JSON depth or node budget")
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float and math.isfinite(value):
            return
        if type(value) is list:
            for item in value:
                visit(item, depth + 1)
            return
        if type(value) is dict and all(type(k) is str for k in value):
            for item in value.values():
                visit(item, depth + 1)
            return
        raise ValueError("attributes must contain finite JSON values")

    for key, value in values.items():
        if type(key) is not str or not key.strip() or len(key) > 256 or any(ord(c) < 32 for c in key):
            raise ValueError("attribute key is empty, too long or contains control characters")
        visit(value, 0)
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("attributes exceed the 48 KiB JSON budget")
    return values


class SetAttribute(StrictDTO):
    operation: Literal["set"]
    value: JsonValue

    @field_validator("value", mode="before")
    @classmethod
    def json_only(cls, value):
        validate_attributes({"value": value})
        return value


class DeleteAttribute(StrictDTO):
    operation: Literal["delete"]


AttributeChange = Annotated[SetAttribute | DeleteAttribute, Field(discriminator="operation")]


class PatchAttributes(StrictDTO):
    expected_identity_revision: int = Field(ge=0)
    field_versions: dict[str, int] = Field(min_length=1, max_length=MAX_KEYS)
    changes: dict[str, AttributeChange] = Field(min_length=1, max_length=MAX_KEYS)

    @model_validator(mode="after")
    def exact_baseline(self):
        if set(self.field_versions) != set(self.changes) or any(v < 0 for v in self.field_versions.values()):
            raise ValueError("every changed key requires exactly one nonnegative baseline")
        validate_attributes({key: change.value if change.operation == "set" else None
                             for key, change in self.changes.items()})
        return self
