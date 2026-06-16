"""add ip_address/user_agent to sys_access_log (login source audit)

Revision ID: c1d7e3a9f2b4
Revises: f6c4a2e9b1d3
Create Date: 2026-06-16 00:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d7e3a9f2b4"
down_revision: Union[str, Sequence[str], None] = "f6c4a2e9b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sys_access_log", sa.Column("ip_address", sa.String(length=64), nullable=True))
    op.add_column("sys_access_log", sa.Column("user_agent", sa.String(length=300), nullable=True))


def downgrade() -> None:
    op.drop_column("sys_access_log", "user_agent")
    op.drop_column("sys_access_log", "ip_address")
