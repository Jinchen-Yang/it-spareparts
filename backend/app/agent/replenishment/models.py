import unicodedata
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


BoundedReference = Annotated[str, Field(min_length=1, max_length=128)]
Numeric14Scale3 = Annotated[
    Decimal,
    Field(gt=0, max_digits=14, decimal_places=3, allow_inf_nan=False),
]
BatchManifestRefs = Annotated[tuple["BatchManifest", ...], Field(max_length=32)]
VerifiedBatchRefs = Annotated[tuple["VerifiedBatchRef", ...], Field(max_length=32)]
EvidenceRefs = Annotated[tuple["EvidenceRef", ...], Field(max_length=8)]
SealedEvidenceRefs = Annotated[tuple["EvidenceRef", ...], Field(max_length=24)]


def _strip_and_reject_control_characters(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError("control characters are not permitted")
    return normalized


class CommercialWindow(_StrictFrozenModel):
    """Closed business-date interval used by the commercial-history gate."""

    start: date
    end: date
    inclusive: Literal[True] = True


class ReplenishmentRequest(_StrictFrozenModel):
    """Minimal trusted request projection; ``as_of`` is intentionally absent."""

    source_application_ref: BoundedReference
    pn: Annotated[str, Field(min_length=1, max_length=128)]
    requested_qty: Numeric14Scale3

    _sanitize_text = field_validator("source_application_ref", "pn", mode="before")(
        _strip_and_reject_control_characters
    )


class CommercialSide(StrEnum):
    PURCHASE = "purchase"
    SALES = "sales"


class BatchStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    PROCESSING = "processing"
    ROLLED_BACK = "rolled_back"


class CompletenessStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ReplenishmentOutcome(StrEnum):
    NEED_INFO = "need_info"
    RECOMMEND_REJECT = "recommend_reject"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class SupportClass(StrEnum):
    UNSCORED = "unscored"


class RuleCode(StrEnum):
    SOURCE_COVERAGE_INCOMPLETE = "RPL-090-source-coverage-incomplete"
    NO_COMMERCIAL_HISTORY = "RPL-100-no-commercial-history"
    MULTIPLE_ACTIVE_POOLS = "RPL-210-multiple-active-pools"
    SHADOW_UNSCORED = "RPL-SHADOW-unscored"


class TechnicalFailureCode(StrEnum):
    LINEAGE_UNVERIFIED = "RPL-TECH-001-lineage-unverified"
    BATCH_NOT_SUCCESSFUL = "RPL-TECH-002-batch-not-successful"
    BATCH_FILE_TYPE_MISMATCH = "RPL-TECH-003-batch-file-type-mismatch"
    ARCHIVE_FILE_MISSING = "RPL-TECH-004-archive-file-missing"
    FILE_HASH_INVALID = "RPL-TECH-005-file-hash-invalid"
    FILE_HASH_MISMATCH = "RPL-TECH-006-file-hash-mismatch"
    BATCH_CONTENT_DRIFT = "RPL-TECH-007-batch-content-drift"
    QUERY_WINDOW_MISMATCH = "RPL-TECH-008-query-window-mismatch"


class EvidenceRefType(StrEnum):
    ACTIVE_POOL = "active_pool"
    MAINTENANCE_USAGE = "maintenance_usage"
    BUSINESS_CONTEXT = "business_context"


class PolicyCaveat(StrEnum):
    ACTIVE_POOL_NON_OVERRIDING = "active_pool_non_overriding_support"
    MULTIPLE_ACTIVE_POOLS = "multiple_active_pools"
    MAINTENANCE_NON_OVERRIDING = "maintenance_non_overriding_support"
    BUSINESS_CONTEXT_NON_OVERRIDING = "business_context_non_overriding_support"


class BatchManifest(_StrictFrozenModel):
    """Adapter claim checked before any business outcome can be sealed."""

    batch_id: BoundedReference
    batch_status: BatchStatus
    file_type: CommercialSide
    import_batch_sha256: Annotated[str, Field(min_length=1, max_length=128)]
    raw_file_sha256: Annotated[str, Field(min_length=1, max_length=128)]
    archived_file_sha256: Annotated[str, Field(min_length=1, max_length=128)] | None

    _sanitize_text = field_validator(
        "batch_id",
        "import_batch_sha256",
        "raw_file_sha256",
        "archived_file_sha256",
        mode="before",
    )(_strip_and_reject_control_characters)


class CommercialCoverage(_StrictFrozenModel):
    coverage_through: date
    completeness_status: CompletenessStatus
    source_batch_refs: BatchManifestRefs = ()
    lineage_verified: bool
    last_successful_import_at: datetime | None = None

    @field_validator("last_successful_import_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("last_successful_import_at must be timezone-aware")
        return value


class CommercialSideEvidence(_StrictFrozenModel):
    order_count: Annotated[int, Field(ge=0)]
    query_window: CommercialWindow
    coverage: CommercialCoverage


class CommercialEvidence(_StrictFrozenModel):
    purchase: CommercialSideEvidence
    sales: CommercialSideEvidence


class ServerReviewContext(_StrictFrozenModel):
    """Trusted Task metadata; adapters derive and freeze ``as_of`` server-side."""

    as_of: date


class EvidenceRef(_StrictFrozenModel):
    """Opaque, bounded reference safe for a minimized Evidence payload."""

    ref_type: EvidenceRefType
    ref_id: BoundedReference
    version: BoundedReference

    _sanitize_text = field_validator("ref_id", "version", mode="before")(
        _strip_and_reject_control_characters
    )


class SupportingContext(_StrictFrozenModel):
    """Bounded references only; no raw business records or descriptive text."""

    active_pool_refs: EvidenceRefs = ()
    maintenance_refs: EvidenceRefs = ()
    business_context_refs: EvidenceRefs = ()

    @model_validator(mode="after")
    def _require_matching_reference_types(self) -> "SupportingContext":
        expected = (
            (self.active_pool_refs, EvidenceRefType.ACTIVE_POOL),
            (self.maintenance_refs, EvidenceRefType.MAINTENANCE_USAGE),
            (self.business_context_refs, EvidenceRefType.BUSINESS_CONTEXT),
        )
        if any(
            ref.ref_type is not ref_type for refs, ref_type in expected for ref in refs
        ):
            raise ValueError(
                "supporting evidence reference type does not match its field"
            )
        return self


class ShadowPolicy(_StrictFrozenModel):
    policy_version: Literal["replenishment-v1-shadow"] = "replenishment-v1-shadow"
    mode: Literal["shadow"] = "shadow"


class ReplenishmentReviewInput(_StrictFrozenModel):
    request: ReplenishmentRequest
    server: ServerReviewContext
    commercial: CommercialEvidence
    supporting: SupportingContext
    policy: ShadowPolicy


class VerifiedBatchRef(_StrictFrozenModel):
    batch_id: BoundedReference
    file_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    file_type: CommercialSide


class SealedCommercialCoverage(_StrictFrozenModel):
    coverage_through: date
    completeness_status: CompletenessStatus
    lineage_verified: bool
    source_batch_refs: VerifiedBatchRefs = ()


class SealedCommercialSideEvidence(_StrictFrozenModel):
    order_count: Annotated[int, Field(ge=0)]
    coverage: SealedCommercialCoverage


class ReplenishmentEvidence(_StrictFrozenModel):
    purchase: SealedCommercialSideEvidence
    sales: SealedCommercialSideEvidence
    supporting_refs: SealedEvidenceRefs = ()


class ReplenishmentDecision(_StrictFrozenModel):
    outcome: ReplenishmentOutcome
    rule_code: RuleCode
    overrideable: bool | None
    support_class: SupportClass
    window: CommercialWindow
    evidence: ReplenishmentEvidence
    caveats: Annotated[tuple[PolicyCaveat, ...], Field(max_length=8)] = ()
