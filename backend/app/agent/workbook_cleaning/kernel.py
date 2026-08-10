"""Pure validation, bounded Evidence projection, and verification for #228."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from .models import (
    CleaningProposalAssessment,
    CleaningProposalRequest,
    ColumnKind,
    FieldChange,
    FieldDiffEvidence,
    ManualReviewReason,
    Operation,
    RiskFlag,
    RulesEvidenceBinding,
    SourceEvidenceBinding,
    TemplateClassification,
    TemplateEvidenceBinding,
)


_PROTECTED_COLUMNS = {ColumnKind.ROW_IDENTITY, ColumnKind.PART_ID, ColumnKind.PN}
_TYPED_COLUMNS = {ColumnKind.AMOUNT, ColumnKind.QUANTITY, ColumnKind.DATE}
_MAX_PROPOSAL_BYTES = 256 * 1024
_MAX_OBSERVED_PROJECTION_BYTES = 256 * 1024


class CleaningProposalRejected(ValueError):
    """Stable, content-free rejection raised before any future side effect."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"unsupported canonical value type: {type(value).__name__}")


def _canonical_bytes(payload: Any) -> bytes:
    """Deterministic comparison bytes, not an integrity/signature protocol."""

    return json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _uuid_key(value: UUID) -> str:
    return value.hex


def _field_key(item: Any) -> tuple[UUID, UUID, int, UUID]:
    return (
        item.row_ref.source_snapshot_ref,
        item.row_ref.sheet_ref,
        item.row_ref.row_number,
        item.target_column_ref,
    )


def _change_key(change: FieldChange) -> tuple[Any, ...]:
    return (
        _uuid_key(change.row_ref.source_snapshot_ref),
        _uuid_key(change.row_ref.sheet_ref),
        change.row_ref.row_number,
        _uuid_key(change.target_column_ref),
        change.operation.value,
        tuple(_uuid_key(ref) for ref in change.source_column_refs),
        _uuid_key(change.observed_field_ref),
        _uuid_key(change.proposed_value_ref),
    )


def _operation_arity_valid(change: FieldChange) -> bool:
    count = len(change.source_column_refs)
    if change.operation == Operation.CONSTANT_VALUE:
        return count == 0
    if change.operation in {Operation.COALESCE, Operation.COMBINE_COLUMNS}:
        return count >= 2
    return count == 1


def _formula_like_text(change: FieldChange) -> bool:
    after = change.proposed_after
    return (
        after.kind == "text"
        and isinstance(after.value, str)
        and after.value.lstrip().startswith(("=", "+", "-", "@"))
    )


def _validate_target_operation(change: FieldChange, target_kind: ColumnKind) -> None:
    if target_kind in _PROTECTED_COLUMNS:
        raise CleaningProposalRejected("protected_column")
    if change.operation == Operation.SEMANTIC_REWRITE:
        if target_kind != ColumnKind.SEMANTIC_TEXT:
            raise CleaningProposalRejected("operation_not_allowed_for_column")
        if change.proposed_after.kind != "text":
            raise CleaningProposalRejected("operation_not_allowed_for_column")
        value = change.proposed_after.value
        if isinstance(value, str) and len(value.encode("utf-8")) > 2_000:
            raise CleaningProposalRejected("semantic_value_budget_exceeded")
        return
    if target_kind in _TYPED_COLUMNS:
        if target_kind == ColumnKind.DATE:
            valid = (
                change.operation == Operation.PARSE_DATE
                and change.proposed_after.kind == "date"
            )
        else:
            valid = (
                change.operation == Operation.PARSE_DECIMAL
                and change.proposed_after.kind in {"decimal", "integer"}
            )
        if not valid:
            raise CleaningProposalRejected("operation_not_allowed_for_column")
    elif change.operation in {Operation.PARSE_DATE, Operation.PARSE_DECIMAL}:
        raise CleaningProposalRejected("operation_not_allowed_for_column")


def _change_risks(
    change: FieldChange, request: CleaningProposalRequest
) -> set[RiskFlag]:
    risks: set[RiskFlag] = set()
    if change.operation == Operation.SEMANTIC_REWRITE:
        risks.add(RiskFlag.SEMANTIC_CONTENT_REWRITE)
    if change.operation == Operation.CONSTANT_VALUE:
        risks.add(RiskFlag.CONSTANT_VALUE_INJECTION)
    if change.operation in {Operation.PARSE_DATE, Operation.PARSE_DECIMAL}:
        risks.add(RiskFlag.TYPE_COERCION)
    if change.operation in {Operation.COALESCE, Operation.COMBINE_COLUMNS}:
        risks.add(RiskFlag.MULTI_SOURCE_COMPOSITION)
    if (
        change.confidence_basis_points
        < request.rules.low_confidence_threshold_basis_points
    ):
        risks.add(RiskFlag.LOW_CONFIDENCE)
    if _formula_like_text(change):
        risks.add(RiskFlag.FORMULA_LIKE_TEXT)
    return risks


