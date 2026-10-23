"""Frozen, bounded migration manifests. A manifest is evidence, never authorization."""
import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from .dto import StrictDTO

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 512 * 1024
RecordKind = Literal["pn", "content", "image", "upload", "multipart", "upload_intent",
                     "inbox", "inbox_job", "agent_job", "agent_run", "bundle", "audit", "identity_event",
                     "manual_research", "image_recovery", "catalog_link", "catalog_conflict"]


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class PartMapping(StrictDTO):
    source_pn_id: UUID
    target_part_id: int | None = Field(default=None, gt=0)
    expected_identity_revision: int | None = Field(default=None, ge=0)
    disposition: Literal["mapped", "unresolved", "conflict"]
    evidence: str = Field(min_length=1, max_length=2000)
    # An identifier into the reviewed mapping artifact, not an asserted actor.
    decision_ref: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def valid_target(self):
        if self.disposition == "mapped":
            if self.target_part_id is None or self.expected_identity_revision is None or not self.decision_ref:
                raise ValueError("mapped PN requires target, identity baseline and reviewed decision reference")
        elif self.target_part_id is not None or self.expected_identity_revision is not None:
            raise ValueError("unresolved PN must not receive a guessed target")
        return self


class LegacyRecord(StrictDTO):
    kind: RecordKind
    source_id: str = Field(min_length=1, max_length=256)
    source_pn_id: UUID | None = None
    source_revision: str | None = Field(default=None, max_length=256)
    payload: dict[str, JsonValue]
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def verify_payload(self):
        encoded = canonical_bytes(self.payload)
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError("legacy record exceeds byte budget")
        if fingerprint(self.payload) != self.payload_sha256:
            raise ValueError("legacy record fingerprint mismatch")
        return self


class MigrationManifest(StrictDTO):
    schema_version: Literal[1] = 1
    namespace: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$")
    snapshot_id: UUID
    batch_id: UUID
    source_schema: str = Field(min_length=1, max_length=128)
    exported_at: datetime
    mappings: list[PartMapping] = Field(default_factory=list, max_length=1000)
    records: list[LegacyRecord] = Field(min_length=1, max_length=1000)

    @field_validator("exported_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("export timestamp requires a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent_records(self):
        mappings = {item.source_pn_id for item in self.mappings}
        if len(mappings) != len(self.mappings):
            raise ValueError("duplicate PN mappings")
        keys = {(item.kind, item.source_id) for item in self.records}
        if len(keys) != len(self.records):
            raise ValueError("duplicate legacy records")
        if any(item.source_pn_id is not None and item.source_pn_id not in mappings for item in self.records):
            raise ValueError("every PN origin needs an explicit mapping or unresolved disposition")
        if len(canonical_bytes(self.model_dump(mode="json"))) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds byte budget")
        return self

    @property
    def sha256(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class VerifiedSourceSnapshot(StrictDTO):
    """Issued by an offline copy verifier after a second read of the frozen source.

    This is internal input to the migration command, not accepted by the public API.
    Its hashes must exactly match the manifest, including unresolved records.
    """
    namespace: str
    snapshot_id: UUID
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_hashes: dict[str, str]
    writer_fence_ref: str = Field(min_length=1, max_length=256)


def source_key(record: LegacyRecord) -> str:
    return f"{record.kind}:{record.source_id}"
