"""add project-manager monthly workbook v3 facts

Revision ID: b7e1c3a9d5f2
Revises: a6c8d2e4f1b7
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7e1c3a9d5f2"
down_revision: str | None = "a6c8d2e4f1b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "maintenance_manager_upload_batch",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("report_month", sa.Date(), nullable=False),
        sa.Column("protocol_version", sa.String(16), nullable=False),
        sa.Column("template_version", sa.String(32), nullable=False),
        sa.Column("export_id", sa.String(64), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("operation_key", sa.String(64), nullable=False, unique=True),
        sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("scope_version", sa.String(64), nullable=False),
        sa.Column("data_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("plan_json", postgresql.JSONB(), nullable=True),
        sa.Column("issues_json", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("error_workbook", sa.LargeBinary(), nullable=True),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_by", sa.String(64), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["sys_user.id"]),
        sa.CheckConstraint(
            "report_month = date_trunc('month', report_month)::date",
            name="ck_maintenance_manager_batch_month_start",
        ),
        sa.CheckConstraint(
            "file_size > 0 AND file_size <= 67108864",
            name="ck_maintenance_manager_batch_file_size",
        ),
        sa.CheckConstraint(
            "status IN ('valid', 'error', 'applied', 'expired')",
            name="ck_maintenance_manager_batch_status",
        ),
        sa.CheckConstraint(
            "(status IN ('valid', 'applied') AND plan_json IS NOT NULL) OR "
            "(status IN ('error', 'expired'))",
            name="ck_maintenance_manager_batch_plan_status",
        ),
        sa.CheckConstraint(
            "error_workbook IS NULL OR (status = 'error' AND octet_length(error_workbook) <= 5242880)",
            name="ck_maintenance_manager_batch_error_workbook",
        ),
        sa.CheckConstraint(
            "(status = 'applied' AND applied_at IS NOT NULL AND applied_by IS NOT NULL "
            "AND result_json IS NOT NULL) OR "
            "(status <> 'applied' AND applied_at IS NULL AND applied_by IS NULL)",
            name="ck_maintenance_manager_batch_applied_state",
        ),
    )
    op.create_index(
        "ix_maintenance_manager_batch_owner_month",
        "maintenance_manager_upload_batch",
        ["owner_user_id", "report_month", "created_at"],
    )
    op.create_index(
        "ix_maintenance_manager_batch_status_expiry",
        "maintenance_manager_upload_batch",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_maintenance_manager_batch_semantic",
        "maintenance_manager_upload_batch",
        ["owner_user_id", "report_month", "semantic_hash"],
    )

    op.create_table(
        "maintenance_service_period",
        sa.Column("project_id", sa.String(36), primary_key=True),
        sa.Column("service_start", sa.Date(), nullable=True),
        sa.Column("service_end", sa.Date(), nullable=True),
        sa.Column("completeness_state", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_batch_id", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(["source_batch_id"], ["maintenance_manager_upload_batch.batch_id"]),
        sa.CheckConstraint(
            "completeness_state IN ('complete', 'start_only', 'end_only', 'empty')",
            name="ck_maintenance_service_period_completeness",
        ),
        sa.CheckConstraint(
            "(completeness_state = 'complete' AND service_start IS NOT NULL AND service_end IS NOT NULL) OR "
            "(completeness_state = 'start_only' AND service_start IS NOT NULL AND service_end IS NULL) OR "
            "(completeness_state = 'end_only' AND service_start IS NULL AND service_end IS NOT NULL) OR "
            "(completeness_state = 'empty' AND service_start IS NULL AND service_end IS NULL)",
            name="ck_maintenance_service_period_state_fields",
        ),
        sa.CheckConstraint(
            "service_start IS NULL OR service_end IS NULL OR service_end >= service_start",
            name="ck_maintenance_service_period_date_order",
        ),
        sa.CheckConstraint(
            "source IN ('direct_api', 'manager_workbook_v3')",
            name="ck_maintenance_service_period_source",
        ),
        sa.CheckConstraint(
            "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL) OR "
            "(source = 'direct_api' AND source_batch_id IS NULL)",
            name="ck_maintenance_service_period_batch_source",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_service_period_version"),
    )

    op.create_table(
        "maintenance_collection_milestone",
        sa.Column("milestone_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("project_contract_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("planned_date", sa.Date(), nullable=True),
        sa.Column("planned_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("completeness_state", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_batch_id", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(
            ["project_contract_id"], ["maintenance_project_contract.project_contract_id"]
        ),
        sa.ForeignKeyConstraint(["source_batch_id"], ["maintenance_manager_upload_batch.batch_id"]),
        sa.CheckConstraint("sequence BETWEEN 1 AND 24", name="ck_maintenance_collection_milestone_sequence"),
        sa.CheckConstraint(
            "planned_amount IS NULL OR (planned_amount > 0 AND planned_amount < 1000000000000)",
            name="ck_maintenance_collection_milestone_amount",
        ),
        sa.CheckConstraint(
            "completeness_state IN ('complete', 'date_only', 'amount_only')",
            name="ck_maintenance_collection_milestone_completeness",
        ),
        sa.CheckConstraint(
            "(completeness_state = 'complete' AND planned_date IS NOT NULL AND planned_amount IS NOT NULL) OR "
            "(completeness_state = 'date_only' AND planned_date IS NOT NULL AND planned_amount IS NULL) OR "
            "(completeness_state = 'amount_only' AND planned_date IS NULL AND planned_amount IS NOT NULL)",
            name="ck_maintenance_collection_milestone_state_fields",
        ),
        sa.CheckConstraint(
            "source IN ('direct_api', 'manager_workbook_v3')",
            name="ck_maintenance_collection_milestone_source",
        ),
        sa.CheckConstraint(
            "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL) OR "
            "(source = 'direct_api' AND source_batch_id IS NULL)",
            name="ck_maintenance_collection_milestone_batch_source",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_collection_milestone_version"),
        sa.UniqueConstraint(
            "project_contract_id", "sequence", name="uq_maintenance_collection_milestone_contract_sequence"
        ),
    )
    op.create_index(
        "ix_maintenance_collection_milestone_project_date",
        "maintenance_collection_milestone",
        ["project_id", "planned_date", "sequence"],
    )

    op.create_table(
        "maintenance_acceptance_deliverable",
        sa.Column("deliverable_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("deliverable_type", sa.String(32), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("submission_status", sa.String(16), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_by", sa.String(64), nullable=True),
        sa.Column("approval_status", sa.String(16), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("configuration_state", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint(
            "deliverable_type IN ('acceptance_report', 'inspection_report')",
            name="ck_maintenance_acceptance_deliverable_type",
        ),
        sa.CheckConstraint(
            "submission_status IN ('not_submitted', 'submitted')",
            name="ck_maintenance_acceptance_submission_status",
        ),
        sa.CheckConstraint(
            "approval_status IN ('not_reviewed', 'approved', 'rejected')",
            name="ck_maintenance_acceptance_approval_status",
        ),
        sa.CheckConstraint(
            "configuration_state IN ('configured', 'pending_business_configuration')",
            name="ck_maintenance_acceptance_configuration",
        ),
        sa.CheckConstraint(
            "(submission_status = 'not_submitted' AND submitted_at IS NULL AND submitted_by IS NULL) OR "
            "(submission_status = 'submitted' AND submitted_at IS NOT NULL AND submitted_by IS NOT NULL)",
            name="ck_maintenance_acceptance_submission_fields",
        ),
        sa.CheckConstraint(
            "(approval_status = 'not_reviewed' AND approved_at IS NULL AND approved_by IS NULL AND rejection_reason IS NULL) OR "
            "(approval_status = 'approved' AND approved_at IS NOT NULL AND approved_by IS NOT NULL AND rejection_reason IS NULL) OR "
            "(approval_status = 'rejected' AND approved_at IS NOT NULL AND approved_by IS NOT NULL "
            "AND rejection_reason IS NOT NULL AND char_length(btrim(rejection_reason)) > 0)",
            name="ck_maintenance_acceptance_approval_fields",
        ),
        sa.CheckConstraint(
            "approval_status = 'not_reviewed' OR submission_status = 'submitted'",
            name="ck_maintenance_acceptance_approval_requires_submission",
        ),
        sa.CheckConstraint(
            "submitted_by IS NULL OR approved_by IS NULL OR submitted_by <> approved_by",
            name="ck_maintenance_acceptance_no_self_approval",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_acceptance_version"),
        sa.UniqueConstraint(
            "project_id", "deliverable_type", name="uq_maintenance_acceptance_project_type"
        ),
    )
    op.create_index(
        "ix_maintenance_acceptance_project_due",
        "maintenance_acceptance_deliverable",
        ["project_id", "due_date", "deliverable_type"],
    )

    op.create_table(
        "business_file",
        sa.Column("file_id", sa.String(36), primary_key=True),
        sa.Column("storage_provider", sa.String(32), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("original_filename", sa.String(256), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("security_state", sa.String(16), nullable=False),
        sa.Column("uploaded_by", sa.String(64), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "storage_provider IN ('local', 'object_storage')",
            name="ck_business_file_storage_provider",
        ),
        sa.CheckConstraint(
            "char_length(btrim(object_key)) > 0 AND "
            "btrim(object_key) !~* '^(https?|ftp)://'",
            name="ck_business_file_object_key_not_external_url",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 52428800",
            name="ck_business_file_size",
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_business_file_sha256"),
        sa.CheckConstraint(
            "security_state IN ('quarantined', 'active', 'blocked')",
            name="ck_business_file_security_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_business_file_version"),
        sa.UniqueConstraint(
            "storage_provider", "object_key", name="uq_business_file_storage_object"
        ),
    )
    op.create_index("ix_business_file_sha256", "business_file", ["sha256"])

    op.create_table(
        "business_file_link",
        sa.Column("link_id", sa.String(36), primary_key=True),
        sa.Column("file_id", sa.String(36), nullable=False),
        sa.Column("entity_type", sa.String(48), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("relation_type", sa.String(24), nullable=False),
        sa.Column("acl_scope", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.String(64), nullable=True),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["business_file.file_id"]),
        sa.CheckConstraint(
            "entity_type = 'maintenance_acceptance_deliverable'",
            name="ck_business_file_link_entity_type",
        ),
        sa.CheckConstraint(
            "relation_type = 'evidence'",
            name="ck_business_file_link_relation_type",
        ),
        sa.CheckConstraint(
            "acl_scope = 'project_members'",
            name="ck_business_file_link_acl_scope",
        ),
        sa.CheckConstraint(
            "(archived_at IS NULL AND archived_by IS NULL AND archive_reason IS NULL) OR "
            "(archived_at IS NOT NULL AND archived_by IS NOT NULL AND archive_reason IS NOT NULL "
            "AND char_length(btrim(archive_reason)) > 0)",
            name="ck_business_file_link_archive",
        ),
        sa.UniqueConstraint(
            "file_id", "entity_type", "entity_id", "relation_type",
            name="uq_business_file_link_identity",
        ),
    )
    op.create_index(
        "ix_business_file_link_entity_active",
        "business_file_link",
        ["entity_type", "entity_id", "archived_at"],
    )

    op.create_table(
        "maintenance_manager_upload_batch_project",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(36), primary_key=True),
        sa.Column("assignment_id", sa.String(36), nullable=False),
        sa.Column("assignment_version", sa.Integer(), nullable=False),
        sa.Column("project_version", sa.Integer(), nullable=False),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["maintenance_manager_upload_batch.batch_id"]),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["maintenance_project_user_assignment.assignment_id"]
        ),
        sa.CheckConstraint(
            "assignment_version >= 1 AND project_version >= 1",
            name="ck_maintenance_manager_batch_project_versions",
        ),
    )
    op.create_index(
        "ix_maintenance_manager_batch_project_monthly_task",
        "maintenance_manager_upload_batch_project",
        ["project_id", "applied_at"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_manager_upload_batch, maintenance_manager_upload_batch_project, "
        "maintenance_service_period, maintenance_collection_milestone, "
        "maintenance_acceptance_deliverable, business_file, business_file_link "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_manager_upload_batch)
             OR EXISTS (SELECT 1 FROM maintenance_service_period)
             OR EXISTS (SELECT 1 FROM maintenance_collection_milestone)
             OR EXISTS (SELECT 1 FROM maintenance_acceptance_deliverable)
             OR EXISTS (SELECT 1 FROM business_file)
          THEN
            RAISE EXCEPTION
              'b7e1c3a9d5f2 downgrade blocked: manager workbook or attachment facts exist';
          END IF;
        END
        $migration$;
        """
    )
    op.drop_index(
        "ix_maintenance_manager_batch_project_monthly_task",
        table_name="maintenance_manager_upload_batch_project",
    )
    op.drop_table("maintenance_manager_upload_batch_project")
    op.drop_index("ix_business_file_link_entity_active", table_name="business_file_link")
    op.drop_table("business_file_link")
    op.drop_index("ix_business_file_sha256", table_name="business_file")
    op.drop_table("business_file")
    op.drop_index(
        "ix_maintenance_acceptance_project_due",
        table_name="maintenance_acceptance_deliverable",
    )
    op.drop_table("maintenance_acceptance_deliverable")
    op.drop_index(
        "ix_maintenance_collection_milestone_project_date",
        table_name="maintenance_collection_milestone",
    )
    op.drop_table("maintenance_collection_milestone")
    op.drop_table("maintenance_service_period")
    op.drop_index(
        "ix_maintenance_manager_batch_semantic",
        table_name="maintenance_manager_upload_batch",
    )
    op.drop_index(
        "ix_maintenance_manager_batch_status_expiry",
        table_name="maintenance_manager_upload_batch",
    )
    op.drop_index(
        "ix_maintenance_manager_batch_owner_month",
        table_name="maintenance_manager_upload_batch",
    )
    op.drop_table("maintenance_manager_upload_batch")
