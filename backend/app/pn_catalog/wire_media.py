"""Typed public media snapshots; provider keys stay in internal plans, not responses."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .wire import PageMeta, WireModel
from .enrichment_dto import RecognitionResult

from .json_types import JsonValue


class MediaMeta(WireModel):
    request_id: str
    replayed: bool
    data_revision: int = Field(ge=0)
    resource_revision: int | None = Field(default=None, ge=0)
    identity_revision: int | None = Field(default=None, ge=0)
    applied_revision: int | None = Field(default=None, ge=0)
    applied_identity_revision: int | None = Field(default=None, ge=0)
    audit_id: int | None = None
    next_cursor: str | None = None


class DerivativeSummary(WireModel):
    derivative_id: str
    recipe_id: str
    status: Literal["pending", "ready", "failed"]


class EntrySnapshot(WireModel):
    entry_id: str
    asset_id: str
    origin_part_id: int | None
    origin_namespace: str
    origin_id: str
    purpose: Literal["common", "defect"]
    status: Literal["gallery", "candidate", "processing", "deleted"]
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    content_hash: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    size_bytes: int = Field(gt=0)
    content_type: str
    source: dict[str, JsonValue]
    derivatives: list[DerivativeSummary]
    deleted_at: str | None


class EntryResponse(WireModel):
    data: EntrySnapshot
    meta: MediaMeta


class GallerySnapshot(WireModel):
    group_id: str | None
    part_id: int | None
    revision: int = Field(ge=0)
    primary_entry_id: str | None
    ordered_entry_ids: list[str]
    entries: list[EntrySnapshot]


class GalleryResponse(WireModel):
    data: GallerySnapshot
    meta: MediaMeta


class MultipartPartSnapshot(WireModel):
    part_number: int = Field(ge=1)
    etag: str
    size_bytes: int = Field(gt=0)


class MultipartSnapshot(WireModel):
    upload_id: str
    revision: int = Field(ge=1)
    state: Literal["initializing", "uploading", "completing", "completed", "cancelled"]
    part_size: int = Field(gt=0)
    parts: list[MultipartPartSnapshot]
    generation: int = Field(ge=1)
    aborted_at: str | None


class UploadForm(WireModel):
    method: str
    url: str
    fields: dict[str, str]
    headers: dict[str, str]
    expires_at: datetime


class UploadSnapshot(WireModel):
    upload_id: str
    part_id: int | None
    identity_revision: int = Field(ge=0)
    purpose: Literal["common", "defect"]
    filename: str
    size_bytes: int = Field(gt=0)
    content_type: str
    status: Literal["issued", "verifying", "ready", "committed", "cancelled", "failed"]
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    asset_id: str | None
    entry_id: str | None
    job_id: str | None
    error_code: str | None
    expires_at: str
    upload_form: UploadForm | None = None
    multipart: MultipartSnapshot | None = None


class UploadResponse(WireModel):
    data: UploadSnapshot
    meta: MediaMeta


class UploadFormResponse(WireModel):
    data: UploadForm
    meta: PageMeta


class MultipartResponse(WireModel):
    data: MultipartSnapshot
    meta: MediaMeta


class PartSignature(WireModel):
    part_number: int = Field(ge=1)
    url: str
    headers: dict[str, str]
    expires_at: datetime


class PartSignaturesResponse(WireModel):
    data: list[PartSignature]
    meta: PageMeta


class SuppressionSnapshot(WireModel):
    suppression_id: str
    group_id: str
    content_hash: str
    active: bool
    revision: int = Field(ge=1)
    reason: str


class SuppressionResponse(WireModel):
    data: SuppressionSnapshot
    meta: MediaMeta


class DerivativeSnapshot(WireModel):
    derivative_id: str
    asset_id: str
    recipe_id: str
    status: Literal["pending", "ready", "failed"]
    revision: int = Field(ge=1)


class DerivativeResponse(WireModel):
    data: DerivativeSnapshot
    meta: MediaMeta


class DerivativeLink(WireModel):
    recipe_id: str
    url: str


class EntryLinks(WireModel):
    entry_id: str
    asset_id: str
    url: str
    derivatives: list[DerivativeLink]
    expires_at: str


class EntryLinksResponse(WireModel):
    data: EntryLinks
    meta: PageMeta


class InboxLinks(WireModel):
    inbox_id: str
    asset_id: str
    url: str
    derivatives: list[DerivativeLink]
    expires_at: str


class InboxLinksResponse(WireModel):
    data: InboxLinks
    meta: PageMeta


class InboxRecognition(WireModel):
    status: Literal["pending", "running", "succeeded", "failed", "cancelled", "stale"]
    result: RecognitionResult | None
    completed_at: str | None


class InboxSnapshot(WireModel):
    inbox_id: str
    upload_id: str
    asset_id: str | None
    group_id: str | None
    position: int = Field(ge=0)
    client_order: int | None = Field(default=None, ge=0, le=1999)
    status: Literal["receiving", "ready", "assigned", "failed", "deleted"]
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    claimed_by: str | None
    claim_revision: int = Field(ge=0)
    claim_expires_at: str | None
    part_id: int | None
    entry_id: str | None
    assignment_key: str | None
    filename: str
    size_bytes: int = Field(gt=0)
    content_type: str
    uploaded_by: dict[str, str]
    uploaded_by_me: bool
    claimed_by_me: bool
    created_at: str
    updated_at: str | None
    purpose: Literal["common", "defect"]
    source: dict[str, JsonValue]
    width: int | None
    height: int | None
    recognition: InboxRecognition | None


class InboxResponse(WireModel):
    data: InboxSnapshot
    meta: MediaMeta


class InboxListResponse(WireModel):
    data: list[InboxSnapshot]
    meta: PageMeta


class InboxGroupSnapshot(WireModel):
    group_id: str
    capture_mode: Literal["single", "same_item", "batch"]
    revision: int = Field(ge=1)
    ordered_item_ids: list[str]


class InboxGroupResponse(WireModel):
    data: InboxGroupSnapshot
    meta: MediaMeta


MediaReceiptResponse = (
    EntryResponse
    | GalleryResponse
    | UploadResponse
    | SuppressionResponse
    | DerivativeResponse
    | InboxResponse
    | InboxGroupResponse
)


class MultipartPartsResponse(WireModel):
    data: list[MultipartPartSnapshot] = Field(max_length=10000)
    meta: PageMeta
