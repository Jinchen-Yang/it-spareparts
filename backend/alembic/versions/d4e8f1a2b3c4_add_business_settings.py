"""add singleton business settings

Revision ID: d4e8f1a2b3c4
Revises: c9d4e7f2a6b1

The row is a typed singleton rather than a free-form key/value bag so every
business switch has a database-level type, default and rollback contract.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8f1a2b3c4"
down_revision: str | None = "c9d4e7f2a6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sys_business_setting",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "maintenance_project_profit_default_basis",
            sa.String(length=8),
            server_default=sa.text("'both'"),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_sys_business_setting_singleton"),
        sa.CheckConstraint(
            "maintenance_project_profit_default_basis IN ('inc', 'ex', 'both')",
            name="ck_sys_business_setting_maintenance_profit_basis",
        ),
        sa.CheckConstraint("version >= 1", name="ck_sys_business_setting_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO sys_business_setting"
        " (id, maintenance_project_profit_default_basis, version)"
        " VALUES (1, 'both', 1)",
    )


def downgrade() -> None:
    op.drop_table("sys_business_setting")
