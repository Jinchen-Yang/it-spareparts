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
        CREATE FUNCTION maintenance_active_wbdd_owner_projects(raw_value text)
        RETURNS text[]
        LANGUAGE sql
        STABLE
        AS $function$
            SELECT array_agg(DISTINCT assignment.project_id ORDER BY assignment.project_id)
            FROM f_maintenance_order AS maintenance_order
            JOIN maintenance_source_order_assignment AS assignment
              ON assignment.source_order_id = maintenance_order.raw_order_id
             AND assignment.is_active IS TRUE
            WHERE maintenance_normalize_xsdd(
                      maintenance_order.linked_sales_order_no
                  ) = maintenance_normalize_xsdd(raw_value)
              AND maintenance_order.data_status = '已生效'
              AND NOT EXISTS (
                  SELECT 1
                  FROM maintenance_demand_tombstone AS tombstone
                  WHERE tombstone.source_order_id = maintenance_order.raw_order_id
                    AND tombstone.restored_at IS NULL
              )
        $function$
        """
    )
    # Contract remains the claim root, but discarded/tombstoned WBDD cannot
    # veto a later authoritative sales owner.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maintenance_claim_project_xsdd(
            raw_value text,
            requested_project_id text,
            claim_source text
        ) RETURNS void
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            identity text := maintenance_normalize_xsdd(raw_value);
            existing_project_id text;
            evidence_projects text[];
        BEGIN
            IF identity = '' THEN
                RETURN;
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtextextended('maintenance-project-xsdd:' || identity, 0)
            );
            SELECT array_agg(DISTINCT project_id ORDER BY project_id)
            INTO evidence_projects
            FROM (
                SELECT unnest(
                    maintenance_active_wbdd_owner_projects(identity)
                ) AS project_id
                UNION
                SELECT contract.project_id
                FROM maintenance_project_contract AS contract
                WHERE maintenance_normalize_xsdd(contract.contract_no) = identity
            ) evidence;
            IF evidence_projects IS NOT NULL
               AND (cardinality(evidence_projects) > 1
                    OR evidence_projects[1] <> requested_project_id) THEN
                RAISE EXCEPTION
                    'XSDD % already belongs to another or multiple projects', raw_value
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_project_xsdd_single_owner';
            END IF;
            INSERT INTO maintenance_project_xsdd
                (xsdd_norm, project_id, source)
            VALUES (identity, requested_project_id, claim_source)
            ON CONFLICT (xsdd_norm) DO NOTHING;
            SELECT project_id INTO existing_project_id
            FROM maintenance_project_xsdd
            WHERE xsdd_norm = identity
            FOR UPDATE;
            IF existing_project_id <> requested_project_id THEN
                RAISE EXCEPTION 'XSDD % already belongs to project %',
                    raw_value, existing_project_id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_project_xsdd_single_owner';
            END IF;
        END
        $function$
        """
    )
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
    op.execute(
        """
        CREATE FUNCTION maintenance_contract_preserve_wbdd_owner_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            old_identity text := maintenance_normalize_xsdd(OLD.contract_no);
            new_identity text := CASE WHEN TG_OP = 'UPDATE'
                THEN maintenance_normalize_xsdd(NEW.contract_no) ELSE '' END;
            lock_identity text;
            active_projects text[];
            remaining_contract_projects text[];
            mapped_project_id text;
        BEGIN
            IF old_identity = '' OR (
                TG_OP = 'UPDATE'
                AND old_identity = new_identity
                AND OLD.project_id = NEW.project_id
            ) THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            FOR lock_identity IN
                SELECT DISTINCT value
                FROM unnest(ARRAY[old_identity, new_identity]) AS identities(value)
                WHERE value <> ''
                ORDER BY value
            LOOP
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'maintenance-project-xsdd:' || lock_identity, 0
                    )
                );
            END LOOP;
            active_projects := maintenance_active_wbdd_owner_projects(old_identity);
            IF active_projects IS NULL THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF cardinality(active_projects) <> 1 THEN
                RAISE EXCEPTION 'XSDD % has ambiguous active WBDD owners', OLD.contract_no
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_contract_preserves_wbdd_owner';
            END IF;
            SELECT array_agg(DISTINCT project_id ORDER BY project_id)
            INTO remaining_contract_projects
            FROM maintenance_project_contract
            WHERE project_contract_id <> OLD.project_contract_id
              AND maintenance_normalize_xsdd(contract_no) = old_identity;
            SELECT project_id INTO mapped_project_id
            FROM maintenance_project_xsdd
            WHERE xsdd_norm = old_identity
            FOR UPDATE;
            IF remaining_contract_projects IS NULL
               OR cardinality(remaining_contract_projects) <> 1
               OR remaining_contract_projects[1] <> active_projects[1]
               OR mapped_project_id IS NULL
               OR mapped_project_id <> active_projects[1] THEN
                RAISE EXCEPTION
                    'XSDD % contract removal would orphan active WBDD ownership',
                    OLD.contract_no
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_contract_preserves_wbdd_owner';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_contract_00_preserve_wbdd_owner
        BEFORE DELETE OR UPDATE OF project_id, contract_no
        ON maintenance_project_contract
        FOR EACH ROW EXECUTE FUNCTION maintenance_contract_preserve_wbdd_owner_trigger()
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_xsdd_map_preserve_evidence_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            old_identity text := OLD.xsdd_norm;
            new_identity text := CASE WHEN TG_OP = 'UPDATE' THEN NEW.xsdd_norm ELSE '' END;
            lock_identity text;
            old_evidence_projects text[];
            new_evidence_projects text[];
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.xsdd_norm = NEW.xsdd_norm
               AND OLD.project_id = NEW.project_id THEN
                RETURN NEW;
            END IF;
            FOR lock_identity IN
                SELECT DISTINCT value
                FROM unnest(ARRAY[old_identity, new_identity]) AS identities(value)
                WHERE value <> ''
                ORDER BY value
            LOOP
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'maintenance-project-xsdd:' || lock_identity, 0
                    )
                );
            END LOOP;
            SELECT array_agg(DISTINCT project_id ORDER BY project_id)
            INTO old_evidence_projects
            FROM (
                SELECT project_id
                FROM maintenance_project_contract
                WHERE maintenance_normalize_xsdd(contract_no) = old_identity
                UNION
                SELECT unnest(
                    maintenance_active_wbdd_owner_projects(old_identity)
                ) AS project_id
            ) evidence;
            -- A reviewed merge may repoint a stale map to the sole project
            -- already proven by contract/WBDD evidence.  Any other removal or
            -- move while evidence remains is forbidden.
            IF old_evidence_projects IS NOT NULL
               AND NOT (
                   TG_OP = 'UPDATE'
                   AND new_identity = old_identity
                   AND cardinality(old_evidence_projects) = 1
                   AND old_evidence_projects[1] = NEW.project_id
               ) THEN
                RAISE EXCEPTION
                    'XSDD % mapping still has contract or active WBDD evidence',
                    old_identity
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_xsdd_map_preserves_evidence';
            END IF;
            IF TG_OP = 'UPDATE' AND new_identity <> old_identity THEN
                SELECT array_agg(DISTINCT project_id ORDER BY project_id)
                INTO new_evidence_projects
                FROM (
                    SELECT project_id
                    FROM maintenance_project_contract
                    WHERE maintenance_normalize_xsdd(contract_no) = new_identity
                    UNION
                    SELECT unnest(
                        maintenance_active_wbdd_owner_projects(new_identity)
                    ) AS project_id
                ) evidence;
                IF new_evidence_projects IS NOT NULL
                   AND (
                       cardinality(new_evidence_projects) <> 1
                       OR new_evidence_projects[1] <> NEW.project_id
                   ) THEN
                    RAISE EXCEPTION
                        'XSDD % mapping target conflicts with existing evidence',
                        new_identity
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_maintenance_xsdd_map_preserves_evidence';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_xsdd_map_preserve_evidence
        BEFORE DELETE OR UPDATE OF xsdd_norm, project_id
        ON maintenance_project_xsdd
        FOR EACH ROW EXECUTE FUNCTION maintenance_xsdd_map_preserve_evidence_trigger()
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_maintenance_xsdd_map_preserve_evidence "
        "ON maintenance_project_xsdd"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS maintenance_xsdd_map_preserve_evidence_trigger()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_maintenance_contract_00_preserve_wbdd_owner "
        "ON maintenance_project_contract"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS maintenance_contract_preserve_wbdd_owner_trigger()"
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maintenance_claim_project_xsdd(
            raw_value text,
            requested_project_id text,
            claim_source text
        ) RETURNS void
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            identity text := maintenance_normalize_xsdd(raw_value);
            existing_project_id text;
            evidence_projects text[];
        BEGIN
            IF identity = '' THEN
                RETURN;
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtextextended('maintenance-project-xsdd:' || identity, 0)
            );
            SELECT array_agg(DISTINCT project_id ORDER BY project_id)
            INTO evidence_projects
            FROM (
                SELECT assignment.project_id
                FROM f_maintenance_order AS maintenance_order
                JOIN maintenance_source_order_assignment AS assignment
                  ON assignment.source_order_id = maintenance_order.raw_order_id
                 AND assignment.is_active IS TRUE
                WHERE maintenance_normalize_xsdd(
                    maintenance_order.linked_sales_order_no
                ) = identity
                UNION
                SELECT contract.project_id
                FROM maintenance_project_contract AS contract
                WHERE maintenance_normalize_xsdd(contract.contract_no) = identity
            ) evidence;
            IF evidence_projects IS NOT NULL
               AND (cardinality(evidence_projects) > 1
                    OR evidence_projects[1] <> requested_project_id) THEN
                RAISE EXCEPTION
                    'XSDD % already belongs to another or multiple projects', raw_value
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_project_xsdd_single_owner';
            END IF;
            INSERT INTO maintenance_project_xsdd
                (xsdd_norm, project_id, source)
            VALUES (identity, requested_project_id, claim_source)
            ON CONFLICT (xsdd_norm) DO NOTHING;
            SELECT project_id INTO existing_project_id
            FROM maintenance_project_xsdd
            WHERE xsdd_norm = identity
            FOR UPDATE;
            IF existing_project_id <> requested_project_id THEN
                RAISE EXCEPTION 'XSDD % already belongs to project %',
                    raw_value, existing_project_id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_project_xsdd_single_owner';
            END IF;
        END
        $function$
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS maintenance_active_wbdd_owner_projects(text)"
    )
