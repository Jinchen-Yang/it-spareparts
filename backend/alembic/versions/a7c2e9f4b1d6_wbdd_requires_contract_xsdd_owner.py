"""require a contract-backed XSDD owner before WBDD assignment

Revision ID: a7c2e9f4b1d6
Revises: f6b1d3e8a2c4
"""

from collections.abc import Sequence

from alembic import op


revision: str = "a7c2e9f4b1d6"
down_revision: str | None = "f6b1d3e8a2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION maintenance_require_contract_xsdd_owner(
            raw_value text,
            requested_project_id text
        ) RETURNS void
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            identity text := maintenance_normalize_xsdd(raw_value);
            contract_projects text[];
            mapped_project_id text;
        BEGIN
            IF identity = '' THEN
                RETURN;
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtextextended('maintenance-project-xsdd:' || identity, 0)
            );
            SELECT array_agg(DISTINCT project_id ORDER BY project_id)
            INTO contract_projects
            FROM maintenance_project_contract
            WHERE maintenance_normalize_xsdd(contract_no) = identity;
            IF contract_projects IS NULL
               OR cardinality(contract_projects) <> 1
               OR contract_projects[1] <> requested_project_id THEN
                RAISE EXCEPTION
                    'XSDD % has no unique matching sales contract owner', raw_value
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_assignment_contract_owner';
            END IF;
            SELECT project_id INTO mapped_project_id
            FROM maintenance_project_xsdd
            WHERE xsdd_norm = identity
            FOR UPDATE;
            IF mapped_project_id IS NULL
               OR mapped_project_id <> requested_project_id THEN
                RAISE EXCEPTION
                    'XSDD % mapping does not match sales contract owner', raw_value
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_assignment_contract_owner';
            END IF;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maintenance_assignment_claim_xsdd_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            raw_xsdd text;
        BEGIN
            IF NEW.is_active IS NOT TRUE THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND OLD.is_active IS TRUE
               AND NEW.project_id = OLD.project_id
               AND NEW.source_order_id = OLD.source_order_id THEN
                RETURN NEW;
            END IF;
            SELECT linked_sales_order_no INTO raw_xsdd
            FROM f_maintenance_order
            WHERE raw_order_id = NEW.source_order_id;
            PERFORM maintenance_require_contract_xsdd_owner(
                raw_xsdd, NEW.project_id
            );
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maintenance_order_claim_xsdd_trigger()
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
                PERFORM maintenance_require_contract_xsdd_owner(
                    NEW.linked_sales_order_no, owner_project_id
                );
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maintenance_assignment_claim_xsdd_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            raw_xsdd text;
        BEGIN
            IF NEW.is_active IS NOT TRUE THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND OLD.is_active IS TRUE
               AND NEW.project_id = OLD.project_id
               AND NEW.source_order_id = OLD.source_order_id THEN
                RETURN NEW;
            END IF;
            SELECT linked_sales_order_no INTO raw_xsdd
            FROM f_maintenance_order
            WHERE raw_order_id = NEW.source_order_id;
            PERFORM maintenance_claim_project_xsdd(
                raw_xsdd, NEW.project_id, 'assignment_trigger'
            );
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maintenance_order_claim_xsdd_trigger()
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
        "DROP FUNCTION IF EXISTS "
        "maintenance_require_contract_xsdd_owner(text, text)"
    )
