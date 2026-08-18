"""cloud replenishment cart and project-master v2 foundation

Revision ID: c9e1a7b3d5f2
Revises: a4c6e8f1b2d3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "c9e1a7b3d5f2"
down_revision: str | None = "a4c6e8f1b2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("maintenance_site_issue_line", sa.Column("remark", sa.Text(), nullable=True))
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.drop_constraint("ck_maintenance_collection_milestone_source", "maintenance_collection_milestone", type_="check")
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_source",
        "maintenance_collection_milestone",
        "source IN ('direct_api', 'manager_workbook_v3', 'project_manager_xls_v1', 'project_master_v2')",
    )
    op.drop_constraint("ck_maintenance_collection_milestone_batch_source", "maintenance_collection_milestone", type_="check")
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_batch_source",
        "maintenance_collection_milestone",
        "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL AND collection_plan_import_batch_id IS NULL AND ledger_batch_id IS NULL) OR "
        "(source = 'project_manager_xls_v1' AND ((collection_plan_import_batch_id IS NOT NULL AND source_batch_id IS NULL AND ledger_batch_id IS NULL) OR (ledger_batch_id IS NOT NULL AND collection_plan_import_batch_id IS NULL AND source_batch_id IS NULL))) OR "
        "(source IN ('direct_api', 'project_master_v2') AND source_batch_id IS NULL AND collection_plan_import_batch_id IS NULL AND ledger_batch_id IS NULL)",
    )
    op.create_index(
        "ix_maintenance_collection_milestone_active_project_date",
        "maintenance_collection_milestone",
        ["project_id", "planned_date", "sequence"],
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "replenishment_cart_draft",
        sa.Column("draft_id", sa.String(length=36), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("request_note", sa.Text(), nullable=True),
        sa.Column("client_request_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["owner_user_id"], ["sys_user.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint("version >= 1", name="ck_replenishment_cart_draft_version"),
        sa.CheckConstraint("char_length(btrim(client_request_id)) BETWEEN 8 AND 128", name="ck_replenishment_cart_draft_client_request"),
        sa.UniqueConstraint("owner_user_id", "project_id", name="uq_replenishment_cart_draft_owner_project"),
        sa.UniqueConstraint("owner_user_id", "client_request_id", name="uq_replenishment_cart_draft_owner_request"),
    )
    op.create_index("ix_replenishment_cart_draft_owner_updated", "replenishment_cart_draft", ["owner_user_id", "updated_at", "draft_id"])
    op.create_table(
        "replenishment_cart_draft_line",
        sa.Column("draft_line_id", sa.String(length=36), primary_key=True),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("special_note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["draft_id"], ["replenishment_cart_draft.draft_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.CheckConstraint("line_no >= 1", name="ck_replenishment_cart_draft_line_no"),
        sa.CheckConstraint("quantity BETWEEN 1 AND 999999", name="ck_replenishment_cart_draft_line_quantity"),
        sa.UniqueConstraint("draft_id", "part_id", name="uq_replenishment_cart_draft_line_part"),
        sa.UniqueConstraint("draft_id", "line_no", name="uq_replenishment_cart_draft_line_no"),
    )


def downgrade() -> None:
    op.drop_table("replenishment_cart_draft_line")
    op.drop_index("ix_replenishment_cart_draft_owner_updated", table_name="replenishment_cart_draft")
    op.drop_table("replenishment_cart_draft")
    op.drop_index("ix_maintenance_collection_milestone_active_project_date", table_name="maintenance_collection_milestone")
    op.drop_constraint("ck_maintenance_collection_milestone_batch_source", "maintenance_collection_milestone", type_="check")
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_batch_source", "maintenance_collection_milestone",
        "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL AND collection_plan_import_batch_id IS NULL AND ledger_batch_id IS NULL) OR "
        "(source = 'project_manager_xls_v1' AND ((collection_plan_import_batch_id IS NOT NULL AND source_batch_id IS NULL AND ledger_batch_id IS NULL) OR (ledger_batch_id IS NOT NULL AND collection_plan_import_batch_id IS NULL AND source_batch_id IS NULL))) OR "
        "(source = 'direct_api' AND source_batch_id IS NULL AND collection_plan_import_batch_id IS NULL AND ledger_batch_id IS NULL)",
    )
    op.drop_constraint("ck_maintenance_collection_milestone_source", "maintenance_collection_milestone", type_="check")
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_source", "maintenance_collection_milestone",
        "source IN ('direct_api', 'manager_workbook_v3', 'project_manager_xls_v1')",
    )
    op.drop_column("maintenance_collection_milestone", "is_active")
    op.drop_column("maintenance_site_issue_line", "remark")
