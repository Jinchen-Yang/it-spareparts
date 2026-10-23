"""Public response schema. OpenAPI export and both clients consume this contract."""
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(WireModel):
    code: str
    message: str
    request_id: str
    retryable: bool
    retry_after_seconds: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(WireModel):
    error: ErrorDetail


class PartIdentity(WireModel):
    pn_std: str
    status: str
    merged_into_id: int | None


class SpecValue(WireModel):
    value: str | None
    unit: str | None
    numeric_value: str | None
    source: str
    version: int = Field(ge=0)
    deleted: bool


class PartSnapshot(WireModel):
    part_id: int = Field(gt=0)
    pn_std: str
    identity: PartIdentity
    identity_revision: int = Field(ge=0)
    profile_revision: int = Field(ge=0)
    availability: Literal["active", "inactive"]
    availability_revision: int = Field(ge=0)
    profile: dict[str, str | None]
    specs: dict[str, SpecValue]
    field_versions: dict[str, int]
    field_provenance: dict[str, dict[str, Any]]
    locked_fields: list[str]


class ResponseMeta(WireModel):
    request_id: str
    replayed: bool
    resource_revision: int = Field(ge=0)
    data_revision: int = Field(ge=0)
    identity_revision: int = Field(ge=0)
    applied_revision: int | None = Field(default=None, ge=0)
    applied_identity_revision: int | None = Field(default=None, ge=0)
    audit_id: int | None = None


class PartResponse(WireModel):
    data: PartSnapshot
    meta: ResponseMeta


class PageMeta(WireModel):
    request_id: str
    replayed: bool
    next_cursor: str | None


class PartSummary(WireModel):
    part_id: int = Field(gt=0)
    pn_std: str
    identity_revision: int = Field(ge=0)
    profile_revision: int = Field(ge=0)
    availability: Literal["active", "inactive"]
    description_preview: str | None = Field(default=None, max_length=240)
    brand: str | None = None
    manufacturer: str | None = None
    category_major: str | None = None
    category_minor: str | None = None

    has_image: bool | None = None
    main_entry_id: UUID | None = None

class PartListResponse(WireModel):
    data: list[PartSummary]
    meta: PageMeta


class AuditEvent(WireModel):
    audit_id: int
    action: str
    actor: dict[str, Any]
    context: dict[str, Any]
    authorization: dict[str, Any]
    request_id: str
    occurred_at: str
    before_revision: int
    after_revision: int
    revisions: dict[str, Any]
    changes: dict[str, Any]


class AuditListResponse(WireModel):
    data: list[AuditEvent]
    meta: PageMeta
