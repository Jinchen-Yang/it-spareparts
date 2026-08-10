"""Strict, side-effect-free contracts for workbook-cleaning proposals.

These models describe a *proposal*.  They are intentionally not an execution
plan, file writer, Artifact creator, or business-data mutation API.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Version = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    ),
]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
    ),
]
OwnerSubject = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
SheetName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class StrictModel(BaseModel):
    """No coercion, ignored fields, or mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TemplateClassification(str, Enum):
    IDENTITY_ONLY = "identity_only"
    BUSINESS_CONTENT = "business_content"
    UNCLASSIFIED = "unclassified"


class ProposalOrigin(str, Enum):
    AI = "ai"
    ALGORITHM = "algorithm"


class ColumnKind(str, Enum):
    ROW_IDENTITY = "row_identity"
    PART_ID = "part_id"
    PN = "pn"
    SEMANTIC_TEXT = "semantic_text"
    TEXT = "text"
    AMOUNT = "amount"
    QUANTITY = "quantity"
    DATE = "date"
    BOOLEAN = "boolean"
    OTHER = "other"


class Operation(str, Enum):
    COPY_COLUMN = "copy_column"
    CONSTANT_VALUE = "constant_value"
    TRIM = "trim"
    COLLAPSE_WHITESPACE = "collapse_whitespace"
    NORMALIZE_UNICODE_WIDTH = "normalize_unicode_width"
    NORMALIZE_CASE = "normalize_case"
    LITERAL_REPLACE = "literal_replace"
    DICTIONARY_MAP = "dictionary_map"
    PARSE_DATE = "parse_date"
    PARSE_DECIMAL = "parse_decimal"
    COALESCE = "coalesce"
    COMBINE_COLUMNS = "combine_columns"
    SPLIT_COLUMN = "split_column"
    SEMANTIC_REWRITE = "semantic_rewrite"


class RiskFlag(str, Enum):
    SEMANTIC_CONTENT_REWRITE = "semantic_content_rewrite"
    TEMPLATE_BUSINESS_CONTENT = "template_business_content"
    FORMULA_LIKE_TEXT = "formula_like_text"
    CONSTANT_VALUE_INJECTION = "constant_value_injection"
    TYPE_COERCION = "type_coercion"
    MULTI_SOURCE_COMPOSITION = "multi_source_composition"
    LOW_CONFIDENCE = "low_confidence"
    LARGE_CHANGE_SET = "large_change_set"


class ManualReviewReason(str, Enum):
    HUMAN_ACCEPT_REQUIRED = "human_accept_required"
    SEMANTIC_CONTENT_REWRITE = "semantic_content_rewrite"
    TEMPLATE_BUSINESS_CONTENT = "template_business_content"
    FORMULA_LIKE_TEXT = "formula_like_text"
    CONSTANT_VALUE_INJECTION = "constant_value_injection"
    TYPE_COERCION = "type_coercion"
    MULTI_SOURCE_COMPOSITION = "multi_source_composition"
    LOW_CONFIDENCE = "low_confidence"
    LARGE_CHANGE_SET = "large_change_set"


class ArtifactSnapshot(StrictModel):
    artifact_id: UUID
    sha256: Sha256
    owner_sub: OwnerSubject
    status: Literal["ready"] = "ready"
    immutable: Literal[True] = True


class ColumnSnapshot(StrictModel):
    """Server-generated column identity; display headers are deliberately absent."""

    column_id: Identifier
    kind: ColumnKind


