"""Issue #228: deterministic dark-mode workbook-cleaning proposal kernel."""

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
)
from app.agent.workbook_cleaning.kernel import CleaningProposalRejected


SOURCE_SHA = "1" * 64
TEMPLATE_SHA = "2" * 64
RULES_SHA = "3" * 64


def _request(
    *,
    changes: tuple[FieldChange, ...] | None = None,
    observed_fields: tuple[ObservedFieldSnapshot, ...] | None = None,
    template_classification: TemplateClassification = TemplateClassification.IDENTITY_ONLY,
) -> CleaningProposalRequest:
    source_artifact = ArtifactSnapshot(
        artifact_id=UUID("11111111-1111-4111-8111-111111111111"),
        sha256=SOURCE_SHA,
        owner_sub="worker-001",
    )
    template_artifact = ArtifactSnapshot(
        artifact_id=UUID("22222222-2222-4222-8222-222222222222"),
        sha256=TEMPLATE_SHA,
        owner_sub="worker-001",
    )
    source_table = TableSnapshot(
        artifact=source_artifact,
        projection_implementation_version="local-table-projection/1.0.0",
        sheet="源数据",
        header_row=1,
        data_start_row=2,
        columns=(
            ColumnSnapshot(column_id="src-row", kind=ColumnKind.ROW_IDENTITY),
            ColumnSnapshot(column_id="src-description", kind=ColumnKind.SEMANTIC_TEXT),
            ColumnSnapshot(column_id="src-name", kind=ColumnKind.TEXT),
            ColumnSnapshot(column_id="src-amount", kind=ColumnKind.AMOUNT),
            ColumnSnapshot(column_id="src-date", kind=ColumnKind.DATE),
            ColumnSnapshot(column_id="src-pn", kind=ColumnKind.PN),
        ),
    )
    template = TemplateSnapshot(
        artifact=template_artifact,
        template_version="client-template/2026-08-10.1",
        classification=template_classification,
        classifier_proof_version="template-classifier/1.0.0",
        target_sheet="清洗结果",
        columns=(
            ColumnSnapshot(column_id="dst-description", kind=ColumnKind.SEMANTIC_TEXT),
            ColumnSnapshot(column_id="dst-name", kind=ColumnKind.TEXT),
            ColumnSnapshot(column_id="dst-amount", kind=ColumnKind.AMOUNT),
            ColumnSnapshot(column_id="dst-date", kind=ColumnKind.DATE),
            ColumnSnapshot(column_id="dst-pn", kind=ColumnKind.PN),
        ),
    )
    operations = tuple(
        OperationImplementation(operation=operation, implementation_version="operation/1.0.0")
        for operation in Operation
    )
    rules = RuleSetSnapshot(
        rule_set_id="client-description-rules",
        rule_set_version="rules/2026-08-10.1",
        rule_set_sha256=RULES_SHA,
        policy_implementation_version="cleaning-policy/1.0.0",
        operations=operations,
        maximum_changes=200,
        semantic_rewrite_limit=100,
        low_confidence_threshold_basis_points=5_000,
        large_change_review_threshold=100,
    )
    if changes is None:
        changes = (
            FieldChange(
                row_ref=SourceRowRef(
                    source_sha256=SOURCE_SHA,
                    sheet="源数据",
                    row_number=2,
                ),
                source_column_ids=("src-description",),
                target_column_id="dst-description",
                operation=Operation.SEMANTIC_REWRITE,
                operation_implementation_version="operation/1.0.0",
                before=CellValue(kind="text", value="  原始 描述  "),
                proposed_after=CellValue(kind="text", value="标准描述"),
                reason_code="description_normalized",
                confidence_basis_points=9_200,
            ),
        )
    proposal = CleaningChangeProposal(
        origin=ProposalOrigin.AI,
        source_artifact_id=source_artifact.artifact_id,
        source_sha256=source_artifact.sha256,
        source_projection_implementation_version=(
            source_table.projection_implementation_version
        ),
        template_artifact_id=template_artifact.artifact_id,
        template_sha256=template_artifact.sha256,
        template_version=template.template_version,
        template_classifier_proof_version=template.classifier_proof_version,
        rule_set_id=rules.rule_set_id,
        rule_set_version=rules.rule_set_version,
        rule_set_sha256=rules.rule_set_sha256,
        policy_implementation_version=rules.policy_implementation_version,
        changes=changes,
    )
    if observed_fields is None:
        observed_by_target: dict[tuple[str, int, str], ObservedFieldSnapshot] = {}
        for change in changes:
            key = (
                change.row_ref.sheet,
                change.row_ref.row_number,
                change.target_column_id,
            )
            observed_by_target.setdefault(
                key,
                ObservedFieldSnapshot(
                    row_ref=change.row_ref,
                    source_column_ids=change.source_column_ids,
                    target_column_id=change.target_column_id,
                    before=change.before,
                ),
            )
        observed_fields = tuple(observed_by_target.values())
    return CleaningProposalRequest(
        task_owner_sub="worker-001",
        source=source_table,
        template=template,
        rules=rules,
        observed_fields=observed_fields,
        proposal=proposal,
    )


