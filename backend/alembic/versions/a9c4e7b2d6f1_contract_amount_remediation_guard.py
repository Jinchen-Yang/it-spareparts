"""add guarded audit ledger for contract amount remediation

Revision ID: a9c4e7b2d6f1
Revises: f7a3d2c8e6b1

This migration deliberately does not rewrite ``amount_inc_tax``.  Revision
f7a3d2c8e6b1 was already applied in production without recording which rows it
changed.  A later automatic UPDATE cannot distinguish its guessed values from
legitimate ledger or manual writes and would therefore risk destroying newer
facts.

The companion ``scripts/remediate_contract_amount_inc_tax.py`` performs an
explicit, compare-and-swap repair and records every applied before/after image
in the append-only tables created here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a9c4e7b2d6f1"
down_revision: str | None = "f7a3d2c8e6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "maintenance_contract_amount_remediation_run",
        sa.Column("run_id", sa.String(length=36), primary_key=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source_run_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operated_by", sa.String(length=64), nullable=False),
        # Keep the database execution identity separate from the named
        # application operator so a shell argument cannot be the only audit
        # attribution.
        sa.Column("database_principal", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        # Apply runs are cryptographically bound to an extract produced from
        # the restored pre-f7 backup.  Rollback runs instead bind to source_run_id.
        sa.Column("source_snapshot_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_backup_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_algorithm_sha256", sa.String(length=64), nullable=True),
        sa.Column("f7_affected_set_sha256", sa.String(length=64), nullable=True),
        sa.Column("preserved_set_sha256", sa.String(length=64), nullable=True),
        sa.Column("changed_set_sha256", sa.String(length=64), nullable=True),
        sa.Column("f7_affected_count", sa.Integer(), nullable=True),
        sa.Column("preserved_count", sa.Integer(), nullable=True),
        sa.Column("authoritative_corrected_count", sa.Integer(), nullable=True),
        sa.Column("cleared_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["maintenance_contract_amount_remediation_run.run_id"],
            name="fk_maint_contract_remediation_source_run",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "manifest_sha256",
            name="uq_maint_contract_remediation_manifest",
        ),
        sa.CheckConstraint(
            "mode IN ('apply', 'rollback')",
            name="ck_maint_contract_remediation_mode",
        ),
        sa.CheckConstraint(
            "row_count >= 1",
            name="ck_maint_contract_remediation_row_count",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "source_snapshot_sha256 IS NULL OR "
            "source_snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_maint_contract_remediation_source_snapshot_sha",
        ),
        sa.CheckConstraint(
            "source_backup_sha256 IS NULL OR "
            "source_backup_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_maint_contract_remediation_source_backup_sha",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_maint_contract_remediation_manifest_sha",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maint_contract_remediation_reason",
        ),
        sa.CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maint_contract_remediation_operator",
        ),
        sa.CheckConstraint(
            "char_length(btrim(database_principal)) > 0",
            name="ck_maint_contract_remediation_db_principal",
        ),
        sa.CheckConstraint(
            "(mode = 'rollback') = (source_run_id IS NOT NULL)",
            name="ck_maint_contract_remediation_source_mode",
        ),
    )
    op.create_table(
        "maintenance_contract_amount_remediation_entry",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        # No FK to the mutable business row: remediation history must survive a
        # later archival/deletion workflow and remain independently auditable.
        sa.Column("project_contract_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("contract_no", sa.String(length=64), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("before_version", sa.Integer(), nullable=False),
        sa.Column("after_version", sa.Integer(), nullable=False),
        sa.Column("before_amount_inc_tax", sa.Numeric(14, 2), nullable=True),
        sa.Column("after_amount_inc_tax", sa.Numeric(14, 2), nullable=True),
        sa.Column("target_state", sa.String(length=16), nullable=False),
        sa.Column("evidence_kind", sa.String(length=40), nullable=False),
        sa.Column("evidence_ref", sa.String(length=128), nullable=False),
        sa.Column(
            "evidence_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["maintenance_contract_amount_remediation_run.run_id"],
            name="fk_maint_contract_remediation_entry_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "project_contract_id",
            name="pk_maint_contract_remediation_entry",
        ),
        sa.CheckConstraint(
            "before_version >= 1 AND expected_version = before_version AND "
            "after_version = before_version + 1",
            name="ck_maint_contract_remediation_versions",
        ),
        sa.CheckConstraint(
            "(before_amount_inc_tax IS NULL OR "
            "(before_amount_inc_tax >= 0 AND "
            "before_amount_inc_tax < 1000000000000)) AND "
            "(after_amount_inc_tax IS NULL OR "
            "(after_amount_inc_tax >= 0 AND "
            "after_amount_inc_tax < 1000000000000))",
            name="ck_maint_contract_remediation_amount_range",
        ),
        sa.CheckConstraint(
            "target_state IN ('authoritative', 'incomplete', 'restored')",
            name="ck_maint_contract_remediation_state",
        ),
        sa.CheckConstraint(
            "(target_state = 'authoritative' AND after_amount_inc_tax IS NOT NULL) "
            "OR (target_state = 'incomplete' AND after_amount_inc_tax IS NULL) "
            "OR target_state = 'restored'",
            name="ck_maint_contract_remediation_amount_state",
        ),
        sa.CheckConstraint(
            "evidence_kind IN ("
            "'ledger_amount_inc_tax', 'sales_explicit_tax', "
            "'no_authoritative_evidence', 'rollback_receipt')",
            name="ck_maint_contract_remediation_evidence",
        ),
        sa.CheckConstraint(
            "(evidence_kind IN ('ledger_amount_inc_tax', 'sales_explicit_tax') "
            "AND target_state = 'authoritative') OR "
            "(evidence_kind = 'no_authoritative_evidence' "
            "AND target_state = 'incomplete') OR "
            "(evidence_kind = 'rollback_receipt' "
            "AND target_state = 'restored')",
            name="ck_maint_contract_remediation_evidence_state",
        ),
        sa.CheckConstraint(
            "char_length(btrim(evidence_ref)) > 0",
            name="ck_maint_contract_remediation_evidence_ref",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_snapshot) = 'object'",
            name="ck_maint_contract_remediation_snapshot_object",
        ),
    )
    op.create_index(
        "ix_maint_contract_remediation_entry_project",
        "maintenance_contract_amount_remediation_entry",
        ["project_id", "applied_at"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_contract_amount_remediation_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table_name in (
        "maintenance_contract_amount_remediation_run",
        "maintenance_contract_amount_remediation_entry",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_append_only
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION reject_contract_amount_remediation_mutation()
            """
        )


