from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agent.replenishment.models import (
    BatchManifest,
    BatchStatus,
    CommercialCoverage,
    CommercialEvidence,
    CommercialSide,
    CommercialSideEvidence,
    CommercialWindow,
    CompletenessStatus,
    EvidenceRef,
    EvidenceRefType,
    PolicyCaveat,
    ReplenishmentDecision,
    ReplenishmentEvidence,
    ReplenishmentOutcome,
    ReplenishmentRequest,
    ReplenishmentReviewInput,
    RuleCode,
    ServerReviewContext,
    ShadowPolicy,
    SupportClass,
    SupportingContext,
    TechnicalFailureCode,
    VerifiedBatchRef,
)
from app.agent.replenishment.policy import (
    ReplenishmentTechnicalError,
    commercial_window,
    evaluate_replenishment,
)


AS_OF = date(2026, 8, 10)
OBSERVED_AT = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
PART_ID = 101


def _batch(
    side: CommercialSide,
    hash_character: str = "a",
    suffix: str = "v1",
) -> BatchManifest:
    file_sha256 = hash_character * 64
    return BatchManifest(
        batch_id=f"{side.value}-batch-{suffix}",
        batch_status=BatchStatus.SUCCESS,
        file_type=side,
        import_batch_sha256=file_sha256,
        raw_file_sha256=file_sha256,
        archived_file_sha256=file_sha256,
    )


def _side_evidence(
    side: CommercialSide,
    *,
    count: int = 0,
    completeness: CompletenessStatus = CompletenessStatus.COMPLETE,
    coverage_through: date | None = None,
    batches: tuple[BatchManifest, ...] | None = None,
    lineage_verified: bool = True,
    query_window: CommercialWindow | None = None,
    canonical_part_id: int = PART_ID,
    as_of: date = AS_OF,
    last_successful_import_at: datetime | None = OBSERVED_AT,
) -> CommercialSideEvidence:
    return CommercialSideEvidence(
        canonical_part_id=canonical_part_id,
        order_count=count,
        query_window=query_window or commercial_window(as_of),
        coverage=CommercialCoverage(
            coverage_through=coverage_through or as_of,
            completeness_status=completeness,
            source_batch_refs=(_batch(side),) if batches is None else batches,
            lineage_verified=lineage_verified,
            last_successful_import_at=last_successful_import_at,
        ),
    )


def _review(
    *,
    purchase: CommercialSideEvidence | None = None,
    sales: CommercialSideEvidence | None = None,
    supporting: SupportingContext | None = None,
    source_application_ref: str = "application-v7",
    source_snapshot_fingerprint: str = "f" * 64,
    canonical_part_id: int = PART_ID,
    as_of: date = AS_OF,
    policy_version: str = "replenishment-v1-shadow",
    pn_display_snapshot: str | None = "PN-001",
) -> ReplenishmentReviewInput:
    return ReplenishmentReviewInput(
        request=ReplenishmentRequest(
            source_application_ref=source_application_ref,
            source_snapshot_fingerprint=source_snapshot_fingerprint,
            pn_display_snapshot=pn_display_snapshot,
            requested_qty=Decimal("5.000"),
        ),
        canonical_part_id=canonical_part_id,
        server=ServerReviewContext(as_of=as_of),
        commercial=CommercialEvidence(
            purchase=purchase
            or _side_evidence(
                CommercialSide.PURCHASE,
                canonical_part_id=canonical_part_id,
                as_of=as_of,
            ),
            sales=sales
            or _side_evidence(
                CommercialSide.SALES,
                canonical_part_id=canonical_part_id,
                as_of=as_of,
            ),
        ),
        supporting=supporting or SupportingContext(),
        policy=ShadowPolicy(policy_version=policy_version),
    )


def test_commercial_window_is_a_closed_six_calendar_month_interval() -> None:
    window = commercial_window(date(2026, 8, 31))

    assert window.start == date(2026, 2, 28)
    assert window.end == date(2026, 8, 31)
    assert window.inclusive is True


