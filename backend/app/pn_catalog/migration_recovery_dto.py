"""Explicit review decisions; original source evidence and old leases never change."""

from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, field_validator
from .dto import StrictDTO
from .enrichment_dto import CreateBundle, RecognizeInbox, ResearchPN


class RecoveryBase(StrictDTO):
    expected_revision: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=2000)
    external_outcome: Literal["no_external_effect", "retained_effect_reviewed"]

    @field_validator("evidence_ref", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("review evidence and reason cannot be blank")
        return value.strip()


class RetireLegacyWork(RecoveryBase):
    decision: Literal["retire_fenced"]


class ConfirmLegacyAssignment(RecoveryBase):
    decision: Literal["confirm_assignment"]
    expected_identity_revision: int = Field(ge=0)
    expected_inbox_revision: int = Field(ge=1)
    expected_entry_revision: int = Field(ge=1)
    assignment_key: UUID

    @field_validator("assignment_key", mode="before")
    @classmethod
    def uuid_wire(cls, value):
        return UUID(value) if type(value) is str else value


class RestartResearch(StrictDTO):
    kind: Literal["research"]
    command: ResearchPN


class RestartRecognition(StrictDTO):
    kind: Literal["recognize"]
    command: RecognizeInbox


class RestartBundle(StrictDTO):
    kind: Literal["bundle"]
    command: CreateBundle


class RestartLegacyWork(RecoveryBase):
    decision: Literal["restart_current"]
    replacement: Annotated[
        RestartResearch | RestartRecognition | RestartBundle,
        Field(discriminator="kind"),
    ]


RecoveryCommand = Annotated[
    RetireLegacyWork | ConfirmLegacyAssignment | RestartLegacyWork,
    Field(discriminator="decision"),
]
