"""add import error batch id id index

Revision ID: f8c3d1a6b2e4
Revises: d5a7c9e1f3b6
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f8c3d1a6b2e4"
down_revision: str | None = "d5a7c9e1f3b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_import_error_batch_id_id", "sys_import_error", ["batch_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_import_error_batch_id_id", table_name="sys_import_error")