def _replace_proposal(request: CleaningProposalRequest, **changes: object) -> CleaningProposalRequest:
    proposal = request.proposal.model_copy(update=changes)
    return request.model_copy(update={"proposal": proposal})


def _expect_rejected(request: CleaningProposalRequest, code: str) -> None:
    with pytest.raises(CleaningProposalRejected) as caught:
        assess_cleaning_proposal(request)
    assert caught.value.code == code


def test_dark_assessment_is_proposal_only_and_evidence_has_no_raw_values() -> None:
    result = assess_cleaning_proposal(_request())

    assert result.mode == "dark"
    assert result.outcome == "human_review_required"
    assert result.executable is False
    assert result.artifact_create_allowed is False
    assert result.business_write_allowed is False
    assert result.change_count == 1
    assert result.semantic_rewrite_count == 1
    assert result.source_binding.sha256 == SOURCE_SHA
    assert result.template_binding.template_version == "client-template/2026-08-10.1"
    assert result.rules_binding.rule_set_version == "rules/2026-08-10.1"
    assert result.rules_binding.maximum_changes == 200
    assert result.rules_binding.semantic_rewrite_limit == 100
    assert result.rules_binding.low_confidence_threshold_basis_points == 5_000
    assert result.rules_binding.large_change_review_threshold == 100
    assert result.field_diffs[0].before_value_sha256 != result.field_diffs[0].after_value_sha256
    assert result.field_diffs[0].requires_human_review is True
    assert (
        result.source_binding.projection_implementation_version
        == "local-table-projection/1.0.0"
    )
    assert RiskFlag.SEMANTIC_CONTENT_REWRITE in result.risk_flags
    assert ManualReviewReason.HUMAN_ACCEPT_REQUIRED in result.manual_review_reasons
    assert len(result.proposal_fingerprint) == 64
    assert len(result.evidence_payload_fingerprint) == 64

    serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert "原始 描述" not in serialized
    assert "标准描述" not in serialized
    assert "worker-001" not in serialized


def test_same_input_is_bit_for_bit_deterministic() -> None:
    request = _request()
    assert assess_cleaning_proposal(request) == assess_cleaning_proposal(request)


def test_change_order_is_not_part_of_change_set_identity() -> None:
    first = _request().proposal.changes[0]
    second = FieldChange(
        row_ref=SourceRowRef(source_sha256=SOURCE_SHA, sheet="源数据", row_number=3),
        source_column_ids=("src-name",),
        target_column_id="dst-name",
        operation=Operation.TRIM,
        operation_implementation_version="operation/1.0.0",
        before=CellValue(kind="text", value="  Alice "),
        proposed_after=CellValue(kind="text", value="Alice"),
        reason_code="trimmed",
        confidence_basis_points=10_000,
    )
    left = _request(changes=(first, second))
    right = _request(changes=(second, first))

    assert assess_cleaning_proposal(left) == assess_cleaning_proposal(right)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("source_sha256", "4" * 64, "source_binding_mismatch"),
        (
            "source_projection_implementation_version",
            "local-table-projection/other",
            "source_binding_mismatch",
        ),
        (
            "source_artifact_id",
            UUID("44444444-4444-4444-8444-444444444444"),
            "source_binding_mismatch",
        ),
        ("template_sha256", "5" * 64, "template_binding_mismatch"),
        ("template_version", "client-template/other", "template_binding_mismatch"),
        (
            "template_classifier_proof_version",
            "template-classifier/other",
            "template_binding_mismatch",
        ),
        ("rule_set_sha256", "6" * 64, "rule_binding_mismatch"),
        ("rule_set_version", "rules/other", "rule_binding_mismatch"),
        (
            "policy_implementation_version",
            "cleaning-policy/other",
            "rule_binding_mismatch",
        ),
    ),
)
def test_proposal_must_bind_every_authoritative_input(field: str, value: object, code: str) -> None:
    _expect_rejected(_replace_proposal(_request(), **{field: value}), code)


def test_all_artifacts_and_task_must_have_same_owner() -> None:
    request = _request()
    foreign_template = request.template.model_copy(
        update={
            "artifact": request.template.artifact.model_copy(update={"owner_sub": "worker-002"})
        }
    )
    _expect_rejected(request.model_copy(update={"template": foreign_template}), "owner_mismatch")


def test_unclassified_template_fails_closed() -> None:
    _expect_rejected(
        _request(template_classification=TemplateClassification.UNCLASSIFIED),
        "template_unclassified",
    )


