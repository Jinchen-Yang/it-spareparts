"""stable project dual-tax costs

Revision ID: b7d2f4a6c8e1
Revises: a4c9e1f2b6d8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b7d2f4a6c8e1"
down_revision: str | None = "a4c9e1f2b6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1
              FROM maintenance_site_issue_line
              WHERE unit_cost IS NOT NULL
                AND cost_amount <> round(quantity * unit_cost, 2)
          )
          THEN
            RAISE EXCEPTION
              'b7d2f4a6c8e1 upgrade blocked: legacy cost_amount does not equal round(quantity * unit_cost, 2)';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1
              FROM maintenance_site_issue_line
              WHERE round(manual_unit_cost * NUMERIC '1.13', 2)
                        >= NUMERIC '1000000000000'
                 OR round(unit_cost * NUMERIC '1.13', 2)
                        >= NUMERIC '1000000000000'
                 OR (
                     unit_cost IS NOT NULL
                     AND round(
                         quantity * round(unit_cost * NUMERIC '1.13', 2),
                         2
                     ) >= NUMERIC '1000000000000'
                 )
          )
          OR EXISTS (
              SELECT 1
              FROM maintenance_project_expense_attribution
              WHERE round(amount_ex_tax * NUMERIC '1.13', 2)
                        >= NUMERIC '1000000000000'
          )
          THEN
            RAISE EXCEPTION
              'b7d2f4a6c8e1 upgrade blocked: ex-tax value cannot be represented as Numeric(14,2) after 13 percent tax';
          END IF;
        END
        $migration$;
        """
    )

    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("manual_unit_cost_inc_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("unit_cost_ex_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("unit_cost_inc_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("cost_amount_ex_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("cost_amount_inc_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column(
            "tax_rate_used",
            sa.Numeric(5, 4),
            server_default=sa.text("0.13"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE maintenance_site_issue_line
        SET manual_unit_cost_inc_tax = CASE
                WHEN manual_unit_cost IS NULL THEN NULL
                ELSE round(manual_unit_cost * NUMERIC '1.13', 2)
            END,
            unit_cost_ex_tax = unit_cost,
            unit_cost_inc_tax = CASE
                WHEN unit_cost IS NULL THEN NULL
                ELSE round(unit_cost * NUMERIC '1.13', 2)
            END,
            cost_amount_ex_tax = cost_amount,
            cost_amount_inc_tax = CASE
                WHEN unit_cost IS NULL THEN NULL
                ELSE round(quantity * round(unit_cost * NUMERIC '1.13', 2), 2)
            END,
            tax_rate_used = NUMERIC '0.13'
        """
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_manual_cost_inc_tax",
        "maintenance_site_issue_line",
        "manual_unit_cost_inc_tax IS NULL OR (manual_unit_cost_inc_tax >= 0 AND manual_unit_cost_inc_tax < 1000000000000)",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_manual_tax_pair",
        "maintenance_site_issue_line",
        "(manual_unit_cost IS NULL AND manual_unit_cost_inc_tax IS NULL) OR "
        "(manual_unit_cost IS NOT NULL AND manual_unit_cost_inc_tax = round(manual_unit_cost * NUMERIC '1.13', 2))",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_line_cost_result_pair",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_cost_result_pair",
        "maintenance_site_issue_line",
        "(cost_source IS NULL AND unit_cost IS NULL AND cost_amount IS NULL "
        "AND unit_cost_ex_tax IS NULL AND unit_cost_inc_tax IS NULL "
        "AND cost_amount_ex_tax IS NULL AND cost_amount_inc_tax IS NULL) OR "
        "(cost_source IS NOT NULL AND unit_cost IS NOT NULL AND cost_amount IS NOT NULL "
        "AND unit_cost_ex_tax IS NOT NULL AND unit_cost_inc_tax IS NOT NULL "
        "AND cost_amount_ex_tax IS NOT NULL AND cost_amount_inc_tax IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_legacy_ex_tax_aliases",
        "maintenance_site_issue_line",
        "unit_cost IS NULL OR (unit_cost = unit_cost_ex_tax AND cost_amount = cost_amount_ex_tax)",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_dual_tax_amounts",
        "maintenance_site_issue_line",
        "unit_cost_ex_tax IS NULL OR ("
        "unit_cost_inc_tax = round(unit_cost_ex_tax * NUMERIC '1.13', 2) "
        "AND cost_amount_ex_tax = round(quantity * unit_cost_ex_tax, 2) "
        "AND cost_amount_inc_tax = round(quantity * unit_cost_inc_tax, 2))",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_tax_rate_used",
        "maintenance_site_issue_line",
        "tax_rate_used = 0.13",
    )

    op.add_column(
        "maintenance_project_expense_attribution",
        sa.Column("amount_inc_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "maintenance_project_expense_attribution",
        sa.Column(
            "tax_rate_used",
            sa.Numeric(5, 4),
            server_default=sa.text("0.13"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE maintenance_project_expense_attribution
        SET amount_inc_tax = round(amount_ex_tax * NUMERIC '1.13', 2),
            tax_rate_used = NUMERIC '0.13'
        """
    )
    op.alter_column(
        "maintenance_project_expense_attribution",
        "amount_inc_tax",
        existing_type=sa.Numeric(14, 2),
        nullable=False,
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
    op.create_check_constraint(
        "ck_maintenance_project_expense_tax_rate_used",
        "maintenance_project_expense_attribution",
        "tax_rate_used = 0.13",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_maintenance_project_expense_tax_rate_used",
        "maintenance_project_expense_attribution",
        type_="check",
    )
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
    op.drop_column("maintenance_project_expense_attribution", "tax_rate_used")
    op.drop_column("maintenance_project_expense_attribution", "amount_inc_tax")

    op.drop_constraint(
        "ck_maintenance_site_issue_line_tax_rate_used",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_line_dual_tax_amounts",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_line_legacy_ex_tax_aliases",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_line_cost_result_pair",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_cost_result_pair",
        "maintenance_site_issue_line",
        "(cost_source IS NULL AND unit_cost IS NULL AND cost_amount IS NULL) OR "
        "(cost_source IS NOT NULL AND unit_cost IS NOT NULL AND cost_amount IS NOT NULL)",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_line_manual_tax_pair",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_line_manual_cost_inc_tax",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.drop_column("maintenance_site_issue_line", "tax_rate_used")
    op.drop_column("maintenance_site_issue_line", "cost_amount_inc_tax")
    op.drop_column("maintenance_site_issue_line", "cost_amount_ex_tax")
    op.drop_column("maintenance_site_issue_line", "unit_cost_inc_tax")
    op.drop_column("maintenance_site_issue_line", "unit_cost_ex_tax")
    op.drop_column("maintenance_site_issue_line", "manual_unit_cost_inc_tax")
