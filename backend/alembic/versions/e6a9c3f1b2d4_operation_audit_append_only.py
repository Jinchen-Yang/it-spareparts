"""make maintenance project operation audit rows append-only

Revision ID: e6a9c3f1b2d4
Revises: c4e8a1d7f2b6
"""

from collections.abc import Sequence

from alembic import op


revision: str = "e6a9c3f1b2d4"
down_revision: str | None = "c4e8a1d7f2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION reject_maintenance_project_operation_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'maintenance_project_operation_audit is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_project_operation_audit_append_only
        BEFORE UPDATE OR DELETE ON maintenance_project_operation_audit
        FOR EACH ROW
        EXECUTE FUNCTION reject_maintenance_project_operation_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER trg_maintenance_project_operation_audit_append_only
        ON maintenance_project_operation_audit
        """
    )
    op.execute(
        "DROP FUNCTION reject_maintenance_project_operation_audit_mutation()"
    )
