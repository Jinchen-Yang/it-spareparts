"""add sys_import_job (batch upload jobs, §9)

Revision ID: d4e8b1a07c62
Revises: 2de8eaf62e48
Create Date: 2026-06-14 23:55:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e8b1a07c62"
down_revision: Union[str, Sequence[str], None] = "2de8eaf62e48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sys_import_job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="processing", nullable=False),
        sa.Column("mode", sa.String(length=16), server_default="skip", nullable=False),
        sa.Column("total_files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("done_files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("sys_import_batch", sa.Column("import_job_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_batch_import_job", "sys_import_batch", "sys_import_job",
                          ["import_job_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_batch_import_job", "sys_import_batch", type_="foreignkey")
    op.drop_column("sys_import_batch", "import_job_id")
    op.drop_table("sys_import_job")
