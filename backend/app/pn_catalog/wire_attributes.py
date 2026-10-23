"""Generated-client contract for the managed attributes aggregate."""
from typing import Any

from pydantic import Field

from .json_types import JsonValue
from .wire import ResponseMeta, WireModel


class AttributeSnapshot(WireModel):
    value: JsonValue
    deleted: bool
    version: int = Field(ge=1)
    protected: bool
    provenance: dict[str, Any]


class AttributesSnapshot(WireModel):
    part_id: int = Field(gt=0)
    identity_revision: int = Field(ge=0)
    revision: int = Field(ge=0)
    items: dict[str, AttributeSnapshot]


class AttributesResponse(WireModel):
    data: AttributesSnapshot
    meta: ResponseMeta
