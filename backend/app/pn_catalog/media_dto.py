"""Bounded commands. Storage keys and verified file facts are never client commands."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BeforeValidator, Field, model_validator

from .dto import StrictDTO


def _body_uuid(value):
    # FastAPI supplies decoded Python JSON; retain strict scalar validation while
    # accepting canonical UUID strings on the transport boundary.
    if isinstance(value, str):
        return UUID(value)
    return value


MediaUUID = Annotated[UUID, BeforeValidator(_body_uuid)]
Purpose = Literal["common", "defect"]


class CreateUpload(StrictDTO):
    client_request_id: MediaUUID
    filename: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024)
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    part_id: int | None = Field(default=None, gt=0)
    expected_identity_revision: int | None = Field(default=None, ge=0)
    purpose: Purpose = "common"

    @model_validator(mode="after")
    def target_fence(self):
        if (self.part_id is None) != (self.expected_identity_revision is None):
            raise ValueError(
                "target and identity revision must both be present or absent"
            )
        return self


class ExpectedRevision(StrictDTO):
    expected_revision: int = Field(ge=0)


class AddEntry(StrictDTO):
    expected_identity_revision: int = Field(ge=0)
    upload_id: MediaUUID | None = None
    asset_id: MediaUUID | None = None
    source_entry_id: MediaUUID | None = None
    purpose: Purpose = "common"

    @model_validator(mode="after")
    def source_reference(self):
        if (self.upload_id is None) == (self.asset_id is None):
            raise ValueError("exactly one upload_id or asset_id is required")
        if self.asset_id is not None and self.source_entry_id is None:
            raise ValueError("asset reuse requires a readable source entry")
        if self.upload_id is not None and self.source_entry_id is not None:
            raise ValueError("source entry only applies to asset reuse")
        return self


class PatchEntry(ExpectedRevision):
    purpose: Purpose | None = None
    status: Literal["gallery", "candidate"] | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if self.purpose is None and self.status is None:
            raise ValueError("at least one change required")
        return self


class RestoreEntry(StrictDTO):
    expected_deleted_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class ArrangeGallery(StrictDTO):
    expected_gallery_revision: int = Field(ge=0)
    primary_entry_id: MediaUUID | None
    ordered_entry_ids: list[MediaUUID] = Field(max_length=2000)


class SuppressHash(StrictDTO):
    expected_gallery_revision: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=2000)


class CreateDerivative(StrictDTO):
    entry_id: MediaUUID
    expected_revision: int = Field(ge=1)
    recipe_id: Literal["thumbnail-600-webp85-v1"] = "thumbnail-600-webp85-v1"


class CreateInboxGroup(StrictDTO):
    client_group_id: MediaUUID
    capture_mode: Literal["single", "same_item", "batch"]


class AddInboxGroupItem(StrictDTO):
    upload_id: MediaUUID
    position: int = Field(ge=0, le=1999)
    expected_group_revision: int = Field(ge=1)


class ArrangeInboxGroup(ExpectedRevision):
    ordered_item_ids: list[MediaUUID] = Field(max_length=2000)


class ClaimInbox(ExpectedRevision):
    takeover_reason: str | None = Field(default=None, min_length=1, max_length=2000)


class ReleaseInbox(StrictDTO):
    expected_claim_revision: int = Field(ge=1)
    reason: str | None = Field(default=None, min_length=1, max_length=2000)


class AssignInbox(StrictDTO):
    claim_revision: int = Field(ge=1)
    part_id: int = Field(gt=0)
    expected_identity_revision: int = Field(ge=0)
    assignment_key: MediaUUID
    purpose: Purpose = "common"


class MultipartPart(StrictDTO):
    part_number: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024)


class RecordMultipartPart(ExpectedRevision):
    part: MultipartPart


class CompleteMultipart(ExpectedRevision):
    parts: list[MultipartPart] = Field(min_length=1, max_length=20)


class MultipartPartURLs(ExpectedRevision):
    part_numbers: list[int] = Field(min_length=1, max_length=20)
