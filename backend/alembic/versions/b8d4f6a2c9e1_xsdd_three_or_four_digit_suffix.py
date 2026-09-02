"""allow three- or four-digit XSDD sequence suffixes

Revision ID: b8d4f6a2c9e1
Revises: a7c2e9f4b1d6
"""

from collections.abc import Sequence

from alembic import op


revision: str = "b8d4f6a2c9e1"
down_revision: str | None = "a7c2e9f4b1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_normalizer(sequence_pattern: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION maintenance_normalize_xsdd(raw_value text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        RETURN CASE
            WHEN regexp_replace(
                upper(regexp_replace(btrim(coalesce(raw_value, '')), '\\s+', '', 'g')),
                '^XSDD-',
                ''
            ) ~ '^[0-9]{{8}}-{sequence_pattern}$'
            THEN regexp_replace(
                upper(regexp_replace(btrim(coalesce(raw_value, '')), '\\s+', '', 'g')),
                '^XSDD-',
                ''
            )
            ELSE ''
        END
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The previous application version ignores three-digit identities and
    # therefore takes no XSDD advisory lock for them.  Freeze the small set of
    # evidence tables while normalization and the one-time backfill change as
    # one transaction; timeout instead of taking a stale snapshot.
    op.execute(
        """
        LOCK TABLE maintenance_project,
                   maintenance_project_contract,
                   f_maintenance_order,
                   maintenance_source_order_assignment,
                   maintenance_demand_tombstone,
                   maintenance_project_xsdd
        IN SHARE MODE
        """
    )
    _replace_normalizer("[0-9]{3,4}")
    # d2's original backfill ran while the shared normalizer accepted only a
    # four-digit suffix.  Re-evaluate the newly valid three-digit identities,
    # but keep sales contracts as the required claim root and leave every
    # ambiguous/inactive identity unmapped for reviewed repair.
    op.execute(
        """
        WITH evidence AS (
            SELECT maintenance_normalize_xsdd(contract.contract_no) AS xsdd_norm,
                   contract.project_id,
                   true AS is_contract
            FROM maintenance_project_contract AS contract
            WHERE maintenance_normalize_xsdd(contract.contract_no)
                      ~ '^[0-9]{8}-[0-9]{3}$'
            UNION ALL
            SELECT maintenance_normalize_xsdd(
                       maintenance_order.linked_sales_order_no
                   ) AS xsdd_norm,
                   assignment.project_id,
                   false AS is_contract
            FROM f_maintenance_order AS maintenance_order
            JOIN maintenance_source_order_assignment AS assignment
              ON assignment.source_order_id = maintenance_order.raw_order_id
             AND assignment.is_active IS TRUE
            WHERE maintenance_order.data_status = '已生效'
              AND NOT EXISTS (
                  SELECT 1
                  FROM maintenance_demand_tombstone AS tombstone
                  WHERE tombstone.source_order_id = maintenance_order.raw_order_id
                    AND tombstone.restored_at IS NULL
              )
              AND maintenance_normalize_xsdd(
                      maintenance_order.linked_sales_order_no
                  ) ~ '^[0-9]{8}-[0-9]{3}$'
        ), candidates AS (
            SELECT evidence.xsdd_norm,
                   min(evidence.project_id) AS project_id
            FROM evidence
            JOIN maintenance_project AS project
              ON project.project_id = evidence.project_id
            GROUP BY evidence.xsdd_norm
            HAVING bool_or(evidence.is_contract)
               AND count(DISTINCT evidence.project_id) = 1
               AND bool_and(project.is_active IS TRUE)
        )
        INSERT INTO maintenance_project_xsdd (xsdd_norm, project_id, source)
        SELECT xsdd_norm,
               project_id,
               'migration_three_digit_contract_evidence'
        FROM candidates
        ORDER BY xsdd_norm
        ON CONFLICT (xsdd_norm) DO NOTHING
        """
    )
    # A pre-existing direct map is allowed only when it agrees with the sole
    # contract-backed evidence.  Never overwrite a conflicting owner silently.
    op.execute(
        """
        DO $migration$
        DECLARE
            conflict_xsdd text;
        BEGIN
            WITH evidence AS (
                SELECT maintenance_normalize_xsdd(contract.contract_no) AS xsdd_norm,
                       contract.project_id,
                       true AS is_contract
                FROM maintenance_project_contract AS contract
                WHERE maintenance_normalize_xsdd(contract.contract_no)
                          ~ '^[0-9]{8}-[0-9]{3}$'
                UNION ALL
                SELECT maintenance_normalize_xsdd(
                           maintenance_order.linked_sales_order_no
                       ) AS xsdd_norm,
                       assignment.project_id,
                       false AS is_contract
                FROM f_maintenance_order AS maintenance_order
                JOIN maintenance_source_order_assignment AS assignment
                  ON assignment.source_order_id = maintenance_order.raw_order_id
                 AND assignment.is_active IS TRUE
                WHERE maintenance_order.data_status = '已生效'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM maintenance_demand_tombstone AS tombstone
                      WHERE tombstone.source_order_id = maintenance_order.raw_order_id
                        AND tombstone.restored_at IS NULL
                  )
                  AND maintenance_normalize_xsdd(
                          maintenance_order.linked_sales_order_no
                      ) ~ '^[0-9]{8}-[0-9]{3}$'
            ), summaries AS (
                SELECT evidence.xsdd_norm,
                       min(evidence.project_id) AS project_id,
                       count(DISTINCT evidence.project_id) AS project_count,
                       bool_or(evidence.is_contract) AS has_contract,
                       bool_and(project.is_active IS TRUE) AS all_projects_active
                FROM evidence
                JOIN maintenance_project AS project
                  ON project.project_id = evidence.project_id
                GROUP BY evidence.xsdd_norm
            )
            SELECT mapping.xsdd_norm
            INTO conflict_xsdd
            FROM maintenance_project_xsdd AS mapping
            LEFT JOIN summaries
              ON summaries.xsdd_norm = mapping.xsdd_norm
            WHERE mapping.xsdd_norm ~ '^[0-9]{8}-[0-9]{3}$'
              AND (
                  summaries.xsdd_norm IS NULL
                  OR summaries.has_contract IS NOT TRUE
                  OR summaries.project_count <> 1
                  OR summaries.all_projects_active IS NOT TRUE
                  OR mapping.project_id <> summaries.project_id
              )
            ORDER BY mapping.xsdd_norm
            LIMIT 1;

            IF conflict_xsdd IS NOT NULL THEN
                RAISE EXCEPTION
                    'three-digit XSDD % has a map conflicting with contract evidence',
                    conflict_xsdd
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_maintenance_project_xsdd_single_owner';
            END IF;
        END
        $migration$
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _replace_normalizer("[0-9]{4}")
    # Narrow the normalizer first so a7's evidence-preservation trigger no
    # longer treats three-digit rows as governed identities.  The a7 helper
    # compares its normalized empty value to the literal map key, so suspend
    # that preservation trigger only around removal of maps created here.
    op.execute(
        """
        ALTER TABLE maintenance_project_xsdd
        DISABLE TRIGGER trg_maintenance_xsdd_map_preserve_evidence
        """
    )
    op.execute(
        """
        DELETE FROM maintenance_project_xsdd
        WHERE source = 'migration_three_digit_contract_evidence'
          AND xsdd_norm ~ '^[0-9]{8}-[0-9]{3}$'
        """
    )
    op.execute(
        """
        ALTER TABLE maintenance_project_xsdd
        ENABLE TRIGGER trg_maintenance_xsdd_map_preserve_evidence
        """
    )
