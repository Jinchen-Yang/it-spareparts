"""Typed effective content, source candidates and explicit migration commands."""
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .dto import ManualSpec, Provenance, StrictDTO, _clean_changes

Grade = Literal["good", "bad"]
ContentStatus = Literal["pending", "ai_filled", "confirmed", "needs_review"]
CandidateStatus = Literal["pending", "partially_adopted", "adopted", "rejected"]


def clean_content_changes(value: dict) -> dict:
    if not value or set(value) - {"title", "description"}:
        raise ValueError("content changes must contain title and/or description")
    result = {}
    for key, text in value.items():
        if text is not None and not isinstance(text, str):
            raise ValueError("content must be text or null")
        text = text.strip() or None if isinstance(text, str) else text
        if text is not None and len(text) > (200 if key == "title" else 20000):
            raise ValueError("content exceeds its field limit")
        result[key] = text
    return result


class PatchContent(StrictDTO):
    expected_identity_revision: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    changes: dict[str, str | None] = Field(min_length=1, max_length=2)

    @field_validator("changes")
    @classmethod
    def clean(cls, value):
        return clean_content_changes(value)


class CandidateProvenance(Provenance):
    kind: Literal["ai", "excel"]

    @model_validator(mode="after")
    def evidence_required(self):
        if self.kind == "ai" and not all((self.provider, self.model, self.run_id, self.template_version)):
            raise ValueError("AI candidates require provider, model, run_id and template_version")
        if self.kind == "excel" and not all((self.file_sha256, self.sheet, self.row)):
            raise ValueError("Excel candidates require file_sha256, sheet and row")
        return self


class CandidateBase(StrictDTO):
    part_id: int = Field(gt=0)
    expected_identity_revision: int = Field(ge=0)
    base_revision: int = Field(ge=0)
    provenance: CandidateProvenance


class CreateProfileCandidate(CandidateBase):
    resource: Literal["profile"]
    changes: dict[str, str | None] = Field(default_factory=dict, max_length=8)
    spec_updates: dict[str, ManualSpec | None] = Field(default_factory=dict, max_length=20)

    @field_validator("changes")
    @classmethod
    def clean(cls, value):
        return _clean_changes(value)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.changes and not self.spec_updates:
            raise ValueError("candidate must propose a change")
        return self


class CreateContentCandidate(CandidateBase):
    resource: Literal["content"]
    grade: Grade
    changes: dict[str, str | None] = Field(min_length=1, max_length=2)

    @field_validator("changes")
    @classmethod
    def clean(cls, value):
        return clean_content_changes(value)


CreateCandidate = Annotated[CreateProfileCandidate | CreateContentCandidate, Field(discriminator="resource")]


class AdoptCandidate(StrictDTO):
    expected_candidate_revision: int = Field(ge=1)
    expected_identity_revision: int = Field(ge=0)
    expected_target_revision: int = Field(ge=0)
    fields: list[str] = Field(min_length=1, max_length=32)
    field_versions: dict[str, int] | None = Field(default=None, max_length=32)
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value):
        if len(set(value)) != len(value) or any(len(key) > 128 for key in value):
            raise ValueError("selected fields must be unique and bounded")
        return value

    @field_validator("field_versions")
    @classmethod
    def valid_versions(cls, value):
        if value is not None and any(type(version) is not int or version < 0 for version in value.values()):
            raise ValueError("field versions must be nonnegative integers")
        return value


class RejectCandidate(StrictDTO):
    expected_candidate_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value):
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class LegacyEffectiveContent(StrictDTO):
    """Internal manifest import only; never a public arbitrary-state write DTO."""
    grade: Grade
    expected_identity_revision: int = Field(ge=0)
    expected_revision: int = Field(default=0, ge=0, le=0)
    source_namespace: str = Field(min_length=1, max_length=128)
    source_record_id: str = Field(min_length=1, max_length=256)
    source_revision: str = Field(min_length=1, max_length=128)
    title: str | None = Field(max_length=200)
    description: str | None = Field(max_length=20000)
    status: ContentStatus
    provenance: Provenance
    historical_actor_id: str | None = Field(default=None, max_length=256)
    historical_actor_display: str | None = Field(default=None, max_length=256)
    protected_fields: list[Literal["title", "description"]] = Field(default_factory=list, max_length=2)
