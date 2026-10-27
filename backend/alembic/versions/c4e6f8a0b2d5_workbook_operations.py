"""通用工作簿两步提交与首次回执。

Revision ID: c4e6f8a0b2d5
Revises: b2d4f6a8c0e1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c4e6f8a0b2d5"
down_revision = "b2d4f6a8c0e1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workbook_operation",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("schema_name", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=True),
        sa.Column("subject_id", sa.BigInteger(), nullable=True),
        sa.Column("export_id", sa.String(128), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="previewed", nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("export_id", "file_sha256", "actor", name="uq_workbook_operation_file_actor"),
        sa.CheckConstraint("status IN ('previewed', 'applied', 'failed')", name="ck_workbook_operation_status"),
    )


def downgrade():
    op.drop_table("workbook_operation")
