"""Explicit offline migration state: source evidence is distinct from live authority."""

from datetime import UTC, datetime
from typing import Literal, get_args
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .dto import StrictDTO
from .migration_dto import RecordKind

ApplicationState = Literal[
    "applied",
    "historical_terminal",
    "recovery_required",
    "dependency_blocked",
    "conflict",
]


class KindInventory(StrictDTO):
    kind: RecordKind
    count: int = Field(ge=0)
    records_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SnapshotRegistration(StrictDTO):
    namespace: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$"
    )
    snapshot_id: UUID
    source_schema: str = Field(min_length=1, max_length=128)
    inventory: list[KindInventory]
    # This implementation accepts isolated copies only. A later production gate
    # must independently verify actual deployed writers; it cannot reuse this flag.
    verification_scope: Literal["isolated_copy"] = "isolated_copy"
    verified_at: datetime
    verifier_ref: str = Field(min_length=1, max_length=256)
    writer_fence_ref: str = Field(min_length=1, max_length=256)
    lease_policy: Literal["drained", "revoked"]
    lease_evidence_ref: str = Field(min_length=1, max_length=256)

    @field_validator("verified_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("verification requires a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def complete_inventory(self):
        kinds = [item.kind for item in self.inventory]
        if len(kinds) != len(set(kinds)) or set(kinds) != set(get_args(RecordKind)):
            raise ValueError(
                "snapshot inventory must enumerate every source kind exactly once"
            )
        return self


class ApplySourceRecords(StrictDTO):
    expected_snapshot_revision: int = Field(ge=1)
    record_ids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("record_ids")
    @classmethod
    def unique_records(cls, value):
        if any(type(item) is not int or item <= 0 for item in value) or len(
            value
        ) != len(set(value)):
            raise ValueError("source records require unique positive IDs")
        return value