def test_business_content_template_is_bound_and_flagged() -> None:
    result = assess_cleaning_proposal(
        _request(template_classification=TemplateClassification.BUSINESS_CONTENT)
    )
    assert result.template_binding.classification == TemplateClassification.BUSINESS_CONTENT
    assert RiskFlag.TEMPLATE_BUSINESS_CONTENT in result.risk_flags


def test_duplicate_row_and_target_field_is_rejected() -> None:
    change = _request().proposal.changes[0]
    _expect_rejected(_request(changes=(change, change)), "duplicate_target_field_change")


def test_row_reference_must_bind_source_snapshot() -> None:
    original = _request().proposal.changes[0]
    wrong_hash = original.model_copy(
        update={
            "row_ref": original.row_ref.model_copy(update={"source_sha256": "7" * 64})
        }
    )
    wrong_sheet = original.model_copy(
        update={"row_ref": original.row_ref.model_copy(update={"sheet": "别的表"})}
    )
    _expect_rejected(_request(changes=(wrong_hash,)), "row_reference_mismatch")
    _expect_rejected(_request(changes=(wrong_sheet,)), "row_reference_mismatch")


def test_untrusted_before_must_match_authoritative_local_projection() -> None:
    request = _request()
    change = request.proposal.changes[0].model_copy(
        update={"before": CellValue(kind="text", value="伪造的原值")}
    )
    _expect_rejected(
        request.model_copy(
            update={"proposal": request.proposal.model_copy(update={"changes": (change,)})}
        ),
        "observed_field_mismatch",
    )


def test_unknown_source_or_target_column_fails_closed() -> None:
    original = _request().proposal.changes[0]
    _expect_rejected(
        _request(changes=(original.model_copy(update={"source_column_ids": ("missing",)}),)),
        "unknown_source_column",
    )
    _expect_rejected(
        _request(changes=(original.model_copy(update={"target_column_id": "missing"}),)),
        "unknown_target_column",
    )


def test_semantic_rewrite_is_only_allowed_for_semantic_text() -> None:
    original = _request().proposal.changes[0]
    amount_change = original.model_copy(
        update={
            "source_column_ids": ("src-amount",),
            "target_column_id": "dst-amount",
            "before": CellValue(kind="decimal", value="100.00"),
            "proposed_after": CellValue(kind="decimal", value="99.00"),
        }
    )
    _expect_rejected(_request(changes=(amount_change,)), "operation_not_allowed_for_column")


@pytest.mark.parametrize("target_column", ("dst-pn",))
def test_identity_fields_are_immutable_in_this_slice(target_column: str) -> None:
    original = _request().proposal.changes[0]
    change = original.model_copy(
        update={
            "source_column_ids": ("src-pn",),
            "target_column_id": target_column,
            "operation": Operation.TRIM,
            "before": CellValue(kind="text", value=" PN-1 "),
            "proposed_after": CellValue(kind="text", value="PN-1"),
        }
    )
    _expect_rejected(_request(changes=(change,)), "protected_column")


@pytest.mark.parametrize(
    ("source_column", "target_column", "operation", "before", "after"),
    (
        (
            "src-amount",
            "dst-amount",
            Operation.PARSE_DECIMAL,
            CellValue(kind="text", value="1,000.00"),
            CellValue(kind="decimal", value="1000.00"),
        ),
        (
            "src-date",
            "dst-date",
            Operation.PARSE_DATE,
            CellValue(kind="text", value="2026/8/10"),
            CellValue(kind="date", value="2026-08-10"),
        ),
    ),
)
def test_versioned_deterministic_parsers_may_propose_typed_changes(
    source_column: str,
    target_column: str,
    operation: Operation,
    before: CellValue,
    after: CellValue,
) -> None:
    original = _request().proposal.changes[0]
    change = original.model_copy(
        update={
            "source_column_ids": (source_column,),
            "target_column_id": target_column,
            "operation": operation,
            "before": before,
            "proposed_after": after,
        }
    )
    result = assess_cleaning_proposal(_request(changes=(change,)))
    assert RiskFlag.TYPE_COERCION in result.risk_flags
    assert result.outcome == "human_review_required"


def test_operation_must_be_present_in_versioned_rule_set() -> None:
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


def test_operation_implementation_must_match_versioned_rule_set() -> None:
    request = _request()
    change = request.proposal.changes[0].model_copy(
        update={"operation_implementation_version": "operation/other"}
    )
    _expect_rejected(
        request.model_copy(
            update={"proposal": request.proposal.model_copy(update={"changes": (change,)})}
        ),
        "operation_implementation_mismatch",
    )


