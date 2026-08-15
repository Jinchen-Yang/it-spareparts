"""maintenance doc head project_id marker (F3 return-rate readiness)

Revision ID: d7f1a3c5e8b2
Revises: c3e9d1b7f5a2
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa

revision = "d7f1a3c5e8b2"
down_revision = "c3e9d1b7f5a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "maintenance_doc_head_row",
        sa.Column("project_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_maintenance_doc_head_project",
        "maintenance_doc_head_row",
        "maintenance_project",
        ["project_id"],
        ["project_id"],
    )
    op.create_index(
        "ix_maintenance_doc_head_project",
        "maintenance_doc_head_row",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_maintenance_doc_head_project", table_name="maintenance_doc_head_row"
    )
    op.drop_constraint(
        "fk_maintenance_doc_head_project",
        "maintenance_doc_head_row",
        type_="foreignkey",
    )
    op.drop_column("maintenance_doc_head_row", "project_id")
