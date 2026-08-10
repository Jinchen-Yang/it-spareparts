"""Issue #228: deterministic, opaque-ref, dark cleaning proposal kernel."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.agent.workbook_cleaning import (
    ArtifactSnapshot,
    CellValue,
    CleaningChangeProposal,
    CleaningProposalRequest,
    ColumnKind,
    ColumnSnapshot,
    FieldChange,
    ManualReviewReason,
    ObservedFieldSnapshot,
    Operation,
    OperationImplementation,
    ProposalOrigin,
    RiskFlag,
    RuleSetSnapshot,
    SourceRowRef,
    TableSnapshot,
    TemplateClassification,
    TemplateSnapshot,
    assess_cleaning_proposal,
    verify_cleaning_assessment,
)
from app.agent.workbook_cleaning.kernel import CleaningProposalRejected


def _u(number: int) -> UUID:
    return UUID(int=number)


SOURCE_SHA = "1" * 64
TEMPLATE_SHA = "2" * 64
RULES_SHA = "3" * 64
SOURCE_ARTIFACT_REF = _u(1)
TEMPLATE_ARTIFACT_REF = _u(2)
SOURCE_SNAPSHOT_REF = _u(10)
TEMPLATE_SNAPSHOT_REF = _u(11)
RULE_SNAPSHOT_REF = _u(12)
PROPOSAL_REF = _u(13)
SOURCE_SHEET_REF = _u(14)
TARGET_SHEET_REF = _u(15)

SRC_ROW = _u(101)
SRC_DESCRIPTION = _u(102)
SRC_NAME = _u(103)
SRC_AMOUNT = _u(104)
SRC_DATE = _u(105)
SRC_PN = _u(106)
DST_DESCRIPTION = _u(201)
DST_NAME = _u(202)
DST_AMOUNT = _u(203)
DST_DATE = _u(204)
DST_PN = _u(205)

SOURCE_COLUMNS = (
    ColumnSnapshot(column_ref=SRC_ROW, kind=ColumnKind.ROW_IDENTITY),
    ColumnSnapshot(column_ref=SRC_DESCRIPTION, kind=ColumnKind.SEMANTIC_TEXT),
    ColumnSnapshot(column_ref=SRC_NAME, kind=ColumnKind.TEXT),
    ColumnSnapshot(column_ref=SRC_AMOUNT, kind=ColumnKind.AMOUNT),
    ColumnSnapshot(column_ref=SRC_DATE, kind=ColumnKind.DATE),
    ColumnSnapshot(column_ref=SRC_PN, kind=ColumnKind.PN),
)
TARGET_COLUMNS = (
    ColumnSnapshot(column_ref=DST_DESCRIPTION, kind=ColumnKind.SEMANTIC_TEXT),
    ColumnSnapshot(column_ref=DST_NAME, kind=ColumnKind.TEXT),
    ColumnSnapshot(column_ref=DST_AMOUNT, kind=ColumnKind.AMOUNT),
    ColumnSnapshot(column_ref=DST_DATE, kind=ColumnKind.DATE),
    ColumnSnapshot(column_ref=DST_PN, kind=ColumnKind.PN),
)


def _pair(
    *,
    row: int = 2,
    source_column_refs: tuple[UUID, ...] = (SRC_DESCRIPTION,),
    target_column_ref: UUID = DST_DESCRIPTION,
    operation: Operation = Operation.SEMANTIC_REWRITE,
    before: CellValue = CellValue(kind="text", value="  原始 描述  "),
    after: CellValue = CellValue(kind="text", value="标准描述"),
    confidence: int = 9_200,
    observed_field_ref: UUID | None = None,
    proposed_value_ref: UUID | None = None,
) -> tuple[FieldChange, ObservedFieldSnapshot]:
    observed_ref = observed_field_ref or _u(10_000 + row * 10 + target_column_ref.int)
    proposed_ref = proposed_value_ref or _u(20_000 + row * 10 + target_column_ref.int)
    row_ref = SourceRowRef(
        source_snapshot_ref=SOURCE_SNAPSHOT_REF,
        sheet_ref=SOURCE_SHEET_REF,
        row_number=row,
    )
    observed = ObservedFieldSnapshot(
        observed_field_ref=observed_ref,
        row_ref=row_ref,
        source_column_refs=source_column_refs,
        target_column_ref=target_column_ref,
        before=before,
    )
    change = FieldChange(
        observed_field_ref=observed_ref,
        proposed_value_ref=proposed_ref,
        row_ref=row_ref,
        source_column_refs=source_column_refs,
        target_column_ref=target_column_ref,
        operation=operation,
        operation_implementation_version="operation/1.0.0",
        proposed_after=after,
        reason_code="description_normalized",
        confidence_basis_points=confidence,
    )
    return change, observed


def _request(
    *,
    pairs: tuple[tuple[FieldChange, ObservedFieldSnapshot], ...] | None = None,
    template_classification: TemplateClassification = TemplateClassification.IDENTITY_ONLY,
    source_columns: tuple[ColumnSnapshot, ...] = SOURCE_COLUMNS,
    target_columns: tuple[ColumnSnapshot, ...] = TARGET_COLUMNS,
) -> CleaningProposalRequest:
    pairs = pairs or (_pair(),)
    source_artifact = ArtifactSnapshot(
        artifact_id=SOURCE_ARTIFACT_REF,
        sha256=SOURCE_SHA,
        owner_sub="worker-001",
    )
    template_artifact = ArtifactSnapshot(
        artifact_id=TEMPLATE_ARTIFACT_REF,
        sha256=TEMPLATE_SHA,
        owner_sub="worker-001",
    )
    source = TableSnapshot(
        source_snapshot_ref=SOURCE_SNAPSHOT_REF,
        artifact=source_artifact,
        projection_implementation_version="local-table-projection/1.0.0",
        sheet_ref=SOURCE_SHEET_REF,
        header_row=1,
        data_start_row=2,
        columns=source_columns,
    )
    template = TemplateSnapshot(
        template_snapshot_ref=TEMPLATE_SNAPSHOT_REF,
        artifact=template_artifact,
        template_version="client-template/2026-08-10.1",
        classification=template_classification,
        classifier_proof_version="template-classifier/1.0.0",
        target_sheet_ref=TARGET_SHEET_REF,
        columns=target_columns,
    )
    rules = RuleSetSnapshot(
        rule_snapshot_ref=RULE_SNAPSHOT_REF,
        rule_set_id="client-description-rules",
        rule_set_version="rules/2026-08-10.1",
        rule_set_sha256=RULES_SHA,
        policy_implementation_version="cleaning-policy/1.0.0",
        operations=tuple(
            OperationImplementation(
                operation=operation,
                implementation_version="operation/1.0.0",
            )
            for operation in Operation
        ),
        maximum_changes=200,
        semantic_rewrite_limit=100,
        low_confidence_threshold_basis_points=5_000,
        large_change_review_threshold=100,
    )
    proposal = CleaningChangeProposal(
        proposal_ref=PROPOSAL_REF,
        origin=ProposalOrigin.AI,
        source_snapshot_ref=source.source_snapshot_ref,
        source_artifact_id=source.artifact.artifact_id,
        source_sha256=source.artifact.sha256,
        source_projection_implementation_version=source.projection_implementation_version,
        template_snapshot_ref=template.template_snapshot_ref,
        template_artifact_id=template.artifact.artifact_id,
        template_sha256=template.artifact.sha256,
        template_version=template.template_version,
        template_classifier_proof_version=template.classifier_proof_version,
        rule_snapshot_ref=rules.rule_snapshot_ref,
        rule_set_id=rules.rule_set_id,
        rule_set_version=rules.rule_set_version,
        rule_set_sha256=rules.rule_set_sha256,
        policy_implementation_version=rules.policy_implementation_version,
        changes=tuple(pair[0] for pair in pairs),
    )
    return CleaningProposalRequest(
        task_owner_sub="worker-001",
        source=source,
        template=template,
        rules=rules,
        observed_fields=tuple(pair[1] for pair in pairs),
        proposal=proposal,
    )


def _expect_rejected(request: CleaningProposalRequest, code: str) -> None:
    with pytest.raises(CleaningProposalRejected) as caught:
        assess_cleaning_proposal(request)
    assert caught.value.code == code


def test_dark_evidence_is_opaque_proposal_only_and_contains_no_raw_or_hash_oracle() -> None:
    result = assess_cleaning_proposal(_request())

    assert result.mode == "dark"
    assert result.outcome == "human_review_required"
    assert result.executable is False
    assert result.artifact_create_allowed is False
    assert result.business_write_allowed is False
    assert result.proposal_ref == PROPOSAL_REF
    assert result.source_binding.source_snapshot_ref == SOURCE_SNAPSHOT_REF
    assert result.source_binding.sheet_ref == SOURCE_SHEET_REF
    assert result.template_binding.target_sheet_ref == TARGET_SHEET_REF
    assert result.rules_binding.rule_set_version == "rules/2026-08-10.1"
    assert result.field_diffs[0].observed_field_ref == _pair()[1].observed_field_ref
    assert result.field_diffs[0].proposed_value_ref == _pair()[0].proposed_value_ref
    assert result.field_diffs[0].requires_human_review is True
    assert RiskFlag.SEMANTIC_CONTENT_REWRITE in result.risk_flags
    assert ManualReviewReason.HUMAN_ACCEPT_REQUIRED in result.manual_review_reasons

    serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    for forbidden in (
        "源数据",
        "清洗结果",
        "原始 描述",
        "标准描述",
        "worker-001",
        SOURCE_SHA,
        TEMPLATE_SHA,
        RULES_SHA,
        "sha256",
        "fingerprint",
    ):
        assert forbidden not in serialized


def test_same_input_is_deterministic_and_change_order_is_not_semantic() -> None:
    first = _pair()
    second = _pair(
        row=3,
        source_column_refs=(SRC_NAME,),
        target_column_ref=DST_NAME,
        operation=Operation.TRIM,
        before=CellValue(kind="text", value="  Alice "),
        after=CellValue(kind="text", value="Alice"),
    )
    left = assess_cleaning_proposal(_request(pairs=(first, second)))
    right = assess_cleaning_proposal(_request(pairs=(second, first)))
    assert left == right
    assert left == assess_cleaning_proposal(_request(pairs=(first, second)))


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("source_snapshot_ref", _u(999), "source_binding_mismatch"),
        ("source_sha256", "4" * 64, "source_binding_mismatch"),
        (
            "source_projection_implementation_version",
            "local-table-projection/other",
            "source_binding_mismatch",
        ),
        ("source_artifact_id", _u(998), "source_binding_mismatch"),
        ("template_snapshot_ref", _u(997), "template_binding_mismatch"),
        ("template_sha256", "5" * 64, "template_binding_mismatch"),
        ("template_version", "client-template/other", "template_binding_mismatch"),
        (
            "template_classifier_proof_version",
            "template-classifier/other",
            "template_binding_mismatch",
        ),
        ("rule_snapshot_ref", _u(996), "rule_binding_mismatch"),
        ("rule_set_sha256", "6" * 64, "rule_binding_mismatch"),
        ("rule_set_version", "rules/other", "rule_binding_mismatch"),
        (
            "policy_implementation_version",
            "cleaning-policy/other",
            "rule_binding_mismatch",
        ),
    ),
)
def test_proposal_binds_authoritative_snapshots(field: str, value: object, code: str) -> None:
    request = _request()
    proposal = request.proposal.model_copy(update={field: value})
    _expect_rejected(request.model_copy(update={"proposal": proposal}), code)


def test_owner_and_template_classification_fail_closed() -> None:
    request = _request()
    foreign_template = request.template.model_copy(
        update={
            "artifact": request.template.artifact.model_copy(update={"owner_sub": "worker-002"})
        }
    )
    _expect_rejected(request.model_copy(update={"template": foreign_template}), "owner_mismatch")
    _expect_rejected(
        _request(template_classification=TemplateClassification.UNCLASSIFIED),
        "template_unclassified",
    )
    business = assess_cleaning_proposal(
        _request(template_classification=TemplateClassification.BUSINESS_CONTENT)
    )
    assert RiskFlag.TEMPLATE_BUSINESS_CONTENT in business.risk_flags


def test_duplicate_target_observed_and_proposed_refs_fail_closed() -> None:
    request = _request()
    change = request.proposal.changes[0]
    duplicate_change_request = request.model_copy(
        update={"proposal": request.proposal.model_copy(update={"changes": (change, change)})}
    )
    _expect_rejected(duplicate_change_request, "duplicate_target_field_change")

    second = _pair(row=3, observed_field_ref=request.observed_fields[0].observed_field_ref)
    _expect_rejected(
        _request(pairs=(_pair(), second)),
        "duplicate_observed_field_ref",
    )
    reused_value = _pair(row=3, proposed_value_ref=change.proposed_value_ref)
    _expect_rejected(_request(pairs=(_pair(), reused_value)), "duplicate_proposed_value_ref")


def test_row_and_observed_field_bindings_cannot_be_retargeted() -> None:
    request = _request()
    change = request.proposal.changes[0]
    wrong_snapshot = change.model_copy(
        update={
            "row_ref": change.row_ref.model_copy(update={"source_snapshot_ref": _u(700)})
        }
    )
    _expect_rejected(
        request.model_copy(
            update={"proposal": request.proposal.model_copy(update={"changes": (wrong_snapshot,)})}
        ),
        "row_reference_mismatch",
    )
    retargeted = change.model_copy(update={"source_column_refs": (SRC_NAME,)})
    _expect_rejected(
        request.model_copy(
            update={"proposal": request.proposal.model_copy(update={"changes": (retargeted,)})}
        ),
        "observed_field_mismatch",
    )


def test_unknown_source_or_target_column_fails_closed() -> None:
    unknown_source = _u(701)
    pair = _pair(source_column_refs=(unknown_source,))
    _expect_rejected(_request(pairs=(pair,)), "unknown_source_column")
    pair = _pair(target_column_ref=_u(702))
    _expect_rejected(_request(pairs=(pair,)), "unknown_target_column")


def test_semantic_and_identity_column_boundaries() -> None:
    semantic_amount = _pair(
        source_column_refs=(SRC_AMOUNT,),
        target_column_ref=DST_AMOUNT,
        before=CellValue(kind="decimal", value="100.00"),
        after=CellValue(kind="decimal", value="99.00"),
    )
    _expect_rejected(
        _request(pairs=(semantic_amount,)),
        "operation_not_allowed_for_column",
    )
    pn = _pair(
        source_column_refs=(SRC_PN,),
        target_column_ref=DST_PN,
        operation=Operation.TRIM,
        before=CellValue(kind="text", value=" PN-1 "),
        after=CellValue(kind="text", value="PN-1"),
    )
    _expect_rejected(_request(pairs=(pn,)), "protected_column")


@pytest.mark.parametrize(
    ("source_ref", "target_ref", "operation", "before", "after"),
    (
        (
            SRC_AMOUNT,
            DST_AMOUNT,
            Operation.PARSE_DECIMAL,
            CellValue(kind="text", value="1,000.00"),
            CellValue(kind="decimal", value="1000.00"),
        ),
        (
            SRC_DATE,
            DST_DATE,
            Operation.PARSE_DATE,
            CellValue(kind="text", value="2026/8/10"),
            CellValue(kind="date", value="2026-08-10"),
        ),
    ),
)
def test_versioned_deterministic_parser_proposals_are_review_only(
    source_ref: UUID,
    target_ref: UUID,
    operation: Operation,
    before: CellValue,
    after: CellValue,
) -> None:
    pair = _pair(
        source_column_refs=(source_ref,),
        target_column_ref=target_ref,
        operation=operation,
        before=before,
        after=after,
    )
    result = assess_cleaning_proposal(_request(pairs=(pair,)))
    assert RiskFlag.TYPE_COERCION in result.risk_flags
    assert result.outcome == "human_review_required"


def test_operation_allowlist_version_formula_noop_and_arity_gates() -> None:
    request = _request()
    rules = request.rules.model_copy(
        update={
            "operations": tuple(
                item
                for item in request.rules.operations
                if item.operation != Operation.SEMANTIC_REWRITE
            )
        }
    )
    _expect_rejected(request.model_copy(update={"rules": rules}), "operation_not_allowlisted")

    change = request.proposal.changes[0].model_copy(
        update={"operation_implementation_version": "operation/other"}
    )
    _expect_rejected(
        request.model_copy(
            update={"proposal": request.proposal.model_copy(update={"changes": (change,)})}
        ),
        "operation_implementation_mismatch",
    )
    formula = _pair(
        source_column_refs=(),
        target_column_ref=DST_NAME,
        operation=Operation.CONSTANT_VALUE,
        before=CellValue(kind="null", value=None),
        after=CellValue(kind="text", value=" =HYPERLINK(\"x\")"),
    )
    formula_result = assess_cleaning_proposal(_request(pairs=(formula,)))
    assert RiskFlag.FORMULA_LIKE_TEXT in formula_result.risk_flags
    assert RiskFlag.CONSTANT_VALUE_INJECTION in formula_result.risk_flags

    noop = _pair(after=CellValue(kind="text", value="  原始 描述  "))
    _expect_rejected(_request(pairs=(noop,)), "noop_change")
    bad_arity = _pair(
        source_column_refs=(SRC_NAME,),
        target_column_ref=DST_NAME,
        operation=Operation.COMBINE_COLUMNS,
        before=CellValue(kind="text", value="A"),
        after=CellValue(kind="text", value="A B"),
    )
    _expect_rejected(_request(pairs=(bad_arity,)), "invalid_operation_arity")


def test_change_semantic_proposal_and_observed_budgets_are_independent() -> None:
    first = _pair()
    second = _pair(row=3)
    request = _request(pairs=(first, second))
    rules = request.rules.model_copy(update={"maximum_changes": 1})
    _expect_rejected(request.model_copy(update={"rules": rules}), "change_budget_exceeded")
    rules = request.rules.model_copy(update={"semantic_rewrite_limit": 1})
    _expect_rejected(request.model_copy(update={"rules": rules}), "semantic_budget_exceeded")

    large_proposal = tuple(
        _pair(
            row=row,
            source_column_refs=(SRC_NAME,),
            target_column_ref=DST_NAME,
            operation=Operation.TRIM,
            before=CellValue(kind="text", value="a"),
            after=CellValue(kind="text", value="b" * 8_192),
        )
        for row in range(2, 42)
    )
    _expect_rejected(
        _request(pairs=large_proposal),
        "proposal_payload_budget_exceeded",
    )
    large_observed = tuple(
        _pair(
            row=row,
            source_column_refs=(SRC_NAME,),
            target_column_ref=DST_NAME,
            operation=Operation.TRIM,
            before=CellValue(kind="text", value="a" * 8_192),
            after=CellValue(kind="text", value="b"),
        )
        for row in range(2, 42)
    )
    _expect_rejected(
        _request(pairs=large_observed),
        "observed_projection_budget_exceeded",
    )


def test_utf8_byte_budgets_block_emoji_character_count_bypass() -> None:
    CellValue(kind="text", value="😀" * 2_048)
    with pytest.raises(ValidationError):
        CellValue(kind="text", value="😀" * 2_049)
    semantic = _pair(after=CellValue(kind="text", value="😀" * 501))
    _expect_rejected(_request(pairs=(semantic,)), "semantic_value_budget_exceeded")


def test_low_confidence_and_version_changes_remain_explicit_review_evidence() -> None:
    low = _pair(confidence=4_999)
    low_result = assess_cleaning_proposal(_request(pairs=(low,)))
    assert RiskFlag.LOW_CONFIDENCE in low_result.risk_flags
    assert ManualReviewReason.LOW_CONFIDENCE in low_result.manual_review_reasons

    request = _request()
    baseline = assess_cleaning_proposal(request)
    rules = request.rules.model_copy(update={"rule_set_version": "rules/2026-08-10.2"})
    proposal = request.proposal.model_copy(update={"rule_set_version": rules.rule_set_version})
    changed = assess_cleaning_proposal(
        request.model_copy(update={"rules": rules, "proposal": proposal})
    )
    assert changed != baseline
    assert changed.rules_binding.rule_set_version == "rules/2026-08-10.2"


def test_source_and_template_column_order_is_preserved_as_semantic_identity() -> None:
    baseline = assess_cleaning_proposal(_request())
    source_reversed = assess_cleaning_proposal(
        _request(source_columns=tuple(reversed(SOURCE_COLUMNS)))
    )
    template_reversed = assess_cleaning_proposal(
        _request(target_columns=tuple(reversed(TARGET_COLUMNS)))
    )
    assert source_reversed != baseline
    assert template_reversed != baseline
    assert source_reversed.source_binding.ordered_column_refs == tuple(
        column.column_ref for column in reversed(SOURCE_COLUMNS)
    )
    assert template_reversed.template_binding.ordered_column_refs == tuple(
        column.column_ref for column in reversed(TARGET_COLUMNS)
    )


def test_verify_is_the_only_consumption_boundary_and_rejects_tamper() -> None:
    request = _request()
    assessment = assess_cleaning_proposal(request)
    assert verify_cleaning_assessment(request, assessment) == assessment

    tampered_risk = assessment.model_copy(update={"risk_flags": ()})
    with pytest.raises(CleaningProposalRejected) as caught:
        verify_cleaning_assessment(request, tampered_risk)
    assert caught.value.code == "assessment_mismatch"

    diff = assessment.field_diffs[0].model_copy(update={"target_column_ref": DST_NAME})
    tampered_diff = assessment.model_copy(update={"field_diffs": (diff,)})
    with pytest.raises(CleaningProposalRejected) as caught:
        verify_cleaning_assessment(request, tampered_diff)
    assert caught.value.code == "assessment_mismatch"

    invalid = assessment.model_copy(update={"mode": "execute"})
    with pytest.raises(CleaningProposalRejected) as caught:
        verify_cleaning_assessment(request, invalid)
    assert caught.value.code == "invalid_assessment_schema"


def test_verify_binds_a_new_upstream_value_ref_without_hashing_value() -> None:
    request = _request()
    assessment = assess_cleaning_proposal(request)
    change = request.proposal.changes[0].model_copy(
        update={
            "proposed_value_ref": _u(999_999),
            "proposed_after": CellValue(kind="text", value="另一个提案"),
        }
    )
    changed_request = request.model_copy(
        update={"proposal": request.proposal.model_copy(update={"changes": (change,)})}
    )
    with pytest.raises(CleaningProposalRejected) as caught:
        verify_cleaning_assessment(changed_request, assessment)
    assert caught.value.code == "assessment_mismatch"


def test_strict_schema_and_kernel_revalidation_fail_closed() -> None:
    with pytest.raises(ValidationError):
        CellValue.model_validate({"kind": "decimal", "value": 1.5})
    with pytest.raises(ValidationError):
        CellValue.model_validate({"kind": "text", "value": "safe", "extra": "ignored?"})
    unsafe = _request().model_copy(update={"mode": "execute"})
    _expect_rejected(unsafe, "invalid_request_schema")


def test_kernel_has_no_io_runtime_registration_or_homegrown_integrity_surface() -> None:
    package = Path(__file__).parents[1] / "app" / "agent" / "workbook_cleaning"
    source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    forbidden = (
        "sqlalchemy",
        "openpyxl",
        "pathlib",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "subprocess",
        "hashlib",
        "hmac.new",
        "@router",
        "_REGISTRY",
        "TOOLS",
    )
    for marker in forbidden:
        assert marker not in source
    assert "compare_digest" in source

    for consumer in (
        Path(__file__).parents[1] / "app" / "agent" / "runtime.py",
        Path(__file__).parents[1] / "app" / "agent" / "tools.py",
    ):
        assert "workbook_cleaning" not in consumer.read_text(encoding="utf-8")
