"""Public job state never includes worker leases, payloads or execution credentials."""
from typing import Literal

from pydantic import Field

from .wire import PageMeta, ResponseMeta, WireModel
from .wire_media import JsonValue, MediaMeta


class JobSnapshot(WireModel):
    job_id: str
    kind: str
    queue: str
    status: Literal["pending", "running", "succeeded", "failed", "cancelled", "stale"]
    resource_type: str
    resource_id: str
    part_id: int | None
    resource_generation: int = Field(ge=0)
    input_revision: int = Field(ge=0)
    attempt: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    revision: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    available_at: str
    created_at: str
    updated_at: str
    finished_at: str | None
    error_code: str | None
    result: dict[str, JsonValue] | None


class JobResponse(WireModel):
    data: JobSnapshot
    meta: MediaMeta


class IdentityPart(WireModel):
    part_id: int = Field(gt=0)
    pn_std: str
    status: str
    merged_into_id: int | None
    identity_revision: int | None = Field(default=None, ge=0)
    profile_revision: int | None = Field(default=None, ge=0)


class IdentitySnapshot(WireModel):
    parts: list[IdentityPart]
    outcome: dict[str, JsonValue]
    recalculation_job: JobSnapshot


class IdentityResponse(WireModel):
    data: IdentitySnapshot
    meta: ResponseMeta


class IdentityBaseline(WireModel):
    identity_revision: int = Field(ge=0)
    profile_revision: int = Field(ge=0)


class IdentityPreviewSnapshot(WireModel):
    operation: Literal["merge", "unmerge", "alias_reassign"]
    target_part_id: int = Field(gt=0)
    expected: dict[str, IdentityBaseline]
    parts: list[IdentityPart]


class IdentityPreviewResponse(WireModel):
    data: IdentityPreviewSnapshot
    meta: PageMeta
