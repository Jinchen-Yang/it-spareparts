"""Auditable control records for maintenance cost/inventory cutover runs."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, TZDateTime


class MaintenanceMigrationRun(Base):
    """One hash-bound dry-run; approval produces a manifest, never activation."""

    __tablename__ = "maintenance_migration_run"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    preview_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_json: Mapped[dict | None] = mapped_column(JSONB)
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    manifest_key_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    reconciled_by: Mapped[str | None] = mapped_column(String(64))
    reconciled_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('previewed', 'reconciled', 'approved')",
            name="ck_maintenance_migration_run_status",
        ),
        CheckConstraint(
            "char_length(request_fingerprint) = 64 "
            "AND char_length(source_snapshot_hash) = 64 "
            "AND (manifest_hash IS NULL OR char_length(manifest_hash) = 64)",
            name="ck_maintenance_migration_run_hashes",
        ),
        CheckConstraint(
            "char_length(btrim(created_by)) > 0 AND version >= 1",
            name="ck_maintenance_migration_run_identity",
        ),
        CheckConstraint(
            "(status = 'previewed' AND reconciled_by IS NULL AND reconciled_at IS NULL "
            "AND approved_by IS NULL AND approved_at IS NULL "
            "AND manifest_json IS NULL AND manifest_hash IS NULL "
            "AND manifest_key_id IS NULL) OR "
            "(status = 'reconciled' AND reconciled_by IS NOT NULL "
            "AND reconciled_at IS NOT NULL AND approved_by IS NULL "
            "AND approved_at IS NULL AND manifest_json IS NULL "
            "AND manifest_hash IS NULL AND manifest_key_id IS NULL) OR "
            "(status = 'approved' AND reconciled_by IS NOT NULL "
            "AND reconciled_at IS NOT NULL AND approved_by IS NOT NULL "
            "AND approved_at IS NOT NULL AND manifest_json IS NOT NULL "
            "AND manifest_hash IS NOT NULL AND manifest_key_id IS NOT NULL)",
            name="ck_maintenance_migration_run_state_evidence",
        ),
        CheckConstraint(
            "approved_by IS NULL OR "
            "(approved_by <> created_by AND approved_by <> reconciled_by)",
            name="ck_maintenance_migration_run_independent_approval",
        ),
        Index("ix_maintenance_migration_run_status_time", "status", "created_at"),
    )


class MaintenanceProjectCutoverPlan(Base):
    """Per-project totals and cutoff bound to one migration run."""

    __tablename__ = "maintenance_project_cutover_plan"

    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_migration_run.run_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    cutover_date: Mapped[date] = mapped_column(Date, nullable=False)
    historical_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    source_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    historical_cost_ex_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    historical_cost_inc_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    post_cutover_cost_ex_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    post_cutover_cost_inc_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    approved_expense_ex_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    approved_expense_inc_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total_cost_ex_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total_cost_inc_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    blocker_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reconciled_by: Mapped[str | None] = mapped_column(String(64))
    reconciled_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    reconciliation_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "historical_mode IN ('approved_cost_baseline', 'stable_site_issues')",
            name="ck_maintenance_project_cutover_historical_mode",
        ),
        CheckConstraint(
            "status IN ('previewed', 'reconciled', 'approved')",
            name="ck_maintenance_project_cutover_status",
        ),
        CheckConstraint(
            "char_length(source_snapshot_hash) = 64 "
            "AND char_length(input_fingerprint) = 64",
            name="ck_maintenance_project_cutover_hashes",
        ),
        CheckConstraint(
            "historical_cost_ex_tax >= 0 AND historical_cost_inc_tax >= 0 "
            "AND post_cutover_cost_ex_tax >= 0 AND post_cutover_cost_inc_tax >= 0 "
            "AND approved_expense_ex_tax >= 0 AND approved_expense_inc_tax >= 0 "
            "AND total_cost_ex_tax >= 0 AND total_cost_inc_tax >= 0",
            name="ck_maintenance_project_cutover_amounts",
        ),
        CheckConstraint(
            "blocker_count >= 0 AND version >= 1",
            name="ck_maintenance_project_cutover_counts",
        ),
        CheckConstraint(
            "(status = 'previewed' AND reconciled_by IS NULL "
            "AND reconciled_at IS NULL AND reconciliation_reason IS NULL) OR "
            "(status IN ('reconciled', 'approved') "
            "AND reconciled_by IS NOT NULL AND reconciled_at IS NOT NULL "
            "AND char_length(btrim(reconciliation_reason)) > 0)",
            name="ck_maintenance_project_cutover_reconciliation",
        ),
        UniqueConstraint(
            "run_id", "project_id", name="uq_maintenance_project_cutover_run_project"
        ),
        Index(
            "ix_maintenance_project_cutover_project_time",
            "project_id",
            "created_at",
        ),
    )


class MaintenanceHistoricalCostBaseline(Base):
    """Candidate historical cost baseline requiring named reconciliation."""

    __tablename__ = "maintenance_historical_cost_baseline"

    baseline_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project_cutover_plan.plan_id"),
        nullable=False,
        unique=True,
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    amount_ex_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    amount_inc_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_state: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    approval_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "amount_ex_tax >= 0 AND amount_inc_tax >= 0",
            name="ck_maintenance_historical_baseline_amounts",
        ),
        CheckConstraint(
            "char_length(evidence_hash) = 64 AND version >= 1",
            name="ck_maintenance_historical_baseline_identity",
        ),
        CheckConstraint(
            "(approval_state = 'pending' AND approved_by IS NULL "
            "AND approved_at IS NULL AND approval_reason IS NULL) OR "
            "(approval_state = 'approved' AND approved_by IS NOT NULL "
            "AND approved_at IS NOT NULL "
            "AND char_length(btrim(approval_reason)) > 0)",
            name="ck_maintenance_historical_baseline_approval",
        ),
    )


class MaintenanceInventoryOpeningBalance(Base):
    """Frozen inventory quantity at the project cutover boundary."""

    __tablename__ = "maintenance_inventory_opening_balance"

    opening_balance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project_cutover_plan.plan_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    balance_key: Mapped[str] = mapped_column(String(256), nullable=False)
    pn: Mapped[str | None] = mapped_column(String(256))
    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_state: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    approval_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "quantity >= 0 AND quantity < 1000000000000",
            name="ck_maintenance_inventory_opening_quantity",
        ),
        CheckConstraint(
            "char_length(evidence_hash) = 64 AND version >= 1",
            name="ck_maintenance_inventory_opening_identity",
        ),
        CheckConstraint(
            "(approval_state = 'pending' AND approved_by IS NULL "
            "AND approved_at IS NULL AND approval_reason IS NULL) OR "
            "(approval_state = 'approved' AND approved_by IS NOT NULL "
            "AND approved_at IS NOT NULL "
            "AND char_length(btrim(approval_reason)) > 0)",
            name="ck_maintenance_inventory_opening_approval",
        ),
        UniqueConstraint(
            "plan_id", "balance_key", name="uq_maintenance_inventory_opening_plan_key"
        ),
        Index(
            "ix_maintenance_inventory_opening_project",
            "project_id",
            "balance_key",
        ),
    )


class MaintenanceMigrationDiscrepancy(Base):
    """Stable project discrepancy; blockers cannot be waived by free text."""

    __tablename__ = "maintenance_migration_discrepancy"

    discrepancy_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_migration_run.run_id"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project_cutover_plan.plan_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "severity IN ('blocker', 'warning')",
            name="ck_maintenance_migration_discrepancy_severity",
        ),
        CheckConstraint(
            "(status = 'open' AND resolved_by IS NULL AND resolved_at IS NULL "
            "AND resolution_reason IS NULL) OR "
            "(status = 'resolved' AND resolved_by IS NOT NULL "
            "AND resolved_at IS NOT NULL "
            "AND char_length(btrim(resolution_reason)) > 0)",
            name="ck_maintenance_migration_discrepancy_resolution",
        ),
        CheckConstraint(
            "char_length(stable_key) = 64 AND version >= 1",
            name="ck_maintenance_migration_discrepancy_identity",
        ),
        UniqueConstraint(
            "plan_id", "stable_key", name="uq_maintenance_migration_discrepancy_key"
        ),
        Index(
            "ix_maintenance_migration_discrepancy_run_status",
            "run_id",
            "status",
            "severity",
        ),
    )


class MaintenanceMigrationEvent(Base):
    """Append-only named state/approval event."""

    __tablename__ = "maintenance_migration_event"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_migration_run.run_id"), nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_project.project_id")
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    operated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('preview', 'reconcile', 'approve')",
            name="ck_maintenance_migration_event_action",
        ),
        CheckConstraint(
            "to_status IN ('previewed', 'reconciled', 'approved') "
            "AND (from_status IS NULL OR "
            "from_status IN ('previewed', 'reconciled'))",
            name="ck_maintenance_migration_event_status",
        ),
        CheckConstraint(
            "char_length(btrim(operation_key)) > 0 "
            "AND char_length(btrim(reason)) > 0 "
            "AND char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_migration_event_identity",
        ),
        Index(
            "ix_maintenance_migration_event_run_time",
            "run_id",
            "operated_at",
            "event_id",
        ),
    )
