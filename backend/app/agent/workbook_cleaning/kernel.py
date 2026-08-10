"""Deterministic validation and evidence projection for cleaning proposals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

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


class CleaningProposalRejected(ValueError):
    """Stable, content-free rejection returned before any future side effect."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _json_safe(value: Any) -> Any:
    """Return a deterministic JSON tree and fail closed on unsupported values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"unsupported canonical value type: {type(value).__name__}")


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _change_key(change: FieldChange) -> tuple[Any, ...]:
    return (
        change.row_ref.source_sha256,
        change.row_ref.sheet,
        change.row_ref.row_number,
        change.target_column_id,
        change.operation.value,
        change.source_column_ids,
    )


def _field_key(change: Any) -> tuple[str, int, str]:
    return (
        change.row_ref.sheet,
        change.row_ref.row_number,
        change.target_column_id,
    )


def _observed_sort_key(observed: Any) -> tuple[Any, ...]:
    return (
        observed.row_ref.source_sha256,
        observed.row_ref.sheet,
        observed.row_ref.row_number,
        observed.target_column_id,
        observed.source_column_ids,
    )


def _operation_arity_valid(change: FieldChange) -> bool:
    count = len(change.source_column_ids)
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
        if isinstance(change.proposed_after.value, str) and len(change.proposed_after.value) > 2_000:
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
    for risk in risks:
        try:
            reasons.add(ManualReviewReason(risk.value))
        except ValueError:
            # Every current risk has a review counterpart.  Fail closed if a future
            # low-level risk is intentionally evidence-only.
            reasons.add(ManualReviewReason.HUMAN_ACCEPT_REQUIRED)
    return tuple(sorted(reasons, key=lambda item: item.value))


def _normalized_proposal_payload(
    request: CleaningProposalRequest, ordered_changes: tuple[FieldChange, ...]
) -> dict[str, Any]:
    return {
        "request_schema_version": request.schema_version,
        "mode": request.mode,
        "owner_subject_sha256": _fingerprint({"owner_sub": request.task_owner_sub}),
        "source": {
            **request.source.model_dump(mode="json", exclude={"artifact": {"owner_sub"}}),
            "columns": [
                column.model_dump(mode="json")
                for column in sorted(request.source.columns, key=lambda item: item.column_id)
            ],
        },
        "observed_fields": [
            field.model_dump(mode="json")
            for field in sorted(request.observed_fields, key=_observed_sort_key)
        ],
        "template": {
            **request.template.model_dump(mode="json", exclude={"artifact": {"owner_sub"}}),
            "columns": [
                column.model_dump(mode="json")
                for column in sorted(request.template.columns, key=lambda item: item.column_id)
            ],
        },
        "rules": {
            **request.rules.model_dump(mode="json", exclude={"operations"}),
            "operations": [
                item.model_dump(mode="json")
                for item in sorted(
                    request.rules.operations, key=lambda item: item.operation.value
                )
            ],
        },
        "proposal": {
            **request.proposal.model_dump(mode="json", exclude={"changes"}),
            "changes": [change.model_dump(mode="json") for change in ordered_changes],
        },
    }


def assess_cleaning_proposal(
    request: CleaningProposalRequest,
) -> CleaningProposalAssessment:
    """Validate an untrusted proposal and return hash-only review evidence.

    The output deliberately cannot authorize execution, Artifact creation, or a
    business write.  Future workflow code must place it behind a Human Interrupt
    and the authoritative Capability/Task/Artifact gates.
    """

    # Internal callers can bypass Pydantic validation with model_construct/model_copy.
    # Revalidate the complete frozen tree so that this safety boundary does not
    # silently trust the caller's construction path.
    try:
        request = CleaningProposalRequest.model_validate(
            request.model_dump(mode="python")
        )
    except (AttributeError, ValidationError):
        raise CleaningProposalRejected("invalid_request_schema") from None

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
        proposal.source_artifact_id != source.artifact.artifact_id
        or proposal.source_sha256 != source.artifact.sha256
        or proposal.source_projection_implementation_version
        != source.projection_implementation_version
    ):
        raise CleaningProposalRejected("source_binding_mismatch")
    if (
        proposal.template_artifact_id != template.artifact.artifact_id
        or proposal.template_sha256 != template.artifact.sha256
        or proposal.template_version != template.template_version
        or proposal.template_classifier_proof_version
        != template.classifier_proof_version
    ):
        raise CleaningProposalRejected("template_binding_mismatch")
    if (
        proposal.rule_set_id != rules.rule_set_id
        or proposal.rule_set_version != rules.rule_set_version
        or proposal.rule_set_sha256 != rules.rule_set_sha256
        or proposal.policy_implementation_version != rules.policy_implementation_version
    ):
        raise CleaningProposalRejected("rule_binding_mismatch")
    if len(proposal.changes) > rules.maximum_changes:
        raise CleaningProposalRejected("change_budget_exceeded")
    if len(_canonical_bytes(proposal.model_dump(mode="json"))) > _MAX_PROPOSAL_BYTES:
        raise CleaningProposalRejected("proposal_payload_budget_exceeded")

    source_columns = {column.column_id: column for column in source.columns}
    target_columns = {column.column_id: column for column in template.columns}
    operation_versions = {
        item.operation: item.implementation_version for item in rules.operations
    }
    ordered_changes = tuple(sorted(proposal.changes, key=_change_key))

    observed_by_target = {}
    for observed in request.observed_fields:
        if (
            observed.row_ref.source_sha256 != source.artifact.sha256
            or observed.row_ref.sheet != source.sheet
            or observed.row_ref.row_number < source.data_start_row
        ):
            raise CleaningProposalRejected("row_reference_mismatch")
        if observed.target_column_id not in target_columns:
            raise CleaningProposalRejected("unknown_target_column")
        if any(column_id not in source_columns for column_id in observed.source_column_ids):
            raise CleaningProposalRejected("unknown_source_column")
        observed_key = _field_key(observed)
        if observed_key in observed_by_target:
            raise CleaningProposalRejected("duplicate_observed_field")
        observed_by_target[observed_key] = observed

    seen_targets: set[tuple[str, int, str]] = set()
    diffs: list[FieldDiffEvidence] = []
    all_risks: set[RiskFlag] = set()
    semantic_count = 0
    for change in ordered_changes:
        if (
            change.row_ref.source_sha256 != source.artifact.sha256
            or change.row_ref.sheet != source.sheet
        ):
            raise CleaningProposalRejected("row_reference_mismatch")
        if change.row_ref.row_number < source.data_start_row:
            raise CleaningProposalRejected("row_outside_data_range")
        target_key = _field_key(change)
        if target_key in seen_targets:
            raise CleaningProposalRejected("duplicate_target_field_change")
        seen_targets.add(target_key)
        if change.target_column_id not in target_columns:
            raise CleaningProposalRejected("unknown_target_column")
        if any(column_id not in source_columns for column_id in change.source_column_ids):
            raise CleaningProposalRejected("unknown_source_column")
        observed = observed_by_target.get(target_key)
        if observed is None:
            raise CleaningProposalRejected("observed_field_missing")
        if (
            change.source_column_ids != observed.source_column_ids
            or change.before != observed.before
        ):
            raise CleaningProposalRejected("observed_field_mismatch")
        if not _operation_arity_valid(change):
            raise CleaningProposalRejected("invalid_operation_arity")
        implementation_version = operation_versions.get(change.operation)
        if implementation_version is None:
            raise CleaningProposalRejected("operation_not_allowlisted")
        if change.operation_implementation_version != implementation_version:
            raise CleaningProposalRejected("operation_implementation_mismatch")
        if change.before == change.proposed_after:
            raise CleaningProposalRejected("noop_change")
        _validate_target_operation(change, target_columns[change.target_column_id].kind)

        if change.operation == Operation.SEMANTIC_REWRITE:
            semantic_count += 1
        risks = _change_risks(change, request)
        all_risks.update(risks)
        diffs.append(
            FieldDiffEvidence(
                row_ref=change.row_ref,
                source_column_ids=change.source_column_ids,
                target_column_id=change.target_column_id,
                operation=change.operation,
                operation_implementation_version=implementation_version,
                before_value_sha256=_fingerprint(observed.before.model_dump(mode="json")),
                after_value_sha256=_fingerprint(
                    change.proposed_after.model_dump(mode="json")
                ),
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

    sorted_operations = tuple(
        sorted(rules.operations, key=lambda item: item.operation.value)
    )
    base_payload: dict[str, Any] = {
        "owner_subject_sha256": _fingerprint({"owner_sub": request.task_owner_sub}),
        "source_binding": SourceEvidenceBinding(
            artifact_id=source.artifact.artifact_id,
            sha256=source.artifact.sha256,
            projection_implementation_version=source.projection_implementation_version,
            sheet=source.sheet,
            header_row=source.header_row,
            data_start_row=source.data_start_row,
        ),
        "template_binding": TemplateEvidenceBinding(
            artifact_id=template.artifact.artifact_id,
            sha256=template.artifact.sha256,
            template_version=template.template_version,
            classification=template.classification,
            classifier_proof_version=template.classifier_proof_version,
            target_sheet=template.target_sheet,
        ),
        "rules_binding": RulesEvidenceBinding(
            rule_set_id=rules.rule_set_id,
            rule_set_version=rules.rule_set_version,
            rule_set_sha256=rules.rule_set_sha256,
            policy_implementation_version=rules.policy_implementation_version,
            operations=sorted_operations,
            maximum_changes=rules.maximum_changes,
            semantic_rewrite_limit=rules.semantic_rewrite_limit,
            low_confidence_threshold_basis_points=(
                rules.low_confidence_threshold_basis_points
            ),
            large_change_review_threshold=rules.large_change_review_threshold,
        ),
        "proposal_schema_version": proposal.schema_version,
        "proposal_origin": proposal.origin,
        "change_count": len(diffs),
        "semantic_rewrite_count": semantic_count,
        "field_diffs": tuple(diffs),
        "risk_flags": tuple(sorted(all_risks, key=lambda item: item.value)),
        "manual_review_reasons": _manual_review_reasons(all_risks),
        "proposal_fingerprint": _fingerprint(
            _normalized_proposal_payload(request, ordered_changes)
        ),
    }
    evidence_payload = {
        "schema_version": "workbook-cleaning-proposal-assessment/v1",
        "assessment_implementation_version": (
            "workbook-cleaning-proposal-kernel/1.0.0"
        ),
        "mode": "dark",
        "outcome": "human_review_required",
        "executable": False,
        "artifact_create_allowed": False,
        "business_write_allowed": False,
        **{
            key: (
                value.model_dump(mode="json")
                if hasattr(value, "model_dump")
                else [item.model_dump(mode="json") for item in value]
                if isinstance(value, tuple) and value and hasattr(value[0], "model_dump")
                else value
            )
            for key, value in base_payload.items()
        },
    }
    return CleaningProposalAssessment(
        **base_payload,
        evidence_payload_fingerprint=_fingerprint(evidence_payload),
    )
