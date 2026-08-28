"""canonical XSDD project identity and display aliases

Revision ID: d2c7e9f1a4b6
Revises: b6e8d1f3a5c7

The migration backfills only XSDDs whose existing assignment/contract evidence
points to exactly one project.  Historical conflicts remain unmapped and every
new cross-project claim fails closed; no project facts are moved here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d2c7e9f1a4b6"
down_revision: str | None = "b6e8d1f3a5c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "maintenance_project_xsdd",
        sa.Column("xsdd_norm", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("maintenance_project.project_id"),
            nullable=False,
        ),
        sa.Column("source", sa.String(64), nullable=False),
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
            "char_length(btrim(xsdd_norm)) > 0",
            name="ck_maintenance_project_xsdd_norm",
        ),
        sa.CheckConstraint(
            "char_length(btrim(source)) > 0",
            name="ck_maintenance_project_xsdd_source",
        ),
    )
    op.create_index(
        "ix_maintenance_project_xsdd_project",
        "maintenance_project_xsdd",
        ["project_id"],
    )
    op.create_table(
        "maintenance_project_alias",
        sa.Column("alias_id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("maintenance_project.project_id"),
            nullable=False,
        ),
        sa.Column("alias_name", sa.String(256), nullable=False),
        sa.Column("alias_key", sa.String(256), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(alias_name)) > 0",
            name="ck_maintenance_project_alias_name",
        ),
        sa.CheckConstraint(
            "char_length(btrim(alias_key)) > 0",
            name="ck_maintenance_project_alias_key",
        ),
        sa.CheckConstraint(
            "char_length(btrim(source)) > 0",
            name="ck_maintenance_project_alias_source",
        ),
        sa.UniqueConstraint(
            "project_id",
            "alias_key",
            name="uq_maintenance_project_alias_identity",
        ),
    )
    op.create_index(
        "ix_maintenance_project_alias_project",
        "maintenance_project_alias",
        ["project_id"],
    )
    op.create_index(
        "ix_maintenance_project_alias_key",
        "maintenance_project_alias",
        ["alias_key"],
    )
    op.create_index(
        "ix_maintenance_project_alias_name_trgm",
        "maintenance_project_alias",
        ["alias_name"],
        postgresql_using="gin",
        postgresql_ops={"alias_name": "gin_trgm_ops"},
    )

    # Every existing primary display name is also a searchable alias.  The
    # normalized key matches project_names.display_name_identity for the
    # whitespace/case variants relevant to stored project names.
    op.execute(
        """
        INSERT INTO maintenance_project_alias
            (alias_id, project_id, alias_name, alias_key, source)
        SELECT gen_random_uuid()::text,
               project_id,
               regexp_replace(btrim(display_name), '\\s+', ' ', 'g'),
               lower(regexp_replace(btrim(display_name), '\\s+', ' ', 'g')),
               'migration_primary_name'
        FROM maintenance_project
        WHERE btrim(display_name) <> ''
        ON CONFLICT (project_id, alias_key) DO NOTHING
        """
    )

    # One normalization function is shared by triggers and the backfill.  Keep
    # it identical to maintenance_expense_integrity.normalize_contract_no.
    op.execute(
        """
        CREATE FUNCTION maintenance_normalize_xsdd(raw_value text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        RETURN CASE
            WHEN regexp_replace(
                upper(regexp_replace(btrim(coalesce(raw_value, '')), '\\s+', '', 'g')),
                '^XSDD-',
                ''
            ) ~ '^[0-9]{8}-[0-9]{4}$'
            THEN regexp_replace(
                upper(regexp_replace(btrim(coalesce(raw_value, '')), '\\s+', '', 'g')),
                '^XSDD-',
                ''
            )
            ELSE ''
        END
        """
    )

    # Only unambiguous identities are safe to backfill.  The six known
    # production conflicts therefore remain absent and visible to preview.
    op.execute(
        """
        WITH evidence AS (
            SELECT maintenance_normalize_xsdd(o.linked_sales_order_no) AS xsdd_norm,
                   a.project_id
            FROM f_maintenance_order o
            JOIN maintenance_source_order_assignment a
              ON a.source_order_id = o.raw_order_id
             AND a.is_active IS TRUE
            WHERE maintenance_normalize_xsdd(o.linked_sales_order_no) <> ''
            UNION
            SELECT maintenance_normalize_xsdd(c.contract_no), c.project_id
            FROM maintenance_project_contract c
            WHERE maintenance_normalize_xsdd(c.contract_no) <> ''
        ), unambiguous AS (
            SELECT xsdd_norm, min(project_id) AS project_id
            FROM evidence
            GROUP BY xsdd_norm
            HAVING count(DISTINCT project_id) = 1
        )
        INSERT INTO maintenance_project_xsdd (xsdd_norm, project_id, source)
        SELECT xsdd_norm, project_id, 'migration_unambiguous_evidence'
        FROM unambiguous
        ON CONFLICT (xsdd_norm) DO NOTHING
        """
    )

    # Shared trigger helper: check all pre-existing evidence before claiming.
    # SQLSTATE 23514 lets APIs translate the invariant violation to 409/422
    # instead of leaking a generic database 500.
    op.execute(
        """
        CREATE FUNCTION maintenance_claim_project_xsdd(
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
                SELECT a.project_id
                FROM f_maintenance_order o
                JOIN maintenance_source_order_assignment a
                  ON a.source_order_id = o.raw_order_id
                 AND a.is_active IS TRUE
                WHERE maintenance_normalize_xsdd(o.linked_sales_order_no) = identity
                UNION
                SELECT c.project_id
                FROM maintenance_project_contract c
                WHERE maintenance_normalize_xsdd(c.contract_no) = identity
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
        CREATE FUNCTION maintenance_contract_claim_xsdd_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND NEW.project_id = OLD.project_id
               AND maintenance_normalize_xsdd(NEW.contract_no)
                   = maintenance_normalize_xsdd(OLD.contract_no) THEN
                RETURN NEW;
            END IF;
            PERFORM maintenance_claim_project_xsdd(
                NEW.contract_no, NEW.project_id, 'contract_trigger'
            );
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_contract_claim_xsdd
        BEFORE INSERT OR UPDATE OF project_id, contract_no
        ON maintenance_project_contract
        FOR EACH ROW EXECUTE FUNCTION maintenance_contract_claim_xsdd_trigger()
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_assignment_claim_xsdd_trigger()
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
        CREATE TRIGGER trg_maintenance_assignment_claim_xsdd
        BEFORE INSERT OR UPDATE OF project_id, source_order_id, is_active
        ON maintenance_source_order_assignment
        FOR EACH ROW EXECUTE FUNCTION maintenance_assignment_claim_xsdd_trigger()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_maintenance_assignment_claim_xsdd "
        "ON maintenance_source_order_assignment"
    )
    op.execute("DROP FUNCTION IF EXISTS maintenance_assignment_claim_xsdd_trigger()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_maintenance_contract_claim_xsdd "
        "ON maintenance_project_contract"
    )
    op.execute("DROP FUNCTION IF EXISTS maintenance_contract_claim_xsdd_trigger()")
    op.execute("DROP FUNCTION IF EXISTS maintenance_claim_project_xsdd(text, text, text)")
    op.execute("DROP FUNCTION IF EXISTS maintenance_normalize_xsdd(text)")
    op.drop_index("ix_maintenance_project_alias_name_trgm", table_name="maintenance_project_alias")
    op.drop_index("ix_maintenance_project_alias_key", table_name="maintenance_project_alias")
    op.drop_index("ix_maintenance_project_alias_project", table_name="maintenance_project_alias")
    op.drop_table("maintenance_project_alias")
    op.drop_index("ix_maintenance_project_xsdd_project", table_name="maintenance_project_xsdd")
    op.drop_table("maintenance_project_xsdd")
