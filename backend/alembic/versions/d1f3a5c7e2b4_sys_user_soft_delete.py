"""sys_user soft-delete column

Revision ID: d1f3a5c7e2b4
Revises: e2a4c6b8d1f3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f3a5c7e2b4"
down_revision: str | None = "e2a4c6b8d1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sys_user",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sys_user_deleted_at", "sys_user", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_sys_user_deleted_at", table_name="sys_user")
    op.drop_column("sys_user", "deleted_at")
