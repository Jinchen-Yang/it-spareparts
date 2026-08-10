import calendar
import re
from datetime import date

from .models import (
    BatchStatus,
    CommercialCoverage,
    CommercialSide,
    CommercialSideEvidence,
    CommercialWindow,
    CompletenessStatus,
    EvidenceRef,
    PolicyCaveat,
    ReplenishmentDecision,
    ReplenishmentEvidence,
    ReplenishmentOutcome,
    ReplenishmentReviewInput,
    RuleCode,
    SealedCommercialCoverage,
    SealedCommercialSideEvidence,
    SupportClass,
    TechnicalFailureCode,
    VerifiedBatchRef,
)


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ReplenishmentTechnicalError(RuntimeError):
    """Stable fail-closed error that must enter the Task retry/fail path."""

    def __init__(self, code: TechnicalFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


def _subtract_calendar_months(value: date, months: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def commercial_window(as_of: date) -> CommercialWindow:
    """Return the closed six-calendar-month interval anchored at ``as_of``."""

    return CommercialWindow(
        start=_subtract_calendar_months(as_of, 6),
        end=as_of,
    )


def _coverage_is_complete(coverage: CommercialCoverage, as_of: date) -> bool:
    return (
        coverage.completeness_status is CompletenessStatus.COMPLETE
        and coverage.coverage_through >= as_of
        and coverage.lineage_verified
        and bool(coverage.source_batch_refs)
        and coverage.last_successful_import_at is not None
    )


def _validate_source_integrity(review: ReplenishmentReviewInput) -> None:
    seen_batches: dict[str, tuple[CommercialSide, str]] = {}
    expected_window = commercial_window(review.server.as_of)
    for expected_side, evidence in (
        (CommercialSide.PURCHASE, review.commercial.purchase),
        (CommercialSide.SALES, review.commercial.sales),
    ):
        if evidence.query_window != expected_window:
            raise ReplenishmentTechnicalError(
                TechnicalFailureCode.QUERY_WINDOW_MISMATCH
            )
        coverage = evidence.coverage
        if not coverage.lineage_verified:
            raise ReplenishmentTechnicalError(TechnicalFailureCode.LINEAGE_UNVERIFIED)
        for batch in coverage.source_batch_refs:
            if batch.batch_status is not BatchStatus.SUCCESS:
                raise ReplenishmentTechnicalError(
                    TechnicalFailureCode.BATCH_NOT_SUCCESSFUL
                )
            if batch.file_type is not expected_side:
                raise ReplenishmentTechnicalError(
                    TechnicalFailureCode.BATCH_FILE_TYPE_MISMATCH
                )
            if batch.archived_file_sha256 is None:
                raise ReplenishmentTechnicalError(
                    TechnicalFailureCode.ARCHIVE_FILE_MISSING
                )
            hashes = (
                batch.import_batch_sha256,
                batch.raw_file_sha256,
                batch.archived_file_sha256,
            )
            if any(_SHA256_PATTERN.fullmatch(value) is None for value in hashes):
                raise ReplenishmentTechnicalError(
                    TechnicalFailureCode.FILE_HASH_INVALID
                )
            if len(set(hashes)) != 1:
                raise ReplenishmentTechnicalError(
                    TechnicalFailureCode.FILE_HASH_MISMATCH
                )
            identity = (batch.file_type, batch.import_batch_sha256)
            previous_identity = seen_batches.setdefault(batch.batch_id, identity)
            if previous_identity != identity:
                raise ReplenishmentTechnicalError(
                    TechnicalFailureCode.BATCH_CONTENT_DRIFT
                )


def _seal_side(
    evidence: CommercialSideEvidence,
) -> SealedCommercialSideEvidence:
    verified_batches: list[VerifiedBatchRef] = []
    seen: set[tuple[str, str, CommercialSide]] = set()
    for batch in evidence.coverage.source_batch_refs:
        identity = (batch.batch_id, batch.import_batch_sha256, batch.file_type)
        if identity in seen:
            continue
        seen.add(identity)
        verified_batches.append(
            VerifiedBatchRef(
                batch_id=batch.batch_id,
                file_sha256=batch.import_batch_sha256,
                file_type=batch.file_type,
            )
        )
    return SealedCommercialSideEvidence(
        order_count=evidence.order_count,
        coverage=SealedCommercialCoverage(
            coverage_through=evidence.coverage.coverage_through,
            completeness_status=evidence.coverage.completeness_status,
            lineage_verified=evidence.coverage.lineage_verified,
            source_batch_refs=tuple(verified_batches),
        ),
    )


def _seal_supporting_refs(review: ReplenishmentReviewInput) -> tuple[EvidenceRef, ...]:
    sealed: list[EvidenceRef] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in (
        *review.supporting.active_pool_refs,
        *review.supporting.maintenance_refs,
        *review.supporting.business_context_refs,
    ):
        identity = (ref.ref_type.value, ref.ref_id, ref.version)
        if identity in seen:
            continue
        seen.add(identity)
        sealed.append(ref)
    return tuple(sealed)


def _hard_gate_caveats(review: ReplenishmentReviewInput) -> tuple[PolicyCaveat, ...]:
    caveats: list[PolicyCaveat] = []
    pool_ids = {ref.ref_id for ref in review.supporting.active_pool_refs}
    if pool_ids:
        caveats.append(PolicyCaveat.ACTIVE_POOL_NON_OVERRIDING)
    if len(pool_ids) > 1:
        caveats.append(PolicyCaveat.MULTIPLE_ACTIVE_POOLS)
    if review.supporting.maintenance_refs:
        caveats.append(PolicyCaveat.MAINTENANCE_NON_OVERRIDING)
    if review.supporting.business_context_refs:
        caveats.append(PolicyCaveat.BUSINESS_CONTEXT_NON_OVERRIDING)
    return tuple(caveats)


def evaluate_replenishment(
    review: ReplenishmentReviewInput,
) -> ReplenishmentDecision:
    """Evaluate the deterministic dark policy without I/O or business writes."""

    _validate_source_integrity(review)
    evidence = ReplenishmentEvidence(
        purchase=_seal_side(review.commercial.purchase),
        sales=_seal_side(review.commercial.sales),
        supporting_refs=_seal_supporting_refs(review),
    )
    coverage_complete = _coverage_is_complete(
        review.commercial.purchase.coverage, review.server.as_of
    ) and _coverage_is_complete(review.commercial.sales.coverage, review.server.as_of)
    if not coverage_complete:
        return ReplenishmentDecision(
            outcome=ReplenishmentOutcome.NEED_INFO,
            rule_code=RuleCode.SOURCE_COVERAGE_INCOMPLETE,
            overrideable=None,
            support_class=SupportClass.UNSCORED,
            window=commercial_window(review.server.as_of),
            evidence=evidence,
        )

    if (
        review.commercial.purchase.order_count == 0
        and review.commercial.sales.order_count == 0
    ):
        return ReplenishmentDecision(
            outcome=ReplenishmentOutcome.RECOMMEND_REJECT,
            rule_code=RuleCode.NO_COMMERCIAL_HISTORY,
            overrideable=False,
            support_class=SupportClass.UNSCORED,
            window=commercial_window(review.server.as_of),
            evidence=evidence,
            caveats=_hard_gate_caveats(review),
        )

    if len({ref.ref_id for ref in review.supporting.active_pool_refs}) > 1:
        return ReplenishmentDecision(
            outcome=ReplenishmentOutcome.NEED_INFO,
            rule_code=RuleCode.MULTIPLE_ACTIVE_POOLS,
            overrideable=None,
            support_class=SupportClass.UNSCORED,
            window=commercial_window(review.server.as_of),
            evidence=evidence,
            caveats=(PolicyCaveat.MULTIPLE_ACTIVE_POOLS,),
        )

    return ReplenishmentDecision(
        outcome=ReplenishmentOutcome.HUMAN_REVIEW_REQUIRED,
        rule_code=RuleCode.SHADOW_UNSCORED,
        overrideable=None,
        support_class=SupportClass.UNSCORED,
        window=commercial_window(review.server.as_of),
        evidence=evidence,
    )
