"""account mgmt: sys_user.permissions/last_login_at + sys_access_log

按用户细粒度权限(permissions JSONB) + 最近登录(last_login_at) + 访问活动日志表。
纯 additive，不动既有数据。

Revision ID: a3f1c9e2d7b8
Revises: d4e8b1a07c62
Create Date: 2026-06-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a3f1c9e2d7b8"
down_revision: Union[str, None] = "d4e8b1a07c62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sys_user", sa.Column("permissions", JSONB(), nullable=True))
    op.add_column("sys_user", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "sys_access_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_user_time", "sys_access_log", ["username", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_access_user_time", table_name="sys_access_log")
    op.drop_table("sys_access_log")
    op.drop_column("sys_user", "last_login_at")
    op.drop_column("sys_user", "permissions")
