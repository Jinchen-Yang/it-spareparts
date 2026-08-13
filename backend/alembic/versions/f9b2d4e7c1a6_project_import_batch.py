"""add tritium project import batch and source link

Revision ID: f9b2d4e7c1a6
Revises: d9f1a3c7e5b2
Create Date: 2026-08-12 18:15:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f9b2d4e7c1a6"
down_revision: Union[str, None] = "d9f1a3c7e5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "maintenance_project_import_batch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), server_default="preview", nullable=False),
        sa.Column("preview_json", postgresql.JSONB(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operated_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('preview', 'applied', 'error')",
            name="ck_project_import_batch_status",
        ),
        sa.CheckConstraint(
            "char_length(btrim(filename)) > 0",
            name="ck_project_import_batch_filename",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_project_import_batch_hash", "maintenance_project_import_batch", ["file_hash"])
    op.create_index("ix_project_import_batch_status", "maintenance_project_import_batch", ["status", "created_at"])

    op.create_table(
        "maintenance_project_source_link",
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("maintenance_project.project_id"), nullable=False),
        sa.Column("first_batch_id", sa.Integer(), sa.ForeignKey("maintenance_project_import_batch.id"), nullable=False),
        sa.Column("latest_batch_id", sa.Integer(), sa.ForeignKey("maintenance_project_import_batch.id"), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "char_length(btrim(source_id)) > 0",
            name="ck_project_source_link_source_id",
        ),
        sa.PrimaryKeyConstraint("source_id"),
        sa.UniqueConstraint("project_id", name="uq_project_source_link_project"),
    )
    op.create_index("ix_project_source_link_project", "maintenance_project_source_link", ["project_id"])


def downgrade() -> None:
    op.drop_table("maintenance_project_source_link")
    op.drop_table("maintenance_project_import_batch")
