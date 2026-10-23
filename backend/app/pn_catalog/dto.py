"""Bounded public command schemas, independent of HTTP and application settings."""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

PROFILE_FIELDS = frozenset({"description", "brand", "manufacturer", "category_major",
                            "category_minor", "machine_or_part", "unit"})
CATEGORY_FIELDS = frozenset({"category_major", "category_minor"})
FIELD_LIMITS = {"description": 20000, "brand": 128, "manufacturer": 256,
                "category_major": 64, "category_minor": 128,
                "machine_or_part": 16, "unit": 16}


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Provenance(StrictDTO):
    kind: Literal["manual", "excel", "ai", "upstream", "migration", "unknown"] = "manual"
    evidence_id: str | None = Field(default=None, max_length=256)
    file_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    sheet: str | None = Field(default=None, max_length=128)
    row: int | None = Field(default=None, ge=1)
    column: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=256)
    template_version: str | None = Field(default=None, max_length=128)
    policy_version: str | None = Field(default=None, max_length=128)


class ActorContext(StrictDTO):
    principal_id: str = Field(min_length=1, max_length=256)
    actor_kind: Literal["human", "system", "unknown"] = "human"
    client_id: str = Field(min_length=1, max_length=128)
    executor_id: str = Field(min_length=1, max_length=256)
    entrypoint: Literal["itsp_ui", "shop_ui", "external_api", "import_job", "maintenance_job", "migration"]
    display_name: str | None = Field(default=None, max_length=256)
    issuer: str | None = Field(default=None, max_length=256)
    subject: str | None = Field(default=None, max_length=256)
    authorization_epoch: int | None = Field(default=None, ge=0)
    mapping_version: int | None = Field(default=None, ge=1)
    client_policy_version: str | None = Field(default=None, max_length=64)
    provenance: Provenance = Field(default_factory=Provenance)


class CommandContext(StrictDTO):
    actor: ActorContext
    request_id: UUID
    correlation_id: str | None = Field(default=None, max_length=256)
    causation_id: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=2000)
    if_match_revision: int | None = Field(default=None, ge=0)
    confirmed_by: str | None = Field(default=None, max_length=256)
    adopted_from_candidate_id: str | None = Field(default=None, max_length=256)


@dataclass(frozen=True)
class AuthorizationDecision:
    """Trusted adapter result; empty field sets deny, '*' is an explicit full grant."""
    readable_fields: frozenset[str] = frozenset()
    writable_fields: frozenset[str] = frozenset()
    metadata: dict = field(default_factory=dict)


Authorize = Callable[[Session, CommandContext, str, int | None], AuthorizationDecision]


def _clean_changes(value: dict) -> dict:
    unknown = set(value) - PROFILE_FIELDS
    if unknown:
        raise ValueError(f"unknown profile fields: {', '.join(sorted(unknown))}")
    clean = {}
    for key, item in value.items():
        if item is not None and not isinstance(item, str):
            raise ValueError(f"{key} must be text or null")
        normalized = item.strip() or None if isinstance(item, str) else item
        if normalized is not None and len(normalized) > FIELD_LIMITS[key]:
            raise ValueError(f"{key} exceeds {FIELD_LIMITS[key]} characters")
        clean[key] = normalized
    return clean


class ManualSpec(StrictDTO):
    value: str = Field(min_length=1, max_length=128)
    unit: str | None
    numeric_value: str | None = Field(max_length=64)


class CreatePart(StrictDTO):
    pn_std: str = Field(min_length=1, max_length=128)
    changes: dict[str, str | None] = Field(default_factory=dict, max_length=8)
    spec_updates: dict[str, ManualSpec | None] = Field(default_factory=dict, max_length=20)
    confirmation_token: str | None = Field(default=None, max_length=4096)
    confirmation_reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("pn_std")
    @classmethod
    def canonical_pn(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("PN must not be empty")
        return value

    @field_validator("changes")
    @classmethod
    def clean_changes(cls, value: dict) -> dict:
        return _clean_changes(value)


class PatchProfile(StrictDTO):
    expected_identity_revision: int = Field(ge=0)
    expected_profile_revision: int | None = Field(default=None, ge=0)
    field_versions: dict[str, int] | None = Field(default=None, max_length=32)
    changes: dict[str, str | None] = Field(default_factory=dict, max_length=8)
    spec_updates: dict[str, ManualSpec | None] = Field(default_factory=dict, max_length=20)

    @field_validator("changes")
    @classmethod
    def clean_changes(cls, value: dict) -> dict:
        return _clean_changes(value)

    @field_validator("field_versions")
    @classmethod
    def valid_versions(cls, value: dict | None) -> dict | None:
        if value is not None and any(type(v) is not int or v < 0 for v in value.values()):
            raise ValueError("field versions must be nonnegative integers")
        return value

    @model_validator(mode="after")
    def nonempty(self):
        if not self.changes and not self.spec_updates:
            raise ValueError("at least one change is required")
        return self


class SetAvailability(StrictDTO):
    expected_identity_revision: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    availability: Literal["active", "inactive"]
    reason: str = Field(min_length=1, max_length=2000)