class TableSnapshot(StrictModel):
    artifact: ArtifactSnapshot
    projection_implementation_version: Version
    sheet: SheetName
    header_row: int = Field(ge=1, le=100_000)
    data_start_row: int = Field(ge=1, le=100_001)
    columns: tuple[ColumnSnapshot, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_table(self) -> "TableSnapshot":
        if self.data_start_row <= self.header_row:
            raise ValueError("data_start_row must be after header_row")
        column_ids = [column.column_id for column in self.columns]
        if len(column_ids) != len(set(column_ids)):
            raise ValueError("source column_id values must be unique")
        return self


class TemplateSnapshot(StrictModel):
    artifact: ArtifactSnapshot
    template_version: Version
    classification: TemplateClassification
    classifier_proof_version: Version
    target_sheet: SheetName
    columns: tuple[ColumnSnapshot, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_columns(self) -> "TemplateSnapshot":
        column_ids = [column.column_id for column in self.columns]
        if len(column_ids) != len(set(column_ids)):
            raise ValueError("target column_id values must be unique")
        return self


class OperationImplementation(StrictModel):
    operation: Operation
    implementation_version: Version


class RuleSetSnapshot(StrictModel):
    """Immutable human rules and their deterministic implementation versions."""

    rule_set_id: Identifier
    rule_set_version: Version
    rule_set_sha256: Sha256
    policy_implementation_version: Version
    operations: tuple[OperationImplementation, ...] = Field(min_length=1, max_length=256)
    maximum_changes: int = Field(ge=0, le=200)
    semantic_rewrite_limit: int = Field(ge=0, le=100)
    low_confidence_threshold_basis_points: int = Field(ge=0, le=10_000)
    large_change_review_threshold: int = Field(ge=1, le=200)

    @model_validator(mode="after")
    def validate_operations(self) -> "RuleSetSnapshot":
        names = [item.operation for item in self.operations]
        if len(names) != len(set(names)):
            raise ValueError("operation implementations must be unique")
        return self


class SourceRowRef(StrictModel):
    source_sha256: Sha256
    sheet: SheetName
    row_number: int = Field(ge=1, le=100_001)


class CellValue(StrictModel):
    """Canonical scalar; floats are forbidden and decimals/dates are strings."""

    kind: Literal["null", "text", "integer", "boolean", "date", "decimal"]
    value: str | int | bool | None

    @model_validator(mode="after")
    def validate_value(self) -> "CellValue":
        if self.kind == "null":
            if self.value is not None:
                raise ValueError("null cell requires value=None")
            return self
        if self.kind == "integer":
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise ValueError("integer cell requires an int")
            if not -(2**53 - 1) <= self.value <= 2**53 - 1:
                raise ValueError("integer cell exceeds canonical safe-integer range")
            return self
        if self.kind == "boolean":
            if not isinstance(self.value, bool):
                raise ValueError("boolean cell requires a bool")
            return self
        if not isinstance(self.value, str):
            raise ValueError(f"{self.kind} cell requires a string")
        if len(self.value) > 8_192:
            raise ValueError("cell text exceeds 8 KiB character budget")
        if self.kind == "date":
            try:
                parsed = date.fromisoformat(self.value)
            except ValueError as exc:
                raise ValueError("date must be canonical YYYY-MM-DD") from exc
            if parsed.isoformat() != self.value:
                raise ValueError("date must be canonical YYYY-MM-DD")
        if self.kind == "decimal":
            if len(self.value) > 64 or not re.fullmatch(
                r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", self.value
            ):
                raise ValueError("decimal must be a canonical decimal string")
        return self


class FieldChange(StrictModel):
    row_ref: SourceRowRef
    source_column_ids: tuple[Identifier, ...] = Field(max_length=64)
    target_column_id: Identifier
    operation: Operation
    operation_implementation_version: Version
    before: CellValue
    proposed_after: CellValue
    reason_code: Identifier
    confidence_basis_points: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_source_columns(self) -> "FieldChange":
        if len(self.source_column_ids) != len(set(self.source_column_ids)):
            raise ValueError("source_column_ids must be unique")
        return self


class ObservedFieldSnapshot(StrictModel):
    """Authoritative local projection matched against an untrusted proposal."""

    row_ref: SourceRowRef
    source_column_ids: tuple[Identifier, ...] = Field(max_length=64)
    target_column_id: Identifier
    before: CellValue

    @model_validator(mode="after")
    def validate_source_columns(self) -> "ObservedFieldSnapshot":
        if len(self.source_column_ids) != len(set(self.source_column_ids)):
            raise ValueError("source_column_ids must be unique")
        return self


class CleaningChangeProposal(StrictModel):
    schema_version: Literal["workbook-cleaning-change-proposal/v1"] = (
        "workbook-cleaning-change-proposal/v1"
    )
    origin: ProposalOrigin
    source_artifact_id: UUID
    source_sha256: Sha256
    source_projection_implementation_version: Version
    template_artifact_id: UUID
    template_sha256: Sha256
    template_version: Version
    template_classifier_proof_version: Version
    rule_set_id: Identifier
    rule_set_version: Version
    rule_set_sha256: Sha256
    policy_implementation_version: Version
    changes: tuple[FieldChange, ...] = Field(min_length=1, max_length=200)


class CleaningProposalRequest(StrictModel):
    schema_version: Literal["workbook-cleaning-proposal-request/v1"] = (
        "workbook-cleaning-proposal-request/v1"
    )
    mode: Literal["dark"] = "dark"
    task_owner_sub: OwnerSubject
    source: TableSnapshot
    template: TemplateSnapshot
    rules: RuleSetSnapshot
    observed_fields: tuple[ObservedFieldSnapshot, ...] = Field(
        min_length=1, max_length=200
    )
    proposal: CleaningChangeProposal


class SourceEvidenceBinding(StrictModel):
    artifact_id: UUID
    sha256: Sha256
    projection_implementation_version: Version
    sheet: SheetName
    header_row: int
    data_start_row: int


class TemplateEvidenceBinding(StrictModel):
    artifact_id: UUID
    sha256: Sha256
    template_version: Version
    classification: TemplateClassification
    classifier_proof_version: Version
    target_sheet: SheetName


class RulesEvidenceBinding(StrictModel):
    rule_set_id: Identifier
    rule_set_version: Version
    rule_set_sha256: Sha256
    policy_implementation_version: Version
    operations: tuple[OperationImplementation, ...]
    maximum_changes: int
    semantic_rewrite_limit: int
    low_confidence_threshold_basis_points: int
    large_change_review_threshold: int


class FieldDiffEvidence(StrictModel):
    row_ref: SourceRowRef
    source_column_ids: tuple[Identifier, ...]
    target_column_id: Identifier
    operation: Operation
    operation_implementation_version: Version
    before_value_sha256: Sha256
    after_value_sha256: Sha256
    confidence_basis_points: int
    risk_flags: tuple[RiskFlag, ...]
    requires_human_review: Literal[True] = True


class CleaningProposalAssessment(StrictModel):
    schema_version: Literal["workbook-cleaning-proposal-assessment/v1"] = (
        "workbook-cleaning-proposal-assessment/v1"
    )
    assessment_implementation_version: Literal[
        "workbook-cleaning-proposal-kernel/1.0.0"
    ] = "workbook-cleaning-proposal-kernel/1.0.0"
    mode: Literal["dark"] = "dark"
    outcome: Literal["human_review_required"] = "human_review_required"
    executable: Literal[False] = False
    artifact_create_allowed: Literal[False] = False
    business_write_allowed: Literal[False] = False
    owner_subject_sha256: Sha256
    source_binding: SourceEvidenceBinding
    template_binding: TemplateEvidenceBinding
    rules_binding: RulesEvidenceBinding
    proposal_schema_version: Literal["workbook-cleaning-change-proposal/v1"]
    proposal_origin: ProposalOrigin
    change_count: int
    semantic_rewrite_count: int
    field_diffs: tuple[FieldDiffEvidence, ...]
    risk_flags: tuple[RiskFlag, ...]
    manual_review_reasons: tuple[ManualReviewReason, ...]
    proposal_fingerprint: Sha256
    evidence_payload_fingerprint: Sha256
