"""Private ITSP migration controls and safe, owner-scoped historical activity."""

from pydantic import Field
from .json_types import JsonValue
from .wire import WireModel, PageMeta
from .wire_enrichment import BundleArchive


class MigrationMeta(WireModel):
    request_id: str
    replayed: bool


class MigrationRecordSnapshot(WireModel):
    source_record_id: int = Field(gt=0)
    namespace: str
    kind: str
    source_id: str
    source_sha256: str
    state: str
    revision: int = Field(ge=0)
    reason_code: str | None
    target_ref: dict[str, JsonValue] | None


class MigrationRecordResponse(WireModel):
    data: MigrationRecordSnapshot
    meta: MigrationMeta


class MigrationRecordsResponse(WireModel):
    data: list[MigrationRecordSnapshot]
    meta: PageMeta


class MigrationSnapshot(WireModel):
    namespace: str
    snapshot_id: str
    source_schema: str
    revision: int
    verification_scope: str
    created_at: str


class MigrationSnapshotsResponse(WireModel):
    data: list[MigrationSnapshot]
    meta: PageMeta


class MigrationProgressResponse(WireModel):
    data: dict[str, JsonValue]
    meta: MigrationMeta


class MigrationEvidence(WireModel):
    source_record_id: int
    source_sha256: str
    payload: dict[str, JsonValue]
    frozen_mapping: dict[str, JsonValue]
    fence_evidence: dict[str, JsonValue]


class MigrationEvidenceResponse(WireModel):
    data: MigrationEvidence
    meta: MigrationMeta


class LegacyActivity(WireModel):
    source_record_id: int
    namespace: str
    source_id: str
    kind: str
    legacy_status: str
    part_id: int | None
    inbox_id: str | None
    historical_actor_id: str | None
    event_at: str | None
    attempts: int | None
    parent_refs: dict[str, str]
    action: str | None
    entity: str | None
    state: str
    revision: int
    continuation: str


class LegacyHistoryResponse(WireModel):
    data: list[LegacyActivity]
    meta: PageMeta


class LegacyBundleDownload(WireModel):
    source_record_id: int
    archives: list[BundleArchive]
    expires_in: int = Field(gt=0, le=900)


class LegacyBundleDownloadResponse(WireModel):
    data: LegacyBundleDownload
    meta: MigrationMeta
