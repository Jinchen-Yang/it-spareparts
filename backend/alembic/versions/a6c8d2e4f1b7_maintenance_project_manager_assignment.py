"""add explicit maintenance project manager assignments

Revision ID: a6c8d2e4f1b7
Revises: e6a9c3f1b2d4
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a6c8d2e4f1b7"
down_revision: str | None = "e6a9c3f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "maintenance_project_user_assignment",
        sa.Column("assignment_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("responsibility_type", sa.String(32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source_manager_text", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("assigned_by", sa.String(64), nullable=False),
        sa.Column("assignment_reason", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.String(64), nullable=True),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["sys_user.id"]),
        sa.CheckConstraint(
            "responsibility_type = 'primary_manager'",
            name="ck_maintenance_project_user_assignment_type",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_maintenance_project_user_assignment_version",
        ),
        sa.CheckConstraint(
            "char_length(btrim(assigned_by)) > 0",
            name="ck_maintenance_project_user_assignment_assigner",
        ),
        sa.CheckConstraint(
            "char_length(btrim(assignment_reason)) > 0",
            name="ck_maintenance_project_user_assignment_reason",
        ),
        sa.CheckConstraint(
            "(archived_at IS NULL AND archived_by IS NULL AND archive_reason IS NULL) OR "
            "(archived_at IS NOT NULL AND archived_by IS NOT NULL AND "
            "archive_reason IS NOT NULL AND char_length(btrim(archived_by)) > 0 AND "
            "char_length(btrim(archive_reason)) > 0 AND archived_at >= assigned_at)",
            name="ck_maintenance_project_user_assignment_archive",
        ),
    )
    op.create_index(
        "ux_maintenance_project_primary_manager_active",
        "maintenance_project_user_assignment",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(
            "archived_at IS NULL AND responsibility_type = 'primary_manager'"
        ),
    )
    op.create_index(
        "ix_maintenance_project_user_assignment_user_active",
        "maintenance_project_user_assignment",
        ["user_id", "archived_at"],
    )
    op.create_index(
        "ix_maintenance_project_user_assignment_project_time",
        "maintenance_project_user_assignment",
        ["project_id", "assigned_at", "assignment_id"],
    )
    op.execute(
        """
        CREATE FUNCTION guard_maintenance_project_user_assignment_history()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $guard$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'maintenance project manager assignment history cannot be deleted';
            END IF;

            IF OLD.archived_at IS NOT NULL THEN
                RAISE EXCEPTION
                    'archived maintenance project manager assignment is immutable';
            END IF;

            IF NEW.assignment_id IS DISTINCT FROM OLD.assignment_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.responsibility_type IS DISTINCT FROM OLD.responsibility_type
               OR NEW.user_id IS DISTINCT FROM OLD.user_id
               OR NEW.source_manager_text IS DISTINCT FROM OLD.source_manager_text
               OR NEW.assigned_at IS DISTINCT FROM OLD.assigned_at
               OR NEW.assigned_by IS DISTINCT FROM OLD.assigned_by
               OR NEW.assignment_reason IS DISTINCT FROM OLD.assignment_reason THEN
                RAISE EXCEPTION
                    'maintenance project manager assignment identity is immutable';
            END IF;

            IF NEW.archived_at IS NULL
               OR NEW.archived_by IS NULL
               OR NEW.archive_reason IS NULL
               OR NEW.version IS DISTINCT FROM OLD.version + 1 THEN
                RAISE EXCEPTION
                    'active maintenance project manager assignment may only be archived once';
            END IF;

            RETURN NEW;
        END;
        $guard$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_project_user_assignment_history
        BEFORE UPDATE OR DELETE ON maintenance_project_user_assignment
        FOR EACH ROW
        EXECUTE FUNCTION guard_maintenance_project_user_assignment_history()
        """
    )
    op.drop_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        "entity_type IN ('project', 'project_contract', 'manager_assignment')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Assignment and audit history are compliance records. Refuse a rollback
    # that would silently discard them or fail later with an opaque check-
    # constraint error.
    op.execute(
        "LOCK TABLE maintenance_project_user_assignment, "
        "maintenance_project_audit_log IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_project_user_assignment)
             OR EXISTS (
                SELECT 1
                FROM maintenance_project_audit_log
                WHERE entity_type = 'manager_assignment'
             )
          THEN
            RAISE EXCEPTION
              'a6c8d2e4f1b7 downgrade blocked: manager assignment history is not empty';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_maintenance_project_user_assignment_history "
        "ON maintenance_project_user_assignment"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS guard_maintenance_project_user_assignment_history()"
    )
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
    op.drop_index(
        "ix_maintenance_project_user_assignment_project_time",
        table_name="maintenance_project_user_assignment",
    )
    op.drop_index(
        "ix_maintenance_project_user_assignment_user_active",
        table_name="maintenance_project_user_assignment",
    )
    op.drop_index(
        "ux_maintenance_project_primary_manager_active",
        table_name="maintenance_project_user_assignment",
    )
    op.drop_table("maintenance_project_user_assignment")
