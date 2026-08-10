"""Strict contracts for the dark workbook-cleaning proposal kernel.

Evidence intentionally contains only upstream-issued opaque UUID references and
bounded metadata. It contains no sheet names, owners, cell values, value hashes,
or home-grown integrity claims.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


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


class StrictModel(BaseModel):
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
    """Authoritative internal input; its hash is never projected to Evidence."""

    artifact_id: UUID
    sha256: Sha256
    owner_sub: OwnerSubject
    status: Literal["ready"] = "ready"
    immutable: Literal[True] = True


class ColumnSnapshot(StrictModel):
    """Server-issued opaque identity; display headers stay in the adapter."""

    column_ref: UUID
    kind: ColumnKind


class TableSnapshot(StrictModel):
    source_snapshot_ref: UUID
    artifact: ArtifactSnapshot
    projection_implementation_version: Version
    sheet_ref: UUID
    header_row: int = Field(ge=1, le=100_000)
    data_start_row: int = Field(ge=1, le=100_001)
    columns: tuple[ColumnSnapshot, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_table(self) -> "TableSnapshot":
        if self.data_start_row <= self.header_row:
            raise ValueError("data_start_row must be after header_row")
        refs = [column.column_ref for column in self.columns]
        if len(refs) != len(set(refs)):
            raise ValueError("source column_ref values must be unique")
        return self


class TemplateSnapshot(StrictModel):
    template_snapshot_ref: UUID
    artifact: ArtifactSnapshot
    template_version: Version
    classification: TemplateClassification
    classifier_proof_version: Version
    target_sheet_ref: UUID
    columns: tuple[ColumnSnapshot, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_columns(self) -> "TemplateSnapshot":
        refs = [column.column_ref for column in self.columns]
        if len(refs) != len(set(refs)):
            raise ValueError("target column_ref values must be unique")
        return self


class OperationImplementation(StrictModel):
    operation: Operation
    implementation_version: Version


class RuleSetSnapshot(StrictModel):
    rule_snapshot_ref: UUID
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
    source_snapshot_ref: UUID
    sheet_ref: UUID
    row_number: int = Field(ge=1, le=100_001)


class CellValue(StrictModel):
    """Canonical scalar with UTF-8 byte budgets; floats are forbidden."""

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
        if len(self.value.encode("utf-8")) > 8_192:
            raise ValueError("cell text exceeds 8192 UTF-8 byte budget")
        if self.kind == "date":
            try:
                parsed = date.fromisoformat(self.value)
            except ValueError as exc:
                raise ValueError("date must be canonical YYYY-MM-DD") from exc
            if parsed.isoformat() != self.value:
                raise ValueError("date must be canonical YYYY-MM-DD")
        if self.kind == "decimal" and (
            len(self.value.encode("utf-8")) > 64
            or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", self.value)
        ):
            raise ValueError("decimal must be a canonical decimal string")
        return self


class ObservedFieldSnapshot(StrictModel):
    """Local authoritative value projected under an immutable opaque reference."""

    observed_field_ref: UUID
    row_ref: SourceRowRef
    source_column_refs: tuple[UUID, ...] = Field(max_length=64)
    target_column_ref: UUID
    before: CellValue

    @model_validator(mode="after")
    def validate_source_columns(self) -> "ObservedFieldSnapshot":
        if len(self.source_column_refs) != len(set(self.source_column_refs)):
            raise ValueError("source_column_refs must be unique")
        return self


class FieldChange(StrictModel):
    """Untrusted proposal joined to server-issued field/value references."""

    observed_field_ref: UUID
    proposed_value_ref: UUID
    row_ref: SourceRowRef
    source_column_refs: tuple[UUID, ...] = Field(max_length=64)
    target_column_ref: UUID
    operation: Operation
    operation_implementation_version: Version
    proposed_after: CellValue
    reason_code: Identifier
    confidence_basis_points: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_source_columns(self) -> "FieldChange":
        if len(self.source_column_refs) != len(set(self.source_column_refs)):
            raise ValueError("source_column_refs must be unique")
        return self


class CleaningChangeProposal(StrictModel):
    schema_version: Literal["workbook-cleaning-change-proposal/v1"] = (
        "workbook-cleaning-change-proposal/v1"
    )
    proposal_ref: UUID
    origin: ProposalOrigin
    source_snapshot_ref: UUID
    source_artifact_id: UUID
    source_sha256: Sha256
    source_projection_implementation_version: Version
    template_snapshot_ref: UUID
    template_artifact_id: UUID
    template_sha256: Sha256
    template_version: Version
    template_classifier_proof_version: Version
    rule_snapshot_ref: UUID
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
    source_snapshot_ref: UUID
    artifact_id: UUID
    projection_implementation_version: Version
    sheet_ref: UUID
    header_row: int
    data_start_row: int
    ordered_column_refs: tuple[UUID, ...]


class TemplateEvidenceBinding(StrictModel):
    template_snapshot_ref: UUID
    artifact_id: UUID
    template_version: Version
    classification: TemplateClassification
    classifier_proof_version: Version
    target_sheet_ref: UUID
    ordered_column_refs: tuple[UUID, ...]


class RulesEvidenceBinding(StrictModel):
    rule_snapshot_ref: UUID
    rule_set_version: Version
    policy_implementation_version: Version
    operations: tuple[OperationImplementation, ...]
    maximum_changes: int
    semantic_rewrite_limit: int
    low_confidence_threshold_basis_points: int
    large_change_review_threshold: int


class FieldDiffEvidence(StrictModel):
    observed_field_ref: UUID
    proposed_value_ref: UUID
    row_ref: SourceRowRef
    source_column_refs: tuple[UUID, ...]
    target_column_ref: UUID
    operation: Operation
    operation_implementation_version: Version
    confidence_basis_points: int
    risk_flags: tuple[RiskFlag, ...]
    requires_human_review: Literal[True] = True


class CleaningProposalAssessment(StrictModel):
    schema_version: Literal["workbook-cleaning-proposal-assessment/v1"] = (
        "workbook-cleaning-proposal-assessment/v1"
    )
    assessment_implementation_version: Literal[
        "workbook-cleaning-proposal-kernel/1.1.0"
    ] = "workbook-cleaning-proposal-kernel/1.1.0"
    mode: Literal["dark"] = "dark"
    outcome: Literal["human_review_required"] = "human_review_required"
    executable: Literal[False] = False
    artifact_create_allowed: Literal[False] = False
    business_write_allowed: Literal[False] = False
    proposal_ref: UUID
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
