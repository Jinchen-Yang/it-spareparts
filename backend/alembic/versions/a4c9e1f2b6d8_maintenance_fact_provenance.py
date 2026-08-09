"""add first-class maintenance fact provenance

Revision ID: a4c9e1f2b6d8
Revises: f3b7d9e1c5a2
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a4c9e1f2b6d8"
down_revision: str | None = "f3b7d9e1c5a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "maintenance_collection_snapshot",
        sa.Column(
            "source",
            sa.String(length=24),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.add_column(
        "maintenance_collection_snapshot",
        sa.Column("import_batch_id", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_maintenance_collection_source",
        "maintenance_collection_snapshot",
        "source IN ('legacy', 'direct_api', 'workbook')",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_import_batch",
        "maintenance_collection_snapshot",
        "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
        "(source IN ('legacy', 'direct_api') AND import_batch_id IS NULL)",
    )
    op.add_column(
        "maintenance_site_issue",
        sa.Column(
            "source",
            sa.String(length=24),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.add_column(
        "maintenance_site_issue",
        sa.Column("import_batch_id", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_source",
        "maintenance_site_issue",
        "source IN ('legacy', 'direct_api', 'workbook')",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_import_batch",
        "maintenance_site_issue",
        "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
        "(source IN ('legacy', 'direct_api') AND import_batch_id IS NULL)",
    )
    op.add_column(
        "maintenance_project_workbook_operation",
        sa.Column("entity_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_maintenance_workbook_operation_collection",
        "maintenance_project_workbook_operation",
        "maintenance_collection_snapshot",
        ["entity_id"],
        ["collection_id"],
    )
    op.create_index(
        "ix_maintenance_project_workbook_operation_entity",
        "maintenance_project_workbook_operation",
        ["entity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_maintenance_project_workbook_operation_entity",
        table_name="maintenance_project_workbook_operation",
    )
    op.drop_constraint(
        "fk_maintenance_workbook_operation_collection",
        "maintenance_project_workbook_operation",
        type_="foreignkey",
    )
    op.drop_column("maintenance_project_workbook_operation", "entity_id")
    op.drop_constraint(
        "ck_maintenance_site_issue_import_batch",
        "maintenance_site_issue",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_source",
        "maintenance_site_issue",
        type_="check",
    )
    op.drop_column("maintenance_site_issue", "import_batch_id")
    op.drop_column("maintenance_site_issue", "source")
    op.drop_constraint(
        "ck_maintenance_collection_import_batch",
        "maintenance_collection_snapshot",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_collection_source",
        "maintenance_collection_snapshot",
        type_="check",
    )
    op.drop_column("maintenance_collection_snapshot", "import_batch_id")
    op.drop_column("maintenance_collection_snapshot", "source")
