"""guard canonical XSDD claims when a maintenance order is updated

Revision ID: e4a8c2f6b1d9
Revises: d2c7e9f1a4b6
"""

from collections.abc import Sequence

from alembic import op


revision: str = "e4a8c2f6b1d9"
down_revision: str | None = "d2c7e9f1a4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION maintenance_order_claim_xsdd_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            owner_project_id text;
        BEGIN
            IF maintenance_normalize_xsdd(NEW.linked_sales_order_no)
               = maintenance_normalize_xsdd(OLD.linked_sales_order_no) THEN
                RETURN NEW;
            END IF;
            SELECT assignment.project_id INTO owner_project_id
            FROM maintenance_source_order_assignment AS assignment
            WHERE assignment.source_order_id = NEW.raw_order_id
              AND assignment.is_active IS TRUE;
            IF owner_project_id IS NOT NULL THEN
                PERFORM maintenance_claim_project_xsdd(
                    NEW.linked_sales_order_no,
                    owner_project_id,
                    'maintenance_order_trigger'
                );
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_order_claim_xsdd
        BEFORE UPDATE OF linked_sales_order_no
        ON f_maintenance_order
        FOR EACH ROW EXECUTE FUNCTION maintenance_order_claim_xsdd_trigger()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_maintenance_order_claim_xsdd "
        "ON f_maintenance_order"
    )
    op.execute("DROP FUNCTION IF EXISTS maintenance_order_claim_xsdd_trigger()")
