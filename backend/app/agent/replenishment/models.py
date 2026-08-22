import calendar
import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


BoundedReference = Annotated[str, Field(min_length=1, max_length=128)]
Sha256Hex = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
Numeric14Scale3 = Annotated[
    Decimal,
    Field(gt=0, max_digits=14, decimal_places=3, allow_inf_nan=False),
]
CanonicalPartId = Annotated[int, Field(gt=0, le=2_147_483_647)]
OrderCount = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
BatchManifestRefs = Annotated[tuple["BatchManifest", ...], Field(max_length=32)]
VerifiedBatchRefs = Annotated[tuple["VerifiedBatchRef", ...], Field(max_length=32)]
EvidenceRefs = Annotated[tuple["EvidenceRef", ...], Field(max_length=8)]
SealedEvidenceRefs = Annotated[tuple["EvidenceRef", ...], Field(max_length=24)]

_QUANTITY_QUANTUM = Decimal("0.001")
_MAX_QUANTITY_REPRESENTATION_LENGTH = 32
_QUANTITY_CANONICAL_CONTEXT_PRECISION = 18  # Numeric(14, 3) plus safety headroom.


def _strip_and_reject_control_characters(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("control characters are not permitted")
    return value.strip()


def _require_timezone_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("last_successful_import_at must be timezone-aware")
    return value


def _prepare_quantity(value: object, info: ValidationInfo) -> object:
    if not isinstance(value, str):
        return value
    if len(value) > _MAX_QUANTITY_REPRESENTATION_LENGTH:
        raise ValueError("quantity representation exceeds the safe length")
    if info.mode == "json":
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("quantity representation is invalid") from exc
    return value


def _canonicalize_quantity(value: Decimal) -> Decimal:
    decimal_tuple = value.as_tuple()
    if (
        len(decimal_tuple.digits) > _MAX_QUANTITY_REPRESENTATION_LENGTH
        or abs(decimal_tuple.exponent) > _MAX_QUANTITY_REPRESENTATION_LENGTH
    ):
        raise ValueError("quantity representation exceeds the safe length")
    try:
        with localcontext() as decimal_context:
            decimal_context.prec = _QUANTITY_CANONICAL_CONTEXT_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            canonical = value.quantize(
                _QUANTITY_QUANTUM,
                context=decimal_context,
            )
    except InvalidOperation as exc:
        raise ValueError("quantity cannot be represented at scale three") from exc
    if canonical != value:
        raise ValueError("quantity has more than three decimal places")
    return canonical


class CommercialWindow(_StrictFrozenModel):
    """Closed business-date interval used by the commercial-history gate."""

    start: date
    end: date
    inclusive: Literal[True] = True


def canonical_commercial_window(as_of: date) -> CommercialWindow:
    """Single canonical derivation for the closed six-calendar-month window."""

    absolute_month = as_of.year * 12 + as_of.month - 1 - 6
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    day = min(as_of.day, calendar.monthrange(year, month)[1])
    return CommercialWindow(start=date(year, month, day), end=as_of)


class ReplenishmentRequest(_StrictFrozenModel):
    """Resolved evaluation request; PN is an optional display snapshot only."""

    source_application_ref: BoundedReference
    source_snapshot_fingerprint: Sha256Hex
    pn_display_snapshot: Annotated[str, Field(min_length=1, max_length=128)] | None = (
        None
    )
    requested_qty: Numeric14Scale3

    _sanitize_text = field_validator(
        "source_application_ref", "pn_display_snapshot", mode="before"
    )(_strip_and_reject_control_characters)
    _prepare_requested_quantity = field_validator("requested_qty", mode="before")(
        _prepare_quantity
    )
    _canonicalize_requested_quantity = field_validator("requested_qty")(
        _canonicalize_quantity
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
    CANONICAL_PART_MISMATCH = "RPL-TECH-009-canonical-part-mismatch"
    SUPPORTING_REF_BINDING_MISMATCH = "RPL-TECH-010-supporting-ref-binding-mismatch"
    SUPPORTING_REF_VERSION_DRIFT = "RPL-TECH-011-supporting-ref-version-drift"


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

    _validate_last_import = field_validator("last_successful_import_at")(
        _require_timezone_aware
    )


class CommercialSideEvidence(_StrictFrozenModel):
    canonical_part_id: CanonicalPartId
    order_count: OrderCount
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
    canonical_part_id: CanonicalPartId
    source_snapshot_fingerprint: Sha256Hex

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
    canonical_part_id: CanonicalPartId
    server: ServerReviewContext
    commercial: CommercialEvidence
    supporting: SupportingContext
    policy: ShadowPolicy


class VerifiedBatchRef(_StrictFrozenModel):
    batch_id: BoundedReference
    file_sha256: Sha256Hex
    file_type: CommercialSide

    _sanitize_batch_id = field_validator("batch_id", mode="before")(
        _strip_and_reject_control_characters
    )


class SealedCommercialCoverage(_StrictFrozenModel):
    coverage_through: date
    completeness_status: CompletenessStatus
    lineage_verified: bool
    last_successful_import_at: datetime | None
    source_batch_refs: VerifiedBatchRefs = ()

    _validate_last_import = field_validator("last_successful_import_at")(
        _require_timezone_aware
    )


class SealedCommercialSideEvidence(_StrictFrozenModel):
    order_count: OrderCount
    coverage: SealedCommercialCoverage


class ReplenishmentEvidence(_StrictFrozenModel):
    schema_version: Literal["replenishment-evidence/v1"] = "replenishment-evidence/v1"
    source_application_ref: BoundedReference
    source_snapshot_fingerprint: Sha256Hex
    canonical_part_id: CanonicalPartId
    requested_qty: Numeric14Scale3
    as_of: date
    window: CommercialWindow
    policy_version: Literal["replenishment-v1-shadow"]
    rule_implementation_version: Literal["replenishment-policy-kernel/v1"] = (
        "replenishment-policy-kernel/v1"
    )
    purchase: SealedCommercialSideEvidence
    sales: SealedCommercialSideEvidence
    supporting_refs: SealedEvidenceRefs = ()
    outcome: ReplenishmentOutcome
    rule_code: RuleCode
    overrideable: bool | None
    support_class: SupportClass
    caveats: Annotated[tuple[PolicyCaveat, ...], Field(max_length=8)] = ()

    _sanitize_reference = field_validator("source_application_ref", mode="before")(
        _strip_and_reject_control_characters
    )
    _prepare_requested_quantity = field_validator("requested_qty", mode="before")(
        _prepare_quantity
    )
    _canonicalize_requested_quantity = field_validator("requested_qty")(
        _canonicalize_quantity
    )

    @model_validator(mode="after")
    def _require_deterministic_sealed_decision(self) -> "ReplenishmentEvidence":
        if self.window != canonical_commercial_window(self.as_of):
            raise ValueError(
                "sealed evidence must use the canonical six-calendar-month window"
            )
        supporting_keys = [
            (
                ref.ref_type.value,
                ref.ref_id,
                ref.version,
                ref.canonical_part_id,
                ref.source_snapshot_fingerprint,
            )
            for ref in self.supporting_refs
        ]
        if supporting_keys != sorted(set(supporting_keys)):
            raise ValueError("supporting evidence refs must be canonical and unique")
        supporting_versions: dict[tuple[str, str], str] = {}
        for ref in self.supporting_refs:
            ref_identity = (ref.ref_type.value, ref.ref_id)
            previous_version = supporting_versions.setdefault(ref_identity, ref.version)
            if previous_version != ref.version:
                raise ValueError("supporting evidence ref has conflicting versions")
        seen_batches: dict[str, tuple[CommercialSide, str]] = {}
        for expected_side, coverage in (
            (CommercialSide.PURCHASE, self.purchase.coverage),
            (CommercialSide.SALES, self.sales.coverage),
        ):
            batch_keys = [
                (ref.file_type.value, ref.batch_id, ref.file_sha256)
                for ref in coverage.source_batch_refs
            ]
            if batch_keys != sorted(set(batch_keys)):
                raise ValueError("batch refs must be canonical and unique")
            for ref in coverage.source_batch_refs:
                if ref.file_type is not expected_side:
                    raise ValueError(
                        "batch ref file type does not match sealed evidence side"
                    )
                identity = (ref.file_type, ref.file_sha256)
                previous_identity = seen_batches.setdefault(ref.batch_id, identity)
                if previous_identity != identity:
                    raise ValueError(
                        "batch ref identity conflicts within sealed evidence"
                    )
        if any(
            ref.canonical_part_id != self.canonical_part_id
            or ref.source_snapshot_fingerprint != self.source_snapshot_fingerprint
            for ref in self.supporting_refs
        ):
            raise ValueError(
                "supporting evidence binding does not match sealed request"
            )
        if not (
            self.purchase.coverage.lineage_verified
            and self.sales.coverage.lineage_verified
        ):
            raise ValueError(
                "unverified lineage cannot be sealed as a business outcome"
            )

        coverage_complete = all(
            coverage.completeness_status is CompletenessStatus.COMPLETE
            and coverage.coverage_through >= self.as_of
            and bool(coverage.source_batch_refs)
            and coverage.last_successful_import_at is not None
            for coverage in (
                self.purchase.coverage,
                self.sales.coverage,
            )
        )
        pool_ids = {
            ref.ref_id
            for ref in self.supporting_refs
            if ref.ref_type is EvidenceRefType.ACTIVE_POOL
        }
        if not coverage_complete:
            expected = (
                ReplenishmentOutcome.NEED_INFO,
                RuleCode.SOURCE_COVERAGE_INCOMPLETE,
                None,
                (),
            )
        elif self.purchase.order_count == 0 and self.sales.order_count == 0:
            expected_caveats: list[PolicyCaveat] = []
            if pool_ids:
                expected_caveats.append(PolicyCaveat.ACTIVE_POOL_NON_OVERRIDING)
            if len(pool_ids) > 1:
                expected_caveats.append(PolicyCaveat.MULTIPLE_ACTIVE_POOLS)
            if any(
                ref.ref_type is EvidenceRefType.MAINTENANCE_USAGE
                for ref in self.supporting_refs
            ):
                expected_caveats.append(PolicyCaveat.MAINTENANCE_NON_OVERRIDING)
            if any(
                ref.ref_type is EvidenceRefType.BUSINESS_CONTEXT
                for ref in self.supporting_refs
            ):
                expected_caveats.append(PolicyCaveat.BUSINESS_CONTEXT_NON_OVERRIDING)
            expected = (
                ReplenishmentOutcome.RECOMMEND_REJECT,
                RuleCode.NO_COMMERCIAL_HISTORY,
                False,
                tuple(expected_caveats),
            )
        elif len(pool_ids) > 1:
            expected = (
                ReplenishmentOutcome.NEED_INFO,
                RuleCode.MULTIPLE_ACTIVE_POOLS,
                None,
                (PolicyCaveat.MULTIPLE_ACTIVE_POOLS,),
            )
        else:
            expected = (
                ReplenishmentOutcome.HUMAN_REVIEW_REQUIRED,
                RuleCode.SHADOW_UNSCORED,
                None,
                (),
            )

        actual = (self.outcome, self.rule_code, self.overrideable, self.caveats)
        if actual != expected or self.support_class is not SupportClass.UNSCORED:
            raise ValueError("sealed decision does not match deterministic evidence")
        return self


class ReplenishmentDecision(_StrictFrozenModel):
    evidence: ReplenishmentEvidence

    @property
    def outcome(self) -> ReplenishmentOutcome:
        return self.evidence.outcome

    @property
    def rule_code(self) -> RuleCode:
        return self.evidence.rule_code

    @property
    def overrideable(self) -> bool | None:
        return self.evidence.overrideable

    @property
    def support_class(self) -> SupportClass:
        return self.evidence.support_class

    @property
    def window(self) -> CommercialWindow:
        return self.evidence.window

    @property
    def caveats(self) -> tuple[PolicyCaveat, ...]:
        return self.evidence.caveats
