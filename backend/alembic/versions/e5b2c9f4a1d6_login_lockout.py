"""add login lockout fields to sys_user (brute-force protection)

Revision ID: e5b2c9f4a1d6
Revises: a3f1c9e2d7b8
Create Date: 2026-06-15 02:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b2c9f4a1d6"
down_revision: Union[str, Sequence[str], None] = "a3f1c9e2d7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sys_user", sa.Column("failed_attempts", sa.Integer(),
                                        server_default="0", nullable=False))
    op.add_column("sys_user", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sys_user", "locked_until")
    op.drop_column("sys_user", "failed_attempts")
