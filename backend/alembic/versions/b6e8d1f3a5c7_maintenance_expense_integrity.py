"""maintenance expense integrity foundation

Revision ID: b6e8d1f3a5c7
Revises: a9c4e7b2d6f1

Database groundwork for expense integrity (K3-G):

- widens ``expense_id`` to varchar(128) so ``bxd:`` + the 80-char raw line id
  always fits;
- adds the exact 1:1 raw link ``raw_expense_line_id`` ->
  ``f_project_expense.raw_line_id`` (ON DELETE RESTRICT, deferrable);
- adds ``tax_basis`` (default ``default_ex``) and makes the dual-tax invariant
  basis-aware, identical in shape to the raw table;
- allows signed money (reversed/corrected expenses) within +/-1e12;
- adds the ownership axis (``ownership_mapping_state``/
  ``ownership_mapping_version``), strictly separate from the approval axis;
- enforces attribution.project_id == contract.project_id through a composite
  FK backed by a new composite unique on ``maintenance_project_contract``.

Data backfill (read-only against production invariants, verified before
writing): every existing attribution links exactly via ``bxd:`` + raw_line_id,
mirrors tax_basis/amounts from the raw row, then resolves historical ownership
by normalized contract_no + expense_date effective window.  A unique candidate
on the same project maps; zero candidates stay unmapped; multiple candidates
become ambiguous; a unique candidate on a *different* project fails the
migration closed.  No contract is invented and no ambiguous row is guessed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b6e8d1f3a5c7"
down_revision: str | None = "a9c4e7b2d6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OWNERSHIP_VERSION = "ownership-v1"
_STATUS_VERSION = "expense-integrity-v1"
# Normalized contract_no: trimmed, whitespace-free, uppercased, without a
# leading XSDD- prefix.  Keep in sync with
# app.services.maintenance_expense_integrity.normalize_contract_no.
_NORM_CONTRACT_SQL = (
    "regexp_replace(upper(regexp_replace(btrim({expr}), '\\s+', '', 'g')),"
    " '^XSDD-', '')"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # This migration derives attribution rows from raw expenses and contract
    # windows, then invalidates workbook tokens.  Freeze every participating
    # writer table before the first schema/data change so the backfill observes
    # one coherent fact set.  The order exactly follows the application's
    # workbook-state -> project -> contract -> attribution -> raw-expense order.
    # EXCLUSIVE on state blocks new SELECT FOR UPDATE owners; ACCESS EXCLUSIVE on
    # attribution is the DDL lock this migration will require anyway.  A busy
    # deployment therefore fails at the 5s gate instead of entering a
    # row-lock/table-lock upgrade cycle.  Release procedure must still stop all
    # app/import writers and verify no business write transaction is active.
    op.execute(
        """
        LOCK TABLE maintenance_project_workbook_state
        IN EXCLUSIVE MODE
        """
    )
    op.execute(
        """
        LOCK TABLE maintenance_project, maintenance_project_contract
        IN SHARE ROW EXCLUSIVE MODE
        """
    )
    op.execute(
        """
        LOCK TABLE maintenance_project_expense_attribution
        IN ACCESS EXCLUSIVE MODE
        """
    )
    op.execute(
        """
        LOCK TABLE f_project_expense
        IN SHARE ROW EXCLUSIVE MODE
        """
    )

    # 1. Relax/widen before any data rewrite.
    op.alter_column(
        "maintenance_project_expense_attribution",
        "expense_id",
        existing_type=sa.String(64),
        type_=sa.String(128),
    )
    op.add_column(
        "maintenance_project_expense_attribution",
        sa.Column("raw_expense_line_id", sa.String(80), nullable=True),
    )
    op.add_column(
        "maintenance_project_expense_attribution",
        sa.Column(
            "tax_basis",
            sa.String(16),
            server_default="default_ex",
            nullable=False,
        ),
    )
    op.add_column(
        "maintenance_project_expense_attribution",
        sa.Column(
            "ownership_mapping_state",
            sa.String(16),
            server_default="unmapped",
            nullable=False,
        ),
    )
    op.add_column(
        "maintenance_project_expense_attribution",
        sa.Column("ownership_mapping_version", sa.String(64), nullable=True),
    )

    # 2. Exact link: expense_id = 'bxd:' + raw_line_id.  Any prefixed row that
    # does not join exactly fails the migration instead of being guessed.
    op.execute(
        """
        UPDATE maintenance_project_expense_attribution
        SET raw_expense_line_id = substring(expense_id from 5)
        WHERE expense_id LIKE 'bxd:%' AND raw_expense_line_id IS NULL
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1
              FROM maintenance_project_expense_attribution a
              WHERE a.raw_expense_line_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM f_project_expense r
                    WHERE r.raw_line_id = a.raw_expense_line_id
                )
          ) THEN
            RAISE EXCEPTION
              'b6e8d1f3a5c7 upgrade blocked: bxd attribution without exact f_project_expense raw_line_id match';
          END IF;
          IF EXISTS (
              SELECT 1
              FROM maintenance_project_expense_attribution
              WHERE raw_expense_line_id IS NOT NULL
              GROUP BY raw_expense_line_id
              HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'b6e8d1f3a5c7 upgrade blocked: duplicate raw_expense_line_id across attributions';
          END IF;
        END
        $migration$;
        """
    )

    # 3. Drop the old unsigned/single-basis checks before mirroring raw facts
    # (raw may legitimately carry negative or inc-basis amounts).
    op.drop_constraint(
        "ck_maintenance_project_expense_dual_tax_amounts",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_amount_inc_tax",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_amount",
        "maintenance_project_expense_attribution",
        type_="check",
    )

    # 4. A raw-backed attribution is a projection, never an independent copy.
    # Fail closed on an incomplete raw fact, then rebuild every mirrored field
    # (including workflow status) before ownership is resolved.  The old loader
    # did not keep these columns synchronized, so preserving any attribution
    # value here could leave an already-void raw expense counting on a card.
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1
              FROM maintenance_project_expense_attribution a
              JOIN f_project_expense r
                ON r.raw_line_id = a.raw_expense_line_id
              WHERE r.expense_date IS NULL
                 OR r.amount_ex_tax IS NULL
                 OR r.amount_inc_tax IS NULL
          ) THEN
            RAISE EXCEPTION
              'b6e8d1f3a5c7 upgrade blocked: raw-backed expense lacks date or dual-tax amounts';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        f"""
        UPDATE maintenance_project_expense_attribution a
        SET expense_ref = CASE
                WHEN r.bxd_no IS NOT NULL AND r.line_no IS NOT NULL
                  THEN r.bxd_no || '#' || r.line_no::text
                ELSE coalesce(r.bxd_no, r.raw_line_id)
            END,
            expense_date = r.expense_date,
            applicant = r.person,
            category = coalesce(r.fee_category, r.expense_type),
            expense_reason = r.reason,
            tax_basis = r.tax_basis,
            amount_ex_tax = r.amount_ex_tax,
            amount_inc_tax = r.amount_inc_tax,
            tax_rate_used = r.tax_rate_used,
            raw_status = coalesce(r.data_status, ''),
            status_mapping_state = CASE
                WHEN r.data_status IN ('已作废', '作废', '已结束')
                  THEN 'mapped'
                ELSE 'unmapped'
            END,
            normalized_status = CASE
                WHEN r.data_status IN ('已作废', '作废') THEN 'void'
                WHEN r.data_status = '已结束' THEN 'approved'
                ELSE 'unknown'
            END,
            status_mapping_version = '{_STATUS_VERSION}'
        FROM f_project_expense r
        WHERE a.raw_expense_line_id = r.raw_line_id
        """
    )

    # 5. Historical ownership by normalized contract_no + expense_date window.
    norm_attribution_contract = _NORM_CONTRACT_SQL.format(
        expr="e.linked_sales_order_no"
    )
    norm_contract_no = _NORM_CONTRACT_SQL.format(expr="c.contract_no")
    op.execute(
        f"""
        CREATE TEMPORARY TABLE expense_ownership_candidates ON COMMIT DROP AS
        SELECT DISTINCT
            a.expense_id,
            a.project_id,
            c.project_contract_id,
            c.project_id AS contract_project_id
        FROM maintenance_project_expense_attribution a
        JOIN f_project_expense e
          ON e.raw_line_id = a.raw_expense_line_id
        JOIN maintenance_project_contract c
          ON {norm_contract_no} = {norm_attribution_contract}
         AND e.expense_date >= c.effective_from
         AND (c.effective_to IS NULL OR e.expense_date < c.effective_to)
        """
    )
    # Fail closed: exactly one candidate but on another project.
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1
              FROM (
                  SELECT expense_id, project_id,
                         count(*) AS candidate_count,
                         min(contract_project_id) AS only_project_id
                  FROM expense_ownership_candidates
                  GROUP BY expense_id, project_id
              ) t
              WHERE t.candidate_count = 1
                AND t.only_project_id <> t.project_id
          ) THEN
            RAISE EXCEPTION
              'b6e8d1f3a5c7 upgrade blocked: unique ownership candidate belongs to a different project';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        f"""
        UPDATE maintenance_project_expense_attribution a
        SET project_contract_id = t.project_contract_id,
            ownership_mapping_state = 'mapped',
            ownership_mapping_version = '{_OWNERSHIP_VERSION}'
        FROM (
            SELECT expense_id, min(project_contract_id) AS project_contract_id
            FROM expense_ownership_candidates
            GROUP BY expense_id
            HAVING count(*) = 1
        ) t
        WHERE a.expense_id = t.expense_id
        """
    )
    op.execute(
        f"""
        UPDATE maintenance_project_expense_attribution a
        SET project_contract_id = NULL,
            ownership_mapping_state = 'ambiguous',
            ownership_mapping_version = '{_OWNERSHIP_VERSION}'
        WHERE a.expense_id IN (
            SELECT expense_id
            FROM expense_ownership_candidates
            GROUP BY expense_id
            HAVING count(*) > 1
        )
        """
    )
    op.execute(
        f"""
        UPDATE maintenance_project_expense_attribution a
        SET project_contract_id = NULL,
            ownership_mapping_state = 'unmapped',
            ownership_mapping_version = '{_OWNERSHIP_VERSION}'
        WHERE a.raw_expense_line_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM expense_ownership_candidates t
              WHERE t.expense_id = a.expense_id
          )
        """
    )
    op.execute("DROP TABLE expense_ownership_candidates")

    # Every raw-backed attribution gained a stable FK and was rebuilt from its
    # source of truth.  Invalidate each affected project's outstanding workbook
    # token once and advance the row OCC version once for this migration.
    op.execute(
        """
        UPDATE maintenance_project_expense_attribution
        SET version = version + 1,
            updated_at = now()
        WHERE raw_expense_line_id IS NOT NULL
        """
    )
    # PostgreSQL core sha256(bytea) keeps this a single atomic UPDATE and also
    # makes ``alembic upgrade ... --sql`` renderable for release review.  UPDATE
    # expressions read the pre-update revision, so both columns advance to the
    # same canonical token ``sha256(project_id:new_revision)``.
    op.execute(
        """
        UPDATE maintenance_project_workbook_state s
        SET revision = s.revision + 1,
            data_version = encode(
                sha256(convert_to(
                    s.project_id || ':' || (s.revision + 1)::text,
                    'UTF8'
                )),
                'hex'
            ),
            updated_at = now()
        WHERE EXISTS (
            SELECT 1
            FROM maintenance_project_expense_attribution a
            WHERE a.project_id = s.project_id
              AND a.raw_expense_line_id IS NOT NULL
        )
        """
    )

    # 6. Constraints after the data is known-good.
    op.create_unique_constraint(
        "uq_maintenance_project_expense_raw_line",
        "maintenance_project_expense_attribution",
        ["raw_expense_line_id"],
    )
    op.create_foreign_key(
        "fk_maintenance_project_expense_raw_line",
        "maintenance_project_expense_attribution",
        "f_project_expense",
        ["raw_expense_line_id"],
        ["raw_line_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    # Composite uniqueness is trivially implied by the contract primary key;
    # declaring it lets the composite FK below enforce project consistency.
    op.create_unique_constraint(
        "uq_maintenance_project_contract_project",
        "maintenance_project_contract",
        ["project_id", "project_contract_id"],
    )
    op.create_foreign_key(
        "fk_maintenance_project_expense_contract_project",
        "maintenance_project_expense_attribution",
        "maintenance_project_contract",
        ["project_id", "project_contract_id"],
        ["project_id", "project_contract_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_tax_basis",
        "maintenance_project_expense_attribution",
        "tax_basis IN ('default_ex', 'ex', 'inc')",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_ownership_state",
        "maintenance_project_expense_attribution",
        "ownership_mapping_state IN ('mapped', 'unmapped', 'ambiguous')",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_ownership_version",
        "maintenance_project_expense_attribution",
        "ownership_mapping_version IS NULL OR "
        "char_length(btrim(ownership_mapping_version)) > 0",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_raw_mapped_contract",
        "maintenance_project_expense_attribution",
        "raw_expense_line_id IS NULL OR ownership_mapping_state <> 'mapped' "
        "OR project_contract_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_amount",
        "maintenance_project_expense_attribution",
        "amount_ex_tax > -1000000000000 AND amount_ex_tax < 1000000000000",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_amount_inc_tax",
        "maintenance_project_expense_attribution",
        "amount_inc_tax > -1000000000000 AND amount_inc_tax < 1000000000000",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_dual_tax_amounts",
        "maintenance_project_expense_attribution",
        "(tax_basis IN ('default_ex', 'ex') "
        "AND amount_inc_tax = round(amount_ex_tax * NUMERIC '1.13', 2)) OR "
        "(tax_basis = 'inc' "
        "AND amount_ex_tax = round(amount_inc_tax / NUMERIC '1.13', 2))",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        "ck_maintenance_project_expense_dual_tax_amounts",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_amount_inc_tax",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_amount",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_raw_mapped_contract",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_ownership_version",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_ownership_state",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_tax_basis",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "fk_maintenance_project_expense_contract_project",
        "maintenance_project_expense_attribution",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_maintenance_project_contract_project",
        "maintenance_project_contract",
        type_="unique",
    )
    op.drop_constraint(
        "fk_maintenance_project_expense_raw_line",
        "maintenance_project_expense_attribution",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_maintenance_project_expense_raw_line",
        "maintenance_project_expense_attribution",
        type_="unique",
    )
    # Restoring the pre-integrity unsigned checks requires the data to still
    # satisfy them; like b7d2f4a6c8e1 this fails loudly instead of rewriting
    # accounting facts.
    op.create_check_constraint(
        "ck_maintenance_project_expense_amount",
        "maintenance_project_expense_attribution",
        "amount_ex_tax >= 0 AND amount_ex_tax < 1000000000000",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_amount_inc_tax",
        "maintenance_project_expense_attribution",
        "amount_inc_tax >= 0 AND amount_inc_tax < 1000000000000",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_dual_tax_amounts",
        "maintenance_project_expense_attribution",
        "amount_inc_tax = round(amount_ex_tax * NUMERIC '1.13', 2)",
    )
    op.drop_column(
        "maintenance_project_expense_attribution", "ownership_mapping_version"
    )
    op.drop_column(
        "maintenance_project_expense_attribution", "ownership_mapping_state"
    )
    op.drop_column("maintenance_project_expense_attribution", "tax_basis")
    op.drop_column("maintenance_project_expense_attribution", "raw_expense_line_id")
    op.alter_column(
        "maintenance_project_expense_attribution",
        "expense_id",
        existing_type=sa.String(128),
        type_=sa.String(64),
    )
