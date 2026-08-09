"""manual source maintenance order assignment history

Revision ID: e6f1a9c3b7d2
Revises: c4e8a1d7f2b6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "e6f1a9c3b7d2"
down_revision: str | None = "c4e8a1d7f2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        "entity_type IN ("
        "'project', 'project_contract', 'source_order_assignment'"
        ")",
    )
    op.create_table(
        "maintenance_source_order_assignment",
        sa.Column("assignment_id", sa.String(length=36), nullable=False),
        sa.Column("source_order_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_by", sa.String(length=64), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_maintenance_source_assignment_version",
        ),
        sa.CheckConstraint(
            "char_length(btrim(created_by)) > 0",
            name="ck_maintenance_source_assignment_creator",
        ),
        sa.CheckConstraint(
            "(is_active AND archived_at IS NULL AND archived_by IS NULL) OR "
            "(NOT is_active AND archived_at IS NOT NULL "
            "AND char_length(btrim(archived_by)) > 0)",
            name="ck_maintenance_source_assignment_archive_state",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["maintenance_project.project_id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_order_id"],
            ["f_maintenance_order.raw_order_id"],
        ),
        sa.PrimaryKeyConstraint("assignment_id"),
    )
    op.create_index(
        "ux_maintenance_source_assignment_active_order",
        "maintenance_source_order_assignment",
        ["source_order_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_maintenance_source_assignment_project_active",
        "maintenance_source_order_assignment",
        ["project_id", "is_active"],
    )
    op.create_index(
        "ix_maintenance_source_assignment_order_created",
        "maintenance_source_order_assignment",
        ["source_order_id", "created_at", "assignment_id"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_source_order_assignment, "
        "maintenance_project_audit_log IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_source_order_assignment)
             OR EXISTS (
               SELECT 1 FROM maintenance_project_audit_log
               WHERE entity_type = 'source_order_assignment'
             )
          THEN
            RAISE EXCEPTION
              'e6f1a9c3b7d2 downgrade blocked: source order assignment history is not empty';
          END IF;
        END
        $migration$;
        """
    )
    op.drop_index(
        "ix_maintenance_source_assignment_order_created",
        table_name="maintenance_source_order_assignment",
    )
    op.drop_index(
        "ix_maintenance_source_assignment_project_active",
        table_name="maintenance_source_order_assignment",
    )
    op.drop_index(
        "ux_maintenance_source_assignment_active_order",
        table_name="maintenance_source_order_assignment",
    )
    op.drop_table("maintenance_source_order_assignment")
    op.drop_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        "entity_type IN ('project', 'project_contract')",
    )
