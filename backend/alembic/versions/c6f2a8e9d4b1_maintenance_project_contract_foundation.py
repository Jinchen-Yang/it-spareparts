"""stable maintenance project and contract foundation

Revision ID: c6f2a8e9d4b1
Revises: f1c8e4a7b2d9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6f2a8e9d4b1"
down_revision: str | None = "f1c8e4a7b2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "maintenance_project",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("project_code", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("project_manager_id", sa.String(length=64), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_project_version"),
        sa.PrimaryKeyConstraint("project_id"),
        sa.UniqueConstraint("project_code"),
    )
    op.create_index(
        "ix_maintenance_project_display_name",
        "maintenance_project",
        ["display_name"],
    )
    op.create_index(
        "ix_maintenance_project_active_code",
        "maintenance_project",
        ["is_active", "project_code"],
    )
    op.execute(
        "CREATE INDEX ix_maintenance_project_code_trgm "
        "ON maintenance_project USING gin (lower(project_code) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_maintenance_project_display_name_trgm "
        "ON maintenance_project USING gin (lower(display_name) gin_trgm_ops)"
    )

    op.create_table(
        "maintenance_project_contract",
        sa.Column("project_contract_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("contract_id", sa.String(length=64), nullable=False),
        sa.Column("contract_no", sa.String(length=64), nullable=False),
        sa.Column("contract_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("contract_status", sa.String(length=64), nullable=True),
        sa.Column("status_mapping_state", sa.String(length=16), nullable=False),
        sa.Column(
            "included_in_total",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status_mapping_state IN ('mapped', 'unmapped')",
            name="ck_maintenance_project_contract_status_mapping",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_maintenance_project_contract_interval",
        ),
        sa.CheckConstraint(
            "contract_amount IS NULL OR "
            "(contract_amount >= 0 AND contract_amount < 1000000000000)",
            name="ck_maintenance_project_contract_amount",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_maintenance_project_contract_version",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["maintenance_project.project_id"],
        ),
        sa.PrimaryKeyConstraint("project_contract_id"),
        sa.UniqueConstraint(
            "project_id",
            "contract_id",
            "effective_from",
            name="uq_maintenance_project_contract_identity",
        ),
    )
    op.create_index(
        "ix_maintenance_project_contract_project",
        "maintenance_project_contract",
        ["project_id"],
    )
    op.create_index(
        "ix_maintenance_project_contract_contract",
        "maintenance_project_contract",
        ["contract_id"],
    )
    op.create_index(
        "ix_maintenance_project_contract_effective",
        "maintenance_project_contract",
        ["included_in_total", "effective_from", "effective_to"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Stable projects and their links are business facts.  A schema rollback may
    # only remove the empty foundation; once populated it must use a forward fix
    # or an explicit export/replay plan rather than silently deleting identity.
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_project)
             OR EXISTS (SELECT 1 FROM maintenance_project_contract)
          THEN
            RAISE EXCEPTION
              'c6f2a8e9d4b1 downgrade blocked: stable maintenance project or contract relationship facts are not empty';
          END IF;
        END
        $migration$;
        """
    )
    op.drop_index(
        "ix_maintenance_project_contract_effective",
        table_name="maintenance_project_contract",
    )
    op.drop_index(
        "ix_maintenance_project_contract_contract",
        table_name="maintenance_project_contract",
    )
    op.drop_index(
        "ix_maintenance_project_contract_project",
        table_name="maintenance_project_contract",
    )
    op.drop_table("maintenance_project_contract")
    op.drop_index(
        "ix_maintenance_project_display_name_trgm",
        table_name="maintenance_project",
    )
    op.drop_index(
        "ix_maintenance_project_code_trgm",
        table_name="maintenance_project",
    )
    op.drop_index("ix_maintenance_project_active_code", table_name="maintenance_project")
    op.drop_index("ix_maintenance_project_display_name", table_name="maintenance_project")
    op.drop_table("maintenance_project")