def test_request_normalizes_optional_pn_snapshot_but_never_accepts_as_of() -> None:
    request = ReplenishmentRequest(
        source_application_ref="application-v7",
        source_snapshot_fingerprint="f" * 64,
        pn_display_snapshot="  PN-001  ",
        requested_qty=Decimal("5.000"),
    )

    assert request.pn_display_snapshot == "PN-001"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReplenishmentRequest(
            source_application_ref="application-v7",
            source_snapshot_fingerprint="f" * 64,
            pn_display_snapshot="PN-001",
            requested_qty=Decimal("5.000"),
            as_of=date(2026, 8, 10),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("pn_display_snapshot", "PN-\x00-001"),
        ("pn_display_snapshot", "PN-\n001"),
        ("pn_display_snapshot", "PN-001\n"),
        ("source_application_ref", "application\u202ev7"),
        ("pn_display_snapshot", "x" * 129),
        ("source_application_ref", "x" * 129),
    ],
)
def test_request_rejects_control_characters_and_string_overflow(
    field: str, value: str
) -> None:
    payload = {
        "source_application_ref": "application-v7",
        "source_snapshot_fingerprint": "f" * 64,
        "pn_display_snapshot": "PN-001",
        "requested_qty": Decimal("5.000"),
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        ReplenishmentRequest(**payload)


@pytest.mark.parametrize(
    "quantity",
    [
        Decimal("0"),
        Decimal("-0.001"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1.0001"),
        Decimal("100000000000.000"),
    ],
)
def test_request_rejects_quantity_outside_numeric_14_3(quantity: Decimal) -> None:
    with pytest.raises(ValidationError):
        ReplenishmentRequest(
            source_application_ref="application-v7",
            source_snapshot_fingerprint="f" * 64,
            pn_display_snapshot="PN-001",
            requested_qty=quantity,
        )


@pytest.mark.parametrize("fingerprint", ["F" * 64, "f" * 63, "z" * 64])
def test_request_requires_lower_hex_source_snapshot_fingerprint(
    fingerprint: str,
) -> None:
    with pytest.raises(ValidationError):
        ReplenishmentRequest(
            source_application_ref="application-v7",
            source_snapshot_fingerprint=fingerprint,
            requested_qty=Decimal("5.000"),
        )


@pytest.mark.parametrize("file_sha256", ["A" * 64, "a" * 63, "z" * 64])
def test_verified_batch_ref_requires_lower_hex_sha256(file_sha256: str) -> None:
    with pytest.raises(ValidationError):
        VerifiedBatchRef(
            batch_id="purchase-batch-v1",
            file_sha256=file_sha256,
            file_type=CommercialSide.PURCHASE,
        )


def test_complete_coverage_proves_zero_and_locks_rpl_100_rejection() -> None:
    decision = evaluate_replenishment(_review())

    assert decision.outcome is ReplenishmentOutcome.RECOMMEND_REJECT
    assert decision.rule_code == "RPL-100-no-commercial-history"
    assert decision.overrideable is False
    assert decision.evidence.purchase.order_count == 0
    assert decision.evidence.sales.order_count == 0
    assert decision.evidence.source_application_ref == "application-v7"
    assert decision.evidence.source_snapshot_fingerprint == "f" * 64
    assert decision.evidence.canonical_part_id == PART_ID
    assert decision.evidence.requested_qty == Decimal("5.000")
    assert decision.evidence.as_of == AS_OF
    assert decision.evidence.window == commercial_window(AS_OF)
    assert decision.evidence.policy_version == "replenishment-v1-shadow"
    assert decision.evidence.rule_implementation_version == (
        "replenishment-policy-kernel/v1"
    )
    assert decision.evidence.purchase.coverage.last_successful_import_at == OBSERVED_AT


def test_final_decision_fields_are_inside_the_immutable_sealed_payload() -> None:
    decision = evaluate_replenishment(_review())

    assert decision.evidence.outcome is ReplenishmentOutcome.RECOMMEND_REJECT
    assert decision.evidence.rule_code == "RPL-100-no-commercial-history"
    assert decision.evidence.overrideable is False
    assert decision.evidence.support_class is SupportClass.UNSCORED
    assert decision.evidence.caveats == ()


def test_rpl_100_evidence_cannot_be_reassembled_as_human_review() -> None:
    decision = evaluate_replenishment(_review())

    with pytest.raises(ValidationError):
        ReplenishmentDecision(
            evidence=decision.evidence,
            outcome=ReplenishmentOutcome.HUMAN_REVIEW_REQUIRED,
            rule_code=RuleCode.SHADOW_UNSCORED,
            overrideable=None,
            support_class=SupportClass.UNSCORED,
            window=decision.window,
            caveats=(),
        )

    altered_payload = decision.evidence.model_dump(mode="python")
    altered_payload.update(
        outcome=ReplenishmentOutcome.HUMAN_REVIEW_REQUIRED,
        rule_code=RuleCode.SHADOW_UNSCORED,
        overrideable=None,
        caveats=(),
    )
    with pytest.raises(ValidationError):
        ReplenishmentEvidence.model_validate(altered_payload)


def test_review_requires_resolved_canonical_part_identity() -> None:
    review = _review()

    with pytest.raises(ValidationError, match="canonical_part_id"):
        ReplenishmentReviewInput(
            request=review.request,
            server=review.server,
            commercial=review.commercial,
            supporting=review.supporting,
            policy=review.policy,
        )

    with pytest.raises(ValidationError, match="greater than 0"):
        ReplenishmentReviewInput(
            request=review.request,
            canonical_part_id=0,
            server=review.server,
            commercial=review.commercial,
            supporting=review.supporting,
            policy=review.policy,
        )


def _evidence_ref(
    ref_type: EvidenceRefType,
    suffix: str,
    *,
    canonical_part_id: int = PART_ID,
    source_snapshot_fingerprint: str = "f" * 64,
) -> EvidenceRef:
    return EvidenceRef(
        ref_type=ref_type,
        ref_id=f"{ref_type.value}-{suffix}",
        version="v1",
        canonical_part_id=canonical_part_id,
        source_snapshot_fingerprint=source_snapshot_fingerprint,
    )


def test_rpl_100_cannot_be_overridden_by_pool_maintenance_or_context() -> None:
    supporting = SupportingContext(
        active_pool_refs=(
            _evidence_ref(EvidenceRefType.ACTIVE_POOL, "a"),
            _evidence_ref(EvidenceRefType.ACTIVE_POOL, "b"),
        ),
        maintenance_refs=(_evidence_ref(EvidenceRefType.MAINTENANCE_USAGE, "a"),),
        business_context_refs=(_evidence_ref(EvidenceRefType.BUSINESS_CONTEXT, "a"),),
    )

    decision = evaluate_replenishment(_review(supporting=supporting))

    assert decision.outcome is ReplenishmentOutcome.RECOMMEND_REJECT
    assert decision.overrideable is False
    assert set(decision.caveats) == {
        PolicyCaveat.ACTIVE_POOL_NON_OVERRIDING,
        PolicyCaveat.MULTIPLE_ACTIVE_POOLS,
        PolicyCaveat.MAINTENANCE_NON_OVERRIDING,
        PolicyCaveat.BUSINESS_CONTEXT_NON_OVERRIDING,
    }
    assert len(decision.evidence.supporting_refs) == 4


def test_multiple_active_pools_need_info_only_after_rpl_100_passes() -> None:
    supporting = SupportingContext(
        active_pool_refs=(
            _evidence_ref(EvidenceRefType.ACTIVE_POOL, "a"),
            _evidence_ref(EvidenceRefType.ACTIVE_POOL, "b"),
        )
    )
    purchase = _side_evidence(CommercialSide.PURCHASE, count=1)

    decision = evaluate_replenishment(_review(purchase=purchase, supporting=supporting))

    assert decision.outcome is ReplenishmentOutcome.NEED_INFO
    assert decision.rule_code == "RPL-210-multiple-active-pools"
    assert decision.caveats == (PolicyCaveat.MULTIPLE_ACTIVE_POOLS,)


@pytest.mark.parametrize(
    ("purchase_count", "sales_count"),
    [(1, 0), (0, 1), (2, 3)],
)
def test_shadow_policy_never_scores_or_approves(
    purchase_count: int,
    sales_count: int,
) -> None:
    decision = evaluate_replenishment(
        _review(
            purchase=_side_evidence(
                CommercialSide.PURCHASE,
                count=purchase_count,
            ),
            sales=_side_evidence(CommercialSide.SALES, count=sales_count),
        )
    )

    assert decision.outcome is ReplenishmentOutcome.HUMAN_REVIEW_REQUIRED
    assert decision.support_class == "unscored"
    assert decision.rule_code == "RPL-SHADOW-unscored"
    assert set(ReplenishmentOutcome) == {
        ReplenishmentOutcome.NEED_INFO,
        ReplenishmentOutcome.RECOMMEND_REJECT,
        ReplenishmentOutcome.HUMAN_REVIEW_REQUIRED,
    }
    assert set(ShadowPolicy.model_fields) == {"policy_version", "mode"}


def test_evidence_is_deeply_immutable_bounded_and_minimized() -> None:
    supporting = SupportingContext(
        active_pool_refs=(_evidence_ref(EvidenceRefType.ACTIVE_POOL, "a"),)
    )
    decision = evaluate_replenishment(_review(supporting=supporting))

    with pytest.raises(ValidationError, match="Instance is frozen"):
        decision.evidence.purchase.order_count = 99

    serialized = decision.evidence.model_dump(mode="json")
    keys: set[str] = set()

    def collect_keys(value: object) -> None:
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                collect_keys(item)
        elif isinstance(value, list):
            for item in value:
                collect_keys(item)

    collect_keys(serialized)
    assert not keys.intersection(
        {
            "filename",
            "file_path",
            "order_id",
            "customer",
            "vendor",
            "price",
            "sn",
        }
    )


def test_supporting_evidence_refs_reject_controls_and_overflow() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(
            ref_type=EvidenceRefType.ACTIVE_POOL,
            ref_id="pool\x00-a",
            version="v1",
            canonical_part_id=PART_ID,
            source_snapshot_fingerprint="f" * 64,
        )

    refs = tuple(
        _evidence_ref(EvidenceRefType.ACTIVE_POOL, str(index)) for index in range(9)
    )
    with pytest.raises(ValidationError):
        SupportingContext(active_pool_refs=refs)


@pytest.mark.parametrize(
    "ref",
    [
        _evidence_ref(
            EvidenceRefType.ACTIVE_POOL,
            "wrong-part",
            canonical_part_id=PART_ID + 1,
        ),
        _evidence_ref(
            EvidenceRefType.ACTIVE_POOL,
            "wrong-snapshot",
            source_snapshot_fingerprint="e" * 64,
        ),
    ],
    ids=["cross-part", "cross-snapshot"],
)
def test_supporting_ref_binding_mismatch_fails_closed(ref: EvidenceRef) -> None:
    supporting = SupportingContext(active_pool_refs=(ref,))

    with pytest.raises(ReplenishmentTechnicalError) as caught:
        evaluate_replenishment(_review(supporting=supporting))

    assert caught.value.code is TechnicalFailureCode.SUPPORTING_REF_BINDING_MISMATCH


@pytest.mark.parametrize(
    "completeness",
    [CompletenessStatus.PARTIAL, CompletenessStatus.UNKNOWN],
)
def test_incomplete_coverage_is_need_info_and_never_a_zero(
    completeness: CompletenessStatus,
) -> None:
    purchase = _side_evidence(
        CommercialSide.PURCHASE,
        completeness=completeness,
    )

    decision = evaluate_replenishment(_review(purchase=purchase))

    assert decision.outcome is ReplenishmentOutcome.NEED_INFO
    assert decision.rule_code == "RPL-090-source-coverage-incomplete"


@pytest.mark.parametrize(
    "purchase",
    [
        _side_evidence(
            CommercialSide.PURCHASE,
            coverage_through=date(2026, 8, 9),
        ),
        _side_evidence(CommercialSide.PURCHASE, batches=()),
    ],
    ids=["stale", "missing-manifest"],
)
def test_self_consistent_but_unproven_coverage_is_need_info(
    purchase: CommercialSideEvidence,
) -> None:
    decision = evaluate_replenishment(_review(purchase=purchase))

    assert decision.outcome is ReplenishmentOutcome.NEED_INFO
    assert decision.rule_code == "RPL-090-source-coverage-incomplete"


def test_missing_last_import_marker_is_need_info_and_not_a_proven_zero() -> None:
    purchase = _side_evidence(
        CommercialSide.PURCHASE,
        last_successful_import_at=None,
    )

    decision = evaluate_replenishment(_review(purchase=purchase))

    assert decision.outcome is ReplenishmentOutcome.NEED_INFO
    assert decision.rule_code == "RPL-090-source-coverage-incomplete"


def test_coverage_through_after_as_of_is_complete_not_future_contamination() -> None:
    purchase = _side_evidence(
        CommercialSide.PURCHASE,
        coverage_through=date(2026, 8, 11),
    )
    sales = _side_evidence(
        CommercialSide.SALES,
        coverage_through=date(2026, 8, 11),
    )

    decision = evaluate_replenishment(_review(purchase=purchase, sales=sales))

    assert decision.outcome is ReplenishmentOutcome.RECOMMEND_REJECT
    assert decision.window.end == AS_OF


@pytest.mark.parametrize(
    "query_window",
    [
        CommercialWindow(
            start=date(2026, 2, 10),
            end=date(2026, 8, 11),
        ),
        CommercialWindow(
            start=date(2026, 2, 9),
            end=AS_OF,
        ),
    ],
    ids=["future-window-end", "wrong-calendar-start"],
)
def test_wrong_or_future_query_window_is_a_stable_technical_failure(
    query_window: CommercialWindow,
) -> None:
    purchase = _side_evidence(
        CommercialSide.PURCHASE,
        query_window=query_window,
    )

    with pytest.raises(ReplenishmentTechnicalError) as caught:
        evaluate_replenishment(_review(purchase=purchase))

    assert caught.value.code is TechnicalFailureCode.QUERY_WINDOW_MISMATCH


def test_commercial_counts_for_another_canonical_part_fail_closed() -> None:
    purchase = _side_evidence(
        CommercialSide.PURCHASE,
        canonical_part_id=PART_ID + 1,
    )

    with pytest.raises(ReplenishmentTechnicalError) as caught:
        evaluate_replenishment(_review(purchase=purchase))

    assert caught.value.code is TechnicalFailureCode.CANONICAL_PART_MISMATCH


def test_sealed_evidence_changes_with_application_part_as_of_and_lineage() -> None:
    baseline = evaluate_replenishment(_review()).evidence.model_dump_json()
    variants = (
        _review(
            source_application_ref="application-v8",
        ),
        _review(source_snapshot_fingerprint="e" * 64),
        _review(canonical_part_id=PART_ID + 1),
        _review(as_of=date(2026, 8, 11)),
        _review(
            purchase=_side_evidence(
                CommercialSide.PURCHASE,
                last_successful_import_at=OBSERVED_AT + timedelta(hours=1),
            )
        ),
    )

    sealed_variants = {
        evaluate_replenishment(review).evidence.model_dump_json() for review in variants
    }

    assert baseline not in sealed_variants
    assert len(sealed_variants) == len(variants)


def test_policy_version_is_sealed_but_unregistered_labels_are_rejected() -> None:
    decision = evaluate_replenishment(_review())

    assert decision.evidence.policy_version == "replenishment-v1-shadow"
    with pytest.raises(ValidationError):
        ShadowPolicy(policy_version="replenishment-v1-shadow-replay-test")


def test_pn_display_snapshot_is_not_a_product_identity_key() -> None:
    baseline = evaluate_replenishment(_review()).evidence.model_dump_json()
    redirected_alias = evaluate_replenishment(
        _review(pn_display_snapshot="OLD-PN-ALIAS")
    ).evidence.model_dump_json()

    assert redirected_alias == baseline


def _purchase_with_batch(batch: BatchManifest) -> CommercialSideEvidence:
    return _side_evidence(CommercialSide.PURCHASE, batches=(batch,))


@pytest.mark.parametrize(
    ("purchase", "error_code"),
    [
        (
            _side_evidence(CommercialSide.PURCHASE, lineage_verified=False),
            TechnicalFailureCode.LINEAGE_UNVERIFIED,
        ),
        (
            _purchase_with_batch(
                _batch(CommercialSide.PURCHASE).model_copy(
                    update={"batch_status": BatchStatus.FAILED}
                )
            ),
            TechnicalFailureCode.BATCH_NOT_SUCCESSFUL,
        ),
        (
            _purchase_with_batch(
                _batch(CommercialSide.PURCHASE).model_copy(
                    update={"file_type": CommercialSide.SALES}
                )
            ),
            TechnicalFailureCode.BATCH_FILE_TYPE_MISMATCH,
        ),
        (
            _purchase_with_batch(
                _batch(CommercialSide.PURCHASE).model_copy(
                    update={"archived_file_sha256": None}
                )
            ),
            TechnicalFailureCode.ARCHIVE_FILE_MISSING,
        ),
        (
            _purchase_with_batch(
                _batch(CommercialSide.PURCHASE).model_copy(
                    update={"import_batch_sha256": "z" * 64}
                )
            ),
            TechnicalFailureCode.FILE_HASH_INVALID,
        ),
        (
            _purchase_with_batch(
                _batch(CommercialSide.PURCHASE).model_copy(
                    update={"raw_file_sha256": "b" * 64}
                )
            ),
            TechnicalFailureCode.FILE_HASH_MISMATCH,
        ),
    ],
    ids=[
        "lineage-break",
        "failed-batch",
        "wrong-file-type",
        "missing-archive",
        "invalid-hash",
        "hash-mismatch",
    ],
)
def test_source_integrity_failures_raise_stable_technical_errors(
    purchase: CommercialSideEvidence,
    error_code: TechnicalFailureCode,
) -> None:
    with pytest.raises(ReplenishmentTechnicalError) as caught:
        evaluate_replenishment(_review(purchase=purchase))

    assert caught.value.code is error_code
    assert str(caught.value) == error_code.value


def test_same_batch_identity_with_changed_content_is_a_technical_failure() -> None:
    first = _batch(CommercialSide.PURCHASE, "a")
    drifted = _batch(CommercialSide.PURCHASE, "b").model_copy(
        update={"batch_id": first.batch_id}
    )
    purchase = _side_evidence(
        CommercialSide.PURCHASE,
        batches=(first, drifted),
    )

    with pytest.raises(ReplenishmentTechnicalError) as caught:
        evaluate_replenishment(_review(purchase=purchase))

    assert caught.value.code is TechnicalFailureCode.BATCH_CONTENT_DRIFT


def test_same_batch_and_hash_replay_is_idempotent() -> None:
    batch = _batch(CommercialSide.PURCHASE)
    purchase = _side_evidence(
        CommercialSide.PURCHASE,
        batches=(batch, batch),
    )

    decision = evaluate_replenishment(_review(purchase=purchase))

    assert decision.outcome is ReplenishmentOutcome.RECOMMEND_REJECT


def test_batch_and_supporting_ref_permutations_seal_identical_json() -> None:
    first_batch = _batch(CommercialSide.PURCHASE, "a", "a")
    second_batch = _batch(CommercialSide.PURCHASE, "b", "b")
    pool_a = _evidence_ref(EvidenceRefType.ACTIVE_POOL, "a")
    pool_b = _evidence_ref(EvidenceRefType.ACTIVE_POOL, "b")
    maintenance_a = _evidence_ref(EvidenceRefType.MAINTENANCE_USAGE, "a")
    maintenance_b = _evidence_ref(EvidenceRefType.MAINTENANCE_USAGE, "b")
    context = _evidence_ref(EvidenceRefType.BUSINESS_CONTEXT, "a")

    baseline = evaluate_replenishment(
        _review(
            purchase=_side_evidence(
                CommercialSide.PURCHASE,
                batches=(first_batch, second_batch),
            ),
            supporting=SupportingContext(
                active_pool_refs=(pool_a, pool_b),
                maintenance_refs=(maintenance_a, maintenance_b),
                business_context_refs=(context,),
            ),
        )
    ).evidence
    permuted = evaluate_replenishment(
        _review(
            purchase=_side_evidence(
                CommercialSide.PURCHASE,
                batches=(second_batch, first_batch, second_batch),
            ),
            supporting=SupportingContext(
                active_pool_refs=(pool_b, pool_a, pool_b),
                maintenance_refs=(maintenance_b, maintenance_a, maintenance_b),
                business_context_refs=(context, context),
            ),
        )
    ).evidence

    assert permuted.model_dump_json() == baseline.model_dump_json()


def test_sealed_payload_rejects_noncanonical_collection_order() -> None:
    first_batch = _batch(CommercialSide.PURCHASE, "a", "a")
    second_batch = _batch(CommercialSide.PURCHASE, "b", "b")
    evidence = evaluate_replenishment(
        _review(
            purchase=_side_evidence(
                CommercialSide.PURCHASE,
                batches=(first_batch, second_batch),
            ),
            supporting=SupportingContext(
                active_pool_refs=(
                    _evidence_ref(EvidenceRefType.ACTIVE_POOL, "a"),
                    _evidence_ref(EvidenceRefType.ACTIVE_POOL, "b"),
                )
            ),
        )
    ).evidence
    payload = evidence.model_dump(mode="python")
    payload["supporting_refs"] = tuple(reversed(payload["supporting_refs"]))
    purchase_coverage = payload["purchase"]["coverage"]
    purchase_coverage["source_batch_refs"] = tuple(
        reversed(purchase_coverage["source_batch_refs"])
    )

    with pytest.raises(ValidationError, match="canonical"):
        ReplenishmentEvidence.model_validate(payload)
