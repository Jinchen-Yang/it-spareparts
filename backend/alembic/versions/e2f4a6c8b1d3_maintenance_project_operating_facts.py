"""maintenance project operating facts

Revision ID: e2f4a6c8b1d3
Revises: d8a3c7e4f2b1
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e2f4a6c8b1d3"
down_revision: str | None = "d8a3c7e4f2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FACT_TABLES = (
    "maintenance_collection_snapshot",
    "maintenance_site_issue",
    "maintenance_site_issue_line",
    "maintenance_project_expense_attribution",
    "maintenance_project_operation_audit",
    "maintenance_project_workbook_state",
    "maintenance_project_workbook_operation",
    "maintenance_project_workbook_validation",
)


def upgrade() -> None:
    op.create_table(
        "maintenance_project_operation_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("before_json", postgresql.JSONB(), nullable=True),
        sa.Column("after_json", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operated_by", sa.String(64), nullable=False),
        sa.Column("operated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maintenance_project_operation_audit_reason",
        ),
        sa.CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_project_operation_audit_operator",
        ),
    )
    op.create_index(
        "ix_maintenance_project_operation_audit_project_time",
        "maintenance_project_operation_audit",
        ["project_id", "operated_at", "id"],
    )
    op.create_table(
        "maintenance_collection_snapshot",
        sa.Column("collection_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("project_contract_id", sa.String(36), nullable=False),
        sa.Column("report_month", sa.Date(), nullable=False),
        sa.Column("cumulative_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("receipt_reference", sa.String(128), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(
            ["project_contract_id"],
            ["maintenance_project_contract.project_contract_id"],
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'unconfirmed', 'void')",
            name="ck_maintenance_collection_status",
        ),
        sa.CheckConstraint(
            "cumulative_amount >= 0 AND cumulative_amount < 1000000000000",
            name="ck_maintenance_collection_amount",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_collection_version"),
        sa.CheckConstraint(
            "report_month = date_trunc('month', report_month)::date",
            name="ck_maintenance_collection_month_start",
        ),
        sa.UniqueConstraint(
            "project_contract_id",
            "report_month",
            name="uq_maintenance_collection_contract_month",
        ),
    )
    op.create_index(
        "ix_maintenance_collection_project_month",
        "maintenance_collection_snapshot",
        ["project_id", "report_month"],
    )
    op.create_table(
        "maintenance_site_issue",
        sa.Column("issue_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("issue_no", sa.String(64), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("raw_status", sa.String(64), nullable=False),
        sa.Column("status_mapping_state", sa.String(16), nullable=False),
        sa.Column("normalized_status", sa.String(16), nullable=False),
        sa.Column("status_mapping_version", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint(
            "status_mapping_state IN ('mapped', 'unmapped')",
            name="ck_maintenance_site_issue_mapping_state",
        ),
        sa.CheckConstraint(
            "normalized_status IN ('confirmed', 'void', 'unknown')",
            name="ck_maintenance_site_issue_normalized_status",
        ),
        sa.CheckConstraint(
            "status_mapping_state = 'mapped' OR normalized_status = 'unknown'",
            name="ck_maintenance_site_issue_unmapped_unknown",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_site_issue_version"),
        sa.UniqueConstraint("project_id", "issue_no", name="uq_maintenance_site_issue_project_no"),
    )
    op.create_index(
        "ix_maintenance_site_issue_project_date",
        "maintenance_site_issue",
        ["project_id", "issue_date"],
    )
    op.create_table(
        "maintenance_site_issue_line",
        sa.Column("issue_line_id", sa.String(64), primary_key=True),
        sa.Column("issue_id", sa.String(36), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn", sa.String(128), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("linked_purchase_line_id", sa.Integer(), nullable=True),
        sa.Column("manual_unit_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("manual_evidence", sa.Text(), nullable=True),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("cost_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("cost_source", sa.String(24), nullable=True),
        sa.Column("price_basis", sa.String(16), server_default="ex_tax", nullable=False),
        sa.Column("reference_side", sa.String(16), nullable=True),
        sa.Column("reference_sample_ids", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("reference_sample_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reference_samples", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("reference_window_from", sa.Date(), nullable=True),
        sa.Column("reference_window_to", sa.Date(), nullable=True),
        sa.Column("algorithm_version", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["maintenance_site_issue.issue_id"]),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["linked_purchase_line_id"], ["f_purchase_line.id"]),
        sa.CheckConstraint(
            "quantity > 0 AND quantity < 1000000000000",
            name="ck_maintenance_site_issue_line_quantity",
        ),
        sa.CheckConstraint(
            "manual_unit_cost IS NULL OR (manual_unit_cost >= 0 AND manual_unit_cost < 1000000000000)",
            name="ck_maintenance_site_issue_line_manual_cost",
        ),
        sa.CheckConstraint(
            "unit_cost IS NULL OR (unit_cost >= 0 AND unit_cost < 1000000000000)",
            name="ck_maintenance_site_issue_line_unit_cost",
        ),
        sa.CheckConstraint(
            "cost_source IS NULL OR cost_source IN ('direct_purchase', 'purchase_window', 'sales_window', 'manual')",
            name="ck_maintenance_site_issue_line_cost_source",
        ),
        sa.CheckConstraint(
            "(manual_unit_cost IS NULL) = (manual_evidence IS NULL)",
            name="ck_maintenance_site_issue_line_manual_evidence_pair",
        ),
        sa.CheckConstraint(
            "(cost_source IS NULL AND unit_cost IS NULL AND cost_amount IS NULL) OR "
            "(cost_source IS NOT NULL AND unit_cost IS NOT NULL AND cost_amount IS NOT NULL)",
            name="ck_maintenance_site_issue_line_cost_result_pair",
        ),
        sa.CheckConstraint(
            "price_basis = 'ex_tax'",
            name="ck_maintenance_site_issue_line_price_basis",
        ),
        sa.CheckConstraint(
            "reference_sample_count = jsonb_array_length(reference_samples)",
            name="ck_maintenance_site_issue_line_sample_evidence_count",
        ),
        sa.CheckConstraint(
            "char_length(btrim(algorithm_version)) > 0",
            name="ck_maintenance_site_issue_line_algorithm_version",
        ),
        sa.CheckConstraint("reference_sample_count >= 0", name="ck_maintenance_site_issue_line_sample_count"),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_site_issue_line_version"),
        sa.UniqueConstraint("issue_id", "line_no", name="uq_maintenance_site_issue_line_no"),
    )
    op.create_index(
        "ix_maintenance_site_issue_line_issue",
        "maintenance_site_issue_line",
        ["issue_id", "line_no"],
    )
    op.create_index(
        "ix_maintenance_site_issue_line_part",
        "maintenance_site_issue_line",
        ["part_id"],
    )
    op.create_table(
        "maintenance_project_expense_attribution",
        sa.Column("expense_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("project_contract_id", sa.String(36), nullable=True),
        sa.Column("expense_ref", sa.String(128), nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("applicant", sa.String(64), nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("expense_reason", sa.Text(), nullable=True),
        sa.Column("amount_ex_tax", sa.Numeric(14, 2), nullable=False),
        sa.Column("raw_status", sa.String(64), nullable=False),
        sa.Column("status_mapping_state", sa.String(16), nullable=False),
        sa.Column("normalized_status", sa.String(16), nullable=False),
        sa.Column("status_mapping_version", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(
            ["project_contract_id"],
            ["maintenance_project_contract.project_contract_id"],
        ),
        sa.CheckConstraint(
            "status_mapping_state IN ('mapped', 'unmapped')",
            name="ck_maintenance_project_expense_mapping_state",
        ),
        sa.CheckConstraint(
            "normalized_status IN ('approved', 'rejected', 'void', 'unknown')",
            name="ck_maintenance_project_expense_status",
        ),
        sa.CheckConstraint(
            "status_mapping_state = 'mapped' OR normalized_status = 'unknown'",
            name="ck_maintenance_project_expense_unmapped_unknown",
        ),
        sa.CheckConstraint(
            "amount_ex_tax >= 0 AND amount_ex_tax < 1000000000000",
            name="ck_maintenance_project_expense_amount",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_project_expense_version"),
        sa.UniqueConstraint(
            "project_id",
            "expense_ref",
            name="uq_maintenance_project_expense_ref",
        ),
    )
    op.create_index(
        "ix_maintenance_project_expense_project_date",
        "maintenance_project_expense_attribution",
        ["project_id", "expense_date"],
    )
    op.create_table(
        "maintenance_project_workbook_state",
        sa.Column("project_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_export_id", sa.String(64), nullable=True),
        sa.Column("last_exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_version", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_maintenance_project_workbook_state_revision",
        ),
    )
    op.create_table(
        "maintenance_project_workbook_operation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("export_id", sa.String(64), nullable=True),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("operation_type", sa.String(24), nullable=False),
        sa.Column("operated_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint(
            "operation_type IN ('collection_create', 'file_export', 'file_apply')",
            name="ck_maintenance_project_workbook_operation_type",
        ),
        sa.UniqueConstraint("operation_key", name="uq_maintenance_project_workbook_operation_key"),
    )
    op.create_index(
        "ix_maintenance_project_workbook_operation_project_file",
        "maintenance_project_workbook_operation",
        ["project_id", "file_sha256"],
    )
    op.create_table(
        "maintenance_project_workbook_validation",
        sa.Column("validation_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("export_id", sa.String(64), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("plan_json", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("issues_json", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("error_workbook", sa.LargeBinary(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint(
            "expected_revision >= 0",
            name="ck_maintenance_project_workbook_validation_revision",
        ),
        sa.CheckConstraint(
            "status IN ('valid', 'error', 'applied', 'expired')",
            name="ck_maintenance_project_workbook_validation_status",
        ),
        sa.CheckConstraint(
            "error_workbook IS NULL OR status = 'error'",
            name="ck_maintenance_project_workbook_validation_error_file_status",
        ),
        sa.CheckConstraint(
            "plan_json IS NULL OR status IN ('valid', 'applied')",
            name="ck_maintenance_project_workbook_validation_plan_status",
        ),
        sa.CheckConstraint(
            "error_workbook IS NULL OR octet_length(error_workbook) <= 5242880",
            name="ck_maintenance_project_workbook_validation_error_file_size",
        ),
    )
    op.create_index(
        "ix_maintenance_project_workbook_validation_expires",
        "maintenance_project_workbook_validation",
        ["expires_at"],
    )
    op.create_index(
        "ix_maintenance_project_workbook_validation_project_file",
        "maintenance_project_workbook_validation",
        ["project_id", "file_sha256"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    nonempty = [
        table
        for table in _FACT_TABLES
        if bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')).scalar()
    ]
    if nonempty:
        raise RuntimeError(
            "maintenance project operating-fact tables are not empty; "
            f"archive them before downgrade: {', '.join(nonempty)}"
        )
    op.drop_index(
        "ix_maintenance_project_workbook_validation_project_file",
        table_name="maintenance_project_workbook_validation",
    )
    op.drop_index(
        "ix_maintenance_project_workbook_validation_expires",
        table_name="maintenance_project_workbook_validation",
    )
    op.drop_table("maintenance_project_workbook_validation")
    op.drop_index(
        "ix_maintenance_project_workbook_operation_project_file",
        table_name="maintenance_project_workbook_operation",
    )
    op.drop_table("maintenance_project_workbook_operation")
    op.drop_table("maintenance_project_workbook_state")
    op.drop_index(
        "ix_maintenance_project_expense_project_date",
        table_name="maintenance_project_expense_attribution",
    )
    op.drop_table("maintenance_project_expense_attribution")
    op.drop_index(
        "ix_maintenance_site_issue_line_part",
        table_name="maintenance_site_issue_line",
    )
    op.drop_index(
        "ix_maintenance_site_issue_line_issue",
        table_name="maintenance_site_issue_line",
    )
    op.drop_table("maintenance_site_issue_line")
    op.drop_index(
        "ix_maintenance_site_issue_project_date",
        table_name="maintenance_site_issue",
    )
    op.drop_table("maintenance_site_issue")
    op.drop_index(
        "ix_maintenance_collection_project_month",
        table_name="maintenance_collection_snapshot",
    )
    op.drop_table("maintenance_collection_snapshot")
    op.drop_index(
        "ix_maintenance_project_operation_audit_project_time",
        table_name="maintenance_project_operation_audit",
    )
    op.drop_table("maintenance_project_operation_audit")
