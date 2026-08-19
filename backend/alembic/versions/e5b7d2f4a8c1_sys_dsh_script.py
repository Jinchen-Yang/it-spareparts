"""sys_dsh_script: DSH agent whitelist scripts

Revision ID: e5b7d2f4a8c1
Revises: d1f3a5c7e2b4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b7d2f4a8c1"
down_revision: str | None = "d1f3a5c7e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sys_dsh_script",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(256), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        # 执行本脚本所需的既有动作权限键（require_action 口径）；空 = 仅需 page_chat
        sa.Column("required_action", sa.String(64), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sys_dsh_script_enabled", "sys_dsh_script", ["enabled"])


def downgrade() -> None:
    op.drop_index("ix_sys_dsh_script_enabled", table_name="sys_dsh_script")
    op.drop_table("sys_dsh_script")
