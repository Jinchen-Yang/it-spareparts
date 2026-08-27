"""Append-only audit ledger for guarded contract-amount remediation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class MaintenanceContractAmountRemediationRun(Base):
    """One immutable apply or rollback run for contract-amount repair."""

    __tablename__ = "maintenance_contract_amount_remediation_run"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    database_principal: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    source_backup_sha256: Mapped[str | None] = mapped_column(String(64))
    source_algorithm_sha256: Mapped[str | None] = mapped_column(String(64))
    f7_affected_set_sha256: Mapped[str | None] = mapped_column(String(64))
    preserved_set_sha256: Mapped[str | None] = mapped_column(String(64))
    changed_set_sha256: Mapped[str | None] = mapped_column(String(64))
    f7_affected_count: Mapped[int | None] = mapped_column(Integer)
    preserved_count: Mapped[int | None] = mapped_column(Integer)
    authoritative_corrected_count: Mapped[int | None] = mapped_column(Integer)
    cleared_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_run_id"],
            ["maintenance_contract_amount_remediation_run.run_id"],
            name="fk_maint_contract_remediation_source_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "manifest_sha256",
            name="uq_maint_contract_remediation_manifest",
        ),
        CheckConstraint(
            "mode IN ('apply', 'rollback')",
            name="ck_maint_contract_remediation_mode",
        ),
        CheckConstraint(
            "row_count >= 1",
            name="ck_maint_contract_remediation_row_count",
        ),
        CheckConstraint(
            "(mode = 'apply' AND source_snapshot_sha256 IS NOT NULL "
            "AND source_backup_sha256 IS NOT NULL "
            "AND source_algorithm_sha256 IS NOT NULL "
            "AND f7_affected_set_sha256 IS NOT NULL "
            "AND preserved_set_sha256 IS NOT NULL "
            "AND changed_set_sha256 IS NOT NULL "
            "AND f7_affected_count IS NOT NULL AND preserved_count IS NOT NULL "
            "AND authoritative_corrected_count IS NOT NULL "
            "AND cleared_count IS NOT NULL "
            "AND f7_affected_count >= 1 AND preserved_count >= 0 "
            "AND authoritative_corrected_count >= 0 AND cleared_count >= 0 "
            "AND f7_affected_count = preserved_count "
            "+ authoritative_corrected_count + cleared_count "
            "AND row_count = authoritative_corrected_count + cleared_count) OR "
            "(mode = 'rollback' AND source_snapshot_sha256 IS NULL "
            "AND source_backup_sha256 IS NULL AND source_algorithm_sha256 IS NULL "
            "AND f7_affected_set_sha256 IS NULL "
            "AND preserved_set_sha256 IS NULL AND changed_set_sha256 IS NULL "
            "AND f7_affected_count IS NULL "
            "AND preserved_count IS NULL "
            "AND authoritative_corrected_count IS NULL AND cleared_count IS NULL)",
            name="ck_maint_contract_remediation_partition",
        ),
        CheckConstraint(
            "source_snapshot_sha256 IS NULL OR "
            "source_snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_maint_contract_remediation_source_snapshot_sha",
        ),
        CheckConstraint(
            "source_backup_sha256 IS NULL OR "
            "source_backup_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_maint_contract_remediation_source_backup_sha",
        ),
        CheckConstraint(
            "(source_algorithm_sha256 IS NULL OR "
            "source_algorithm_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(f7_affected_set_sha256 IS NULL OR "
            "f7_affected_set_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(preserved_set_sha256 IS NULL OR "
            "preserved_set_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(changed_set_sha256 IS NULL OR "
            "changed_set_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_maint_contract_remediation_partition_shas",
        ),
        CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_maint_contract_remediation_manifest_sha",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maint_contract_remediation_reason",
        ),
        CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maint_contract_remediation_operator",
        ),
        CheckConstraint(
            "char_length(btrim(database_principal)) > 0",
            name="ck_maint_contract_remediation_db_principal",
        ),
        CheckConstraint(
            "(mode = 'rollback') = (source_run_id IS NOT NULL)",
            name="ck_maint_contract_remediation_source_mode",
        ),
    )


class MaintenanceContractAmountRemediationEntry(Base):
    """Immutable before/after evidence for one repaired project contract."""

    __tablename__ = "maintenance_contract_amount_remediation_entry"

    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_contract_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    contract_no: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_version: Mapped[int] = mapped_column(Integer, nullable=False)
    after_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_amount_inc_tax: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    after_amount_inc_tax: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    target_state: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
    )
    applied_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            ["maintenance_contract_amount_remediation_run.run_id"],
            name="fk_maint_contract_remediation_entry_run",
            ondelete="RESTRICT",
        ),
        PrimaryKeyConstraint(
            "run_id",
            "project_contract_id",
            name="pk_maint_contract_remediation_entry",
        ),
        CheckConstraint(
            "before_version >= 1 AND expected_version = before_version AND "
            "after_version = before_version + 1",
            name="ck_maint_contract_remediation_versions",
        ),
        CheckConstraint(
            "(before_amount_inc_tax IS NULL OR "
            "(before_amount_inc_tax >= 0 AND "
            "before_amount_inc_tax < 1000000000000)) AND "
            "(after_amount_inc_tax IS NULL OR "
            "(after_amount_inc_tax >= 0 AND "
            "after_amount_inc_tax < 1000000000000))",
            name="ck_maint_contract_remediation_amount_range",
        ),
        CheckConstraint(
            "target_state IN ('authoritative', 'incomplete', 'restored')",
            name="ck_maint_contract_remediation_state",
        ),
        CheckConstraint(
            "(target_state = 'authoritative' AND after_amount_inc_tax IS NOT NULL) "
            "OR (target_state = 'incomplete' AND after_amount_inc_tax IS NULL) "
            "OR target_state = 'restored'",
            name="ck_maint_contract_remediation_amount_state",
        ),
        CheckConstraint(
            "evidence_kind IN ("
            "'ledger_amount_inc_tax', 'sales_explicit_tax', "
            "'no_authoritative_evidence', 'rollback_receipt')",
            name="ck_maint_contract_remediation_evidence",
        ),
        CheckConstraint(
            "(evidence_kind IN ('ledger_amount_inc_tax', 'sales_explicit_tax') "
            "AND target_state = 'authoritative') OR "
            "(evidence_kind = 'no_authoritative_evidence' "
            "AND target_state = 'incomplete') OR "
            "(evidence_kind = 'rollback_receipt' "
            "AND target_state = 'restored')",
            name="ck_maint_contract_remediation_evidence_state",
        ),
        CheckConstraint(
            "char_length(btrim(evidence_ref)) > 0",
            name="ck_maint_contract_remediation_evidence_ref",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_snapshot) = 'object'",
            name="ck_maint_contract_remediation_snapshot_object",
        ),
        Index(
            "ix_maint_contract_remediation_entry_project",
            "project_id",
            "applied_at",
        ),
    )