def test_formula_like_text_is_flagged_without_becoming_executable() -> None:
    original = _request().proposal.changes[0]
    formula = original.model_copy(
        update={
            "operation": Operation.CONSTANT_VALUE,
            "source_column_ids": (),
            "target_column_id": "dst-name",
            "before": CellValue(kind="null", value=None),
            "proposed_after": CellValue(kind="text", value="  =HYPERLINK(\"x\")"),
        }
    )
    result = assess_cleaning_proposal(_request(changes=(formula,)))
    assert RiskFlag.FORMULA_LIKE_TEXT in result.risk_flags
    assert RiskFlag.CONSTANT_VALUE_INJECTION in result.risk_flags
    assert result.executable is False


def test_noop_and_wrong_operation_arity_are_rejected() -> None:
    original = _request().proposal.changes[0]
    _expect_rejected(
        _request(changes=(original.model_copy(update={"proposed_after": original.before}),)),
        "noop_change",
    )
    _expect_rejected(
        _request(changes=(original.model_copy(update={"source_column_ids": ()}),)),
        "invalid_operation_arity",
    )
    combined = original.model_copy(
        update={
            "operation": Operation.COMBINE_COLUMNS,
            "source_column_ids": ("src-name",),
            "target_column_id": "dst-name",
        }
    )
    _expect_rejected(_request(changes=(combined,)), "invalid_operation_arity")


def test_rule_budgets_are_enforced_before_assessment() -> None:
    request = _request()
    rules = request.rules.model_copy(update={"maximum_changes": 1})
    first = request.proposal.changes[0]
    second = first.model_copy(
        update={"row_ref": first.row_ref.model_copy(update={"row_number": 3})}
    )
    _expect_rejected(
        request.model_copy(
            update={
                "rules": rules,
                "proposal": request.proposal.model_copy(update={"changes": (first, second)}),
            }
        ),
        "change_budget_exceeded",
    )


def test_semantic_budget_is_enforced() -> None:
    request = _request()
    rules = request.rules.model_copy(update={"semantic_rewrite_limit": 0})
    _expect_rejected(request.model_copy(update={"rules": rules}), "semantic_budget_exceeded")


def test_serialized_proposal_budget_is_enforced_before_diff_projection() -> None:
    template = _request().proposal.changes[0]
    changes = tuple(
        template.model_copy(
            update={
                "row_ref": template.row_ref.model_copy(update={"row_number": row}),
                "source_column_ids": ("src-name",),
                "target_column_id": "dst-name",
                "operation": Operation.TRIM,
                "before": CellValue(kind="text", value="a" * 8_192),
                "proposed_after": CellValue(kind="text", value="b" * 8_192),
            }
        )
        for row in range(2, 22)
    )
    _expect_rejected(
        _request(changes=changes),
        "proposal_payload_budget_exceeded",
    )


def test_low_confidence_is_a_computed_review_risk() -> None:
    original = _request().proposal.changes[0]
    low_confidence = original.model_copy(update={"confidence_basis_points": 4_999})
    result = assess_cleaning_proposal(_request(changes=(low_confidence,)))
    assert RiskFlag.LOW_CONFIDENCE in result.risk_flags
    assert ManualReviewReason.LOW_CONFIDENCE in result.manual_review_reasons


def test_version_and_snapshot_changes_rekey_evidence() -> None:
    baseline_request = _request()
    baseline = assess_cleaning_proposal(baseline_request)

    changed_rules = baseline_request.rules.model_copy(
        update={"rule_set_version": "rules/2026-08-10.2"}
    )
    changed_proposal = baseline_request.proposal.model_copy(
        update={"rule_set_version": changed_rules.rule_set_version}
    )
    changed = assess_cleaning_proposal(
        baseline_request.model_copy(
            update={"rules": changed_rules, "proposal": changed_proposal}
        )
    )
    assert changed.proposal_fingerprint != baseline.proposal_fingerprint
    assert changed.evidence_payload_fingerprint != baseline.evidence_payload_fingerprint


def test_strict_schema_rejects_float_values_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CellValue.model_validate({"kind": "decimal", "value": 1.5})
    with pytest.raises(ValidationError):
        CellValue.model_validate({"kind": "text", "value": "safe", "payload": "ignored?"})


def test_kernel_revalidates_instances_constructed_through_unsafe_copy_paths() -> None:
    unsafe = _request().model_copy(update={"mode": "execute"})
    _expect_rejected(unsafe, "invalid_request_schema")


def test_kernel_has_no_io_or_runtime_registration_surface() -> None:
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
        "@router",
        "_REGISTRY",
        "TOOLS",
    )
    for marker in forbidden:
        assert marker not in source

    tracked_consumers = (
        Path(__file__).parents[1] / "app" / "agent" / "runtime.py",
        Path(__file__).parents[1] / "app" / "agent" / "tools.py",
    )
    for consumer in tracked_consumers:
        assert "workbook_cleaning" not in consumer.read_text(encoding="utf-8")
