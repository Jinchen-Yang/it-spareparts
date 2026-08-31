"""record explicit salesperson overrides on maintenance projects

Revision ID: f6b1d3e8a2c4
Revises: e4a8c2f6b1d9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "f6b1d3e8a2c4"
down_revision: str | None = "e4a8c2f6b1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "maintenance_project",
        sa.Column(
            "salesperson_override_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("maintenance_project", "salesperson_override_active")
