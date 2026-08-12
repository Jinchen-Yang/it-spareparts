"""Immutable server-derived provider egress authorization."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EgressPurpose = Literal["query.registry", "query.result"]
Sensitivity = Literal["business_confidential", "business_restricted"]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FIELD_REF = re.compile(r"^[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}$")


class ProviderEgressSnapshot(BaseModel):
    """Value-free provider policy resolved by trusted server code only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_ref: str = Field(min_length=1, max_length=128, repr=False)
    policy_version: int = Field(ge=0, le=2**63 - 1, strict=True)
    policy_fingerprint: str = Field(repr=False)
    authz_fingerprint: str = Field(repr=False)
    allowed_purposes: frozenset[EgressPurpose] = Field(repr=False)
    allowed_field_refs: frozenset[str] = Field(max_length=512, repr=False)
    allowed_sensitivities: frozenset[Sensitivity] = Field(repr=False)

    @field_validator("profile_ref")
    @classmethod
    def _profile_ref_is_plain_text(cls, value: str) -> str:
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("control, format, and surrogate characters are forbidden")
        return value

    @field_validator("policy_fingerprint", "authz_fingerprint")
    @classmethod
    def _fingerprint_is_lower_hex64(cls, value: str) -> str:
        if not _HEX64.fullmatch(value):
            raise ValueError("fingerprint must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("allowed_field_refs")
    @classmethod
    def _field_refs_are_canonical(cls, values: frozenset[str]) -> frozenset[str]:
        if any(not _FIELD_REF.fullmatch(value) for value in values):
            raise ValueError("field refs must be canonical dataset.field identifiers")
        return values

    def fingerprint(self) -> str:
        payload = {
            "profile_ref": self.profile_ref,
            "policy_version": self.policy_version,
            "policy_fingerprint": self.policy_fingerprint,
            "authz_fingerprint": self.authz_fingerprint,
            "allowed_purposes": sorted(self.allowed_purposes),
            "allowed_field_refs": sorted(self.allowed_field_refs),
            "allowed_sensitivities": sorted(self.allowed_sensitivities),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    def evidence_binding(self) -> dict[str, object]:
        """Canonical value-free projection for sealed server-side evidence."""

        return {
            "profile_ref": self.profile_ref,
            "policy_version": self.policy_version,
            "policy_fingerprint": self.policy_fingerprint,
            "authz_fingerprint": self.authz_fingerprint,
            "allowed_purposes": sorted(self.allowed_purposes),
            "allowed_field_refs": sorted(self.allowed_field_refs),
            "allowed_sensitivities": sorted(self.allowed_sensitivities),
            "snapshot_fingerprint": self.fingerprint(),
        }
