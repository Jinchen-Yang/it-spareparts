"""index workbook validation retention cleanup

Revision ID: f3b7d9e1c5a2
Revises: e2f4a6c8b1d3
"""

from collections.abc import Sequence

from alembic import op


revision: str = "f3b7d9e1c5a2"
down_revision: str | None = "e2f4a6c8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "ix_maintenance_project_workbook_validation_status_applied"
_TABLE_NAME = "maintenance_project_workbook_validation"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        _TABLE_NAME,
        ["status", "applied_at"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