def downgrade() -> None:
    # Once a repair has been executed these rows are the rollback evidence.
    # Refuse a downgrade that would silently erase them; operators must retain
    # an exported receipt and explicitly archive the ledger before any schema
    # removal is considered.
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Close the check-then-drop race with a concurrent remediation execute.
    # An in-flight writer finishes before this lock is granted; any later
    # writer waits and then fails safely after the schema is removed.
    op.execute(
        "LOCK TABLE maintenance_contract_amount_remediation_run, "
        "maintenance_contract_amount_remediation_entry "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM maintenance_contract_amount_remediation_run
            ) THEN
                RAISE EXCEPTION
                    'contract amount remediation audit exists; downgrade refused';
            END IF;
        END;
        $$
        """
    )
    for table_name in (
        "maintenance_contract_amount_remediation_entry",
        "maintenance_contract_amount_remediation_run",
    ):
        op.execute(
            f"DROP TRIGGER trg_{table_name}_append_only ON {table_name}"
        )
    op.execute("DROP FUNCTION reject_contract_amount_remediation_mutation()")
    op.drop_index(
        "ix_maint_contract_remediation_entry_project",
        table_name="maintenance_contract_amount_remediation_entry",
    )
    op.drop_table("maintenance_contract_amount_remediation_entry")
    op.drop_table("maintenance_contract_amount_remediation_run")
