"""controlled maintenance project master management

Revision ID: d8a3c7e4f2b1
Revises: c6f2a8e9d4b1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8a3c7e4f2b1"
down_revision: str | None = "c6f2a8e9d4b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION = "action_maintenance_project_manage"


def _validate_permission_json() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM sys_role_template
              WHERE permissions IS NOT NULL
                AND permissions <> 'null'::jsonb
                AND jsonb_typeof(permissions) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE template_perms IS NOT NULL
                AND template_perms <> 'null'::jsonb
                AND jsonb_typeof(template_perms) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE permissions IS NOT NULL
                AND permissions <> 'null'::jsonb
                AND jsonb_typeof(permissions) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE perm_overrides IS NOT NULL
                AND perm_overrides <> 'null'::jsonb
                AND jsonb_typeof(perm_overrides) IS DISTINCT FROM 'object'
          )
          THEN
            RAISE EXCEPTION
              'd8a3c7e4f2b1 upgrade blocked: permission JSONB payload must be an object, SQL NULL, or JSON null';
          END IF;
        END
        $migration$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _validate_permission_json()
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT lower(project_code)
              FROM maintenance_project
              GROUP BY lower(project_code)
              HAVING count(*) > 1
          )
          THEN
            RAISE EXCEPTION
              'd8a3c7e4f2b1 blocked: maintenance project codes conflict case-insensitively';
          END IF;
        END
        $migration$;
        """
    )
    op.create_index(
        "ux_maintenance_project_code_ci",
        "maintenance_project",
        [sa.text("lower(project_code)")],
        unique=True,
    )
    op.create_table(
        "maintenance_project_audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("before_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operated_by", sa.String(length=64), nullable=False),
        sa.Column(
            "operated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entity_type IN ('project', 'project_contract')",
            name="ck_maintenance_project_audit_entity_type",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maintenance_project_audit_reason",
        ),
        sa.CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_project_audit_operator",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["maintenance_project.project_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_maintenance_project_audit_project_time",
        "maintenance_project_audit_log",
        ["project_id", "operated_at", "id"],
    )
    op.create_index(
        "ix_maintenance_project_audit_entity_time",
        "maintenance_project_audit_log",
        ["entity_type", "entity_id", "operated_at", "id"],
    )

    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'action_maintenance_project_manage', code = 'admin'
            )
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
                'action_maintenance_project_manage',
                role = 'admin'
            ),
            perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides
                    ELSE '{}'::jsonb
                END - 'action_maintenance_project_manage'
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'action_maintenance_project_manage', role = 'admin'
            )
        WHERE permissions IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Only object payloads are mutated below.  Malformed legacy payloads remain
    # untouched so the predecessor migration can enforce its own downgrade guard.
    op.execute(
        "LOCK TABLE maintenance_project_audit_log, maintenance_project_contract, "
        "maintenance_project IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_project_audit_log)
             OR EXISTS (SELECT 1 FROM maintenance_project_contract)
             OR EXISTS (SELECT 1 FROM maintenance_project)
          THEN
            RAISE EXCEPTION
              'd8a3c7e4f2b1 downgrade blocked: maintenance project facts are not empty';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = permissions - 'action_maintenance_project_manage'
        WHERE jsonb_typeof(permissions) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms - 'action_maintenance_project_manage'
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET permissions = permissions - 'action_maintenance_project_manage'
        WHERE jsonb_typeof(permissions) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET perm_overrides = perm_overrides - 'action_maintenance_project_manage'
        WHERE jsonb_typeof(perm_overrides) = 'object'
        """
    )
    op.drop_index(
        "ix_maintenance_project_audit_entity_time",
        table_name="maintenance_project_audit_log",
    )
    op.drop_index(
        "ix_maintenance_project_audit_project_time",
        table_name="maintenance_project_audit_log",
    )
    op.drop_table("maintenance_project_audit_log")
    op.drop_index("ux_maintenance_project_code_ci", table_name="maintenance_project")