def _manual_review_reasons(risks: set[RiskFlag]) -> tuple[ManualReviewReason, ...]:
    reasons = {ManualReviewReason.HUMAN_ACCEPT_REQUIRED}
    reasons.update(ManualReviewReason(risk.value) for risk in risks)
    return tuple(sorted(reasons, key=lambda item: item.value))


def _revalidate_request(request: CleaningProposalRequest) -> CleaningProposalRequest:
    try:
        return CleaningProposalRequest.model_validate(request.model_dump(mode="python"))
    except (AttributeError, ValidationError):
        raise CleaningProposalRejected("invalid_request_schema") from None


def assess_cleaning_proposal(
    request: CleaningProposalRequest,
) -> CleaningProposalAssessment:
    """Create non-executable dark-mode Evidence from an untrusted proposal."""

    request = _revalidate_request(request)
    owners = {
        request.task_owner_sub,
        request.source.artifact.owner_sub,
        request.template.artifact.owner_sub,
    }
    if len(owners) != 1:
        raise CleaningProposalRejected("owner_mismatch")
    if request.template.classification == TemplateClassification.UNCLASSIFIED:
        raise CleaningProposalRejected("template_unclassified")

    proposal = request.proposal
    source = request.source
    template = request.template
    rules = request.rules
    if (
        proposal.source_snapshot_ref != source.source_snapshot_ref
        or proposal.source_artifact_id != source.artifact.artifact_id
        or proposal.source_sha256 != source.artifact.sha256
        or proposal.source_projection_implementation_version
        != source.projection_implementation_version
    ):
        raise CleaningProposalRejected("source_binding_mismatch")
    if (
        proposal.template_snapshot_ref != template.template_snapshot_ref
        or proposal.template_artifact_id != template.artifact.artifact_id
        or proposal.template_sha256 != template.artifact.sha256
        or proposal.template_version != template.template_version
        or proposal.template_classifier_proof_version
        != template.classifier_proof_version
    ):
        raise CleaningProposalRejected("template_binding_mismatch")
    if (
        proposal.rule_snapshot_ref != rules.rule_snapshot_ref
        or proposal.rule_set_id != rules.rule_set_id
        or proposal.rule_set_version != rules.rule_set_version
        or proposal.rule_set_sha256 != rules.rule_set_sha256
        or proposal.policy_implementation_version != rules.policy_implementation_version
    ):
        raise CleaningProposalRejected("rule_binding_mismatch")
    if len(proposal.changes) > rules.maximum_changes:
        raise CleaningProposalRejected("change_budget_exceeded")
    if len(_canonical_bytes(proposal.model_dump(mode="json"))) > _MAX_PROPOSAL_BYTES:
        raise CleaningProposalRejected("proposal_payload_budget_exceeded")
    observed_payload = [
        field.model_dump(mode="json") for field in request.observed_fields
    ]
    if len(_canonical_bytes(observed_payload)) > _MAX_OBSERVED_PROJECTION_BYTES:
        raise CleaningProposalRejected("observed_projection_budget_exceeded")

    source_columns = {column.column_ref: column for column in source.columns}
    target_columns = {column.column_ref: column for column in template.columns}
    operation_versions = {
        item.operation: item.implementation_version for item in rules.operations
    }
    observed_by_ref = {}
    observed_targets = set()
    for observed in request.observed_fields:
        if (
            observed.row_ref.source_snapshot_ref != source.source_snapshot_ref
            or observed.row_ref.sheet_ref != source.sheet_ref
            or observed.row_ref.row_number < source.data_start_row
        ):
            raise CleaningProposalRejected("row_reference_mismatch")
        if observed.target_column_ref not in target_columns:
            raise CleaningProposalRejected("unknown_target_column")
        if any(ref not in source_columns for ref in observed.source_column_refs):
            raise CleaningProposalRejected("unknown_source_column")
        if observed.observed_field_ref in observed_by_ref:
            raise CleaningProposalRejected("duplicate_observed_field_ref")
        if _field_key(observed) in observed_targets:
            raise CleaningProposalRejected("duplicate_observed_field")
        observed_by_ref[observed.observed_field_ref] = observed
        observed_targets.add(_field_key(observed))

    ordered_changes = tuple(sorted(proposal.changes, key=_change_key))
    seen_targets = set()
    seen_proposed_values = set()
    diffs: list[FieldDiffEvidence] = []
    all_risks: set[RiskFlag] = set()
    semantic_count = 0
    for change in ordered_changes:
        if (
            change.row_ref.source_snapshot_ref != source.source_snapshot_ref
            or change.row_ref.sheet_ref != source.sheet_ref
        ):
            raise CleaningProposalRejected("row_reference_mismatch")
        if change.row_ref.row_number < source.data_start_row:
            raise CleaningProposalRejected("row_outside_data_range")
        target_key = _field_key(change)
        if target_key in seen_targets:
            raise CleaningProposalRejected("duplicate_target_field_change")
        seen_targets.add(target_key)
        if change.proposed_value_ref in seen_proposed_values:
            raise CleaningProposalRejected("duplicate_proposed_value_ref")
        seen_proposed_values.add(change.proposed_value_ref)
        if change.target_column_ref not in target_columns:
            raise CleaningProposalRejected("unknown_target_column")
        if any(ref not in source_columns for ref in change.source_column_refs):
            raise CleaningProposalRejected("unknown_source_column")
        observed = observed_by_ref.get(change.observed_field_ref)
        if observed is None:
            raise CleaningProposalRejected("observed_field_missing")
        if (
            change.row_ref != observed.row_ref
            or change.source_column_refs != observed.source_column_refs
            or change.target_column_ref != observed.target_column_ref
        ):
            raise CleaningProposalRejected("observed_field_mismatch")
        if not _operation_arity_valid(change):
            raise CleaningProposalRejected("invalid_operation_arity")
        implementation_version = operation_versions.get(change.operation)
        if implementation_version is None:
            raise CleaningProposalRejected("operation_not_allowlisted")
        if change.operation_implementation_version != implementation_version:
            raise CleaningProposalRejected("operation_implementation_mismatch")
        if observed.before == change.proposed_after:
            raise CleaningProposalRejected("noop_change")
        _validate_target_operation(
            change, target_columns[change.target_column_ref].kind
        )

        if change.operation == Operation.SEMANTIC_REWRITE:
            semantic_count += 1
        risks = _change_risks(change, request)
        all_risks.update(risks)
        diffs.append(
            FieldDiffEvidence(
                observed_field_ref=change.observed_field_ref,
                proposed_value_ref=change.proposed_value_ref,
                row_ref=change.row_ref,
                source_column_refs=change.source_column_refs,
                target_column_ref=change.target_column_ref,
                operation=change.operation,
                operation_implementation_version=implementation_version,
                confidence_basis_points=change.confidence_basis_points,
                risk_flags=tuple(sorted(risks, key=lambda item: item.value)),
            )
        )
    if semantic_count > rules.semantic_rewrite_limit:
        raise CleaningProposalRejected("semantic_budget_exceeded")
    if len(diffs) >= rules.large_change_review_threshold:
        all_risks.add(RiskFlag.LARGE_CHANGE_SET)
    if template.classification == TemplateClassification.BUSINESS_CONTENT:
        all_risks.add(RiskFlag.TEMPLATE_BUSINESS_CONTENT)

    return CleaningProposalAssessment(
        proposal_ref=proposal.proposal_ref,
        source_binding=SourceEvidenceBinding(
            source_snapshot_ref=source.source_snapshot_ref,
            artifact_id=source.artifact.artifact_id,
            projection_implementation_version=source.projection_implementation_version,
            sheet_ref=source.sheet_ref,
            header_row=source.header_row,
            data_start_row=source.data_start_row,
            ordered_column_refs=tuple(column.column_ref for column in source.columns),
        ),
        template_binding=TemplateEvidenceBinding(
            template_snapshot_ref=template.template_snapshot_ref,
            artifact_id=template.artifact.artifact_id,
            template_version=template.template_version,
            classification=template.classification,
            classifier_proof_version=template.classifier_proof_version,
            target_sheet_ref=template.target_sheet_ref,
            ordered_column_refs=tuple(column.column_ref for column in template.columns),
        ),
        rules_binding=RulesEvidenceBinding(
            rule_snapshot_ref=rules.rule_snapshot_ref,
            rule_set_version=rules.rule_set_version,
            policy_implementation_version=rules.policy_implementation_version,
            operations=tuple(
                sorted(rules.operations, key=lambda item: item.operation.value)
            ),
            maximum_changes=rules.maximum_changes,
            semantic_rewrite_limit=rules.semantic_rewrite_limit,
            low_confidence_threshold_basis_points=(
                rules.low_confidence_threshold_basis_points
            ),
            large_change_review_threshold=rules.large_change_review_threshold,
        ),
        proposal_schema_version=proposal.schema_version,
        proposal_origin=proposal.origin,
        change_count=len(diffs),
        semantic_rewrite_count=semantic_count,
        field_diffs=tuple(diffs),
        risk_flags=tuple(sorted(all_risks, key=lambda item: item.value)),
        manual_review_reasons=_manual_review_reasons(all_risks),
    )


def verify_cleaning_assessment(
    request: CleaningProposalRequest,
    assessment: CleaningProposalAssessment,
) -> CleaningProposalAssessment:
    """Only supported consumption boundary for an assessment.

    This detects accidental or adversarial mutation by re-assessing the complete
    request and comparing the bounded canonical projections in constant time. It
    is not authenticity, authorization, or `integrity-envelope/v1`; callers must
    consume only the returned fresh instance.
    """

    request = _revalidate_request(request)
    try:
        supplied = CleaningProposalAssessment.model_validate(
            assessment.model_dump(mode="python")
        )
    except (AttributeError, ValidationError):
        raise CleaningProposalRejected("invalid_assessment_schema") from None
    expected = assess_cleaning_proposal(request)
    expected_bytes = _canonical_bytes(expected.model_dump(mode="json"))
    supplied_bytes = _canonical_bytes(supplied.model_dump(mode="json"))
    if not hmac.compare_digest(expected_bytes, supplied_bytes):
        raise CleaningProposalRejected("assessment_mismatch")
    return expected
