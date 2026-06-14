"""add token_version to sys_user (immediate token revocation)

Revision ID: f6c4a2e9b1d3
Revises: e5b2c9f4a1d6
Create Date: 2026-06-15 03:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6c4a2e9b1d3"
down_revision: Union[str, Sequence[str], None] = "e5b2c9f4a1d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sys_user", sa.Column("token_version", sa.Integer(),
                                        server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("sys_user", "token_version")
