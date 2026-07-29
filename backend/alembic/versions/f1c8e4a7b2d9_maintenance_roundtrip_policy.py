"""maintenance roundtrip, dual-tax expenses and enforced display policy

Revision ID: f1c8e4a7b2d9
Revises: e5f9a2b3c4d5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1c8e4a7b2d9"
down_revision: str | None = "e5f9a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_COST_SQL = (
    "cost_amount IS NOT NULL"
    " AND cost_amount >= 0"
    " AND cost_amount < 1000000000000"
)
_ACTUAL_WITH_MANUAL_SQL = (
    "cost_source IN ('direct', 'month_avg', 'window', 'manual')"
)
_ACTUAL_BEFORE_SQL = "cost_source IN ('direct', 'month_avg', 'window')"
_ESTIMATED_SQL = (
    "cost_source IN ("
    "'pool_purchase', 'pool_sales', 'purchase_history', 'sales_history',"
    " 'sales_ref', 'trace_avg'"
    ")"
)


def _cost_bucket_sql(actual_sources: str) -> str:
    return (
        "CASE"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {actual_sources}"
        " AND cost_tax_basis = 'inc' THEN 1"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {actual_sources}"
        " AND cost_tax_basis = 'ex' THEN 2"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {_ESTIMATED_SQL}"
        " AND cost_tax_basis = 'inc' AND confidence = 'low' THEN 3"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {_ESTIMATED_SQL}"
        " AND cost_tax_basis = 'inc' THEN 4"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {_ESTIMATED_SQL}"
        " AND cost_tax_basis = 'ex' AND confidence = 'low' THEN 5"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {_ESTIMATED_SQL}"
        " AND cost_tax_basis = 'ex' THEN 6"
        " ELSE 0 END"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Permission-center v2 historically serialized an absent mapping in two
    # forms: SQL NULL and JSON ``null``. PostgreSQL's JSONB delete operator
    # rejects scalar JSON ``null`` ("cannot delete from scalar"), so handle
    # each empty form according to that column's runtime meaning: preserve the
    # template fallback sentinel, normalize only mappings that are edited, and
    # fail closed for every other non-object payload.
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
              'f1c8e4a7b2d9 upgrade blocked: permission JSONB payload must be an object, SQL NULL, or JSON null';
          END IF;
        END
        $migration$;
        """
    )

    # File hashes are idempotency keys within an import protocol, not globally.
    # In particular, a historical generic-import success for a roundtrip XLSX
    # must not block the dedicated, signature-validating endpoint.
    op.drop_index("ux_batch_success_hash", table_name="sys_import_batch")
    op.create_index(
        "ux_batch_success_hash",
        "sys_import_batch",
        ["file_type", "file_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'success'"),
    )
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END
            || jsonb_build_object(
                'action_maintenance_roundtrip_apply',
                code IN ('admin', 'boss')
            )
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
                'action_maintenance_roundtrip_apply',
                COALESCE(template_code, role) IN ('admin', 'boss')
            ),
            perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides
                    ELSE '{}'::jsonb
                END
                - 'action_maintenance_roundtrip_apply'
        -- SQL NULL and JSON null both mean "no v2 snapshot": runtime falls
        -- back to role + legacy permissions.  Preserve that sentinel instead
        -- of turning it into a partial snapshot that would deny every old key.
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END
            || jsonb_build_object(
                'action_maintenance_roundtrip_apply',
                role IN ('admin', 'boss')
            )
        WHERE permissions IS NOT NULL
        """
    )

    op.add_column(
        "sys_business_setting",
        sa.Column(
            "purchase_display_basis",
            sa.String(length=8),
            server_default=sa.text("'both'"),
            nullable=False,
        ),
    )
    op.add_column(
        "sys_business_setting",
        sa.Column(
            "sales_display_basis",
            sa.String(length=8),
            server_default=sa.text("'ex'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_sys_business_setting_purchase_display_basis",
        "sys_business_setting",
        "purchase_display_basis IN ('inc', 'ex', 'both')",
    )
    op.create_check_constraint(
        "ck_sys_business_setting_sales_display_basis",
        "sys_business_setting",
        "sales_display_basis IN ('inc', 'ex', 'both')",
    )

    # The predecessor schema stores only one expense amount and cannot prove
    # whether a historical workbook declared that value as tax-inclusive or
    # tax-exclusive.  Silently treating existing rows as ex-tax would corrupt
    # contribution margin when an archived source explicitly said "含税".
    #
    # Production had zero expense facts at the audited DEV-15 cutover
    # preflight.  Keep the migration fail-closed if that invariant drifts:
    # non-empty installations must first audit/replay their archived workbooks
    # with the DEV-15 parser, which preserves explicit basis and defaults only
    # genuinely untyped amounts to ex-tax.
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM f_project_expense) THEN
            RAISE EXCEPTION
              'f1c8e4a7b2d9 upgrade blocked: historical project expenses require archived-workbook tax-basis audit and replay';
          END IF;
        END
        $migration$;
        """
    )

    op.add_column(
        "f_project_expense",
        sa.Column("amount_ex_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "f_project_expense",
        sa.Column("amount_inc_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "f_project_expense",
        sa.Column(
            "tax_basis",
            sa.String(length=16),
            server_default=sa.text("'default_ex'"),
            nullable=False,
        ),
    )
    op.add_column(
        "f_project_expense",
        sa.Column(
            "tax_rate_used",
            sa.Numeric(5, 4),
            server_default=sa.text("0.13"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE f_project_expense
        SET amount_ex_tax = amount,
            amount_inc_tax = CASE
                WHEN amount IS NULL THEN NULL
                ELSE round(amount * 1.13, 2)
            END,
            tax_basis = 'default_ex',
            tax_rate_used = 0.13
        """,
    )
    op.create_check_constraint(
        "ck_project_expense_tax_basis",
        "f_project_expense",
        "tax_basis IN ('default_ex', 'ex', 'inc')",
    )
    op.create_check_constraint(
        "ck_project_expense_tax_rate_used",
        "f_project_expense",
        "tax_rate_used = 0.13",
    )
    op.create_check_constraint(
        "ck_project_expense_tax_amount_presence",
        "f_project_expense",
        """
        (
            amount IS NULL
            AND amount_ex_tax IS NULL
            AND amount_inc_tax IS NULL
        )
        OR (
            amount IS NOT NULL
            AND amount_ex_tax IS NOT NULL
            AND amount_inc_tax IS NOT NULL
        )
        """,
    )
    op.create_check_constraint(
        "ck_project_expense_tax_amounts_match",
        "f_project_expense",
        """
        (amount_ex_tax IS NULL AND amount_inc_tax IS NULL)
        OR (
            tax_basis IN ('default_ex', 'ex')
            AND amount_inc_tax
                = round(amount_ex_tax * NUMERIC '1.13', 2)
        )
        OR (
            tax_basis = 'inc'
            AND amount_ex_tax
                = round(amount_inc_tax / NUMERIC '1.13', 2)
        )
        """,
    )
    op.create_check_constraint(
        "ck_project_expense_amount_matches_basis",
        "f_project_expense",
        """
        (
            amount IS NULL
            AND amount_ex_tax IS NULL
            AND amount_inc_tax IS NULL
        )
        OR (
            tax_basis IN ('default_ex', 'ex')
            AND amount = amount_ex_tax
        )
        OR (
            tax_basis = 'inc'
            AND amount = amount_inc_tax
        )
        """,
    )

    op.create_table(
        "maintenance_manual_cost_override",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("line_id", sa.Integer(), nullable=False),
        sa.Column("unit_cost_ex_tax", sa.Numeric(14, 2), nullable=False),
        sa.Column("unit_cost_inc_tax", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "tax_rate_used",
            sa.Numeric(5, 4),
            server_default=sa.text("0.13"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "unit_cost_ex_tax >= 0 AND unit_cost_ex_tax < 1000000000000",
            name="ck_maintenance_manual_cost_ex",
        ),
        sa.CheckConstraint(
            "unit_cost_inc_tax >= 0 AND unit_cost_inc_tax < 1000000000000",
            name="ck_maintenance_manual_cost_inc",
        ),
        sa.CheckConstraint(
            "tax_rate_used = 0.13",
            name="ck_maintenance_manual_cost_tax_rate",
        ),
        sa.CheckConstraint(
            """
            unit_cost_inc_tax
                = round(unit_cost_ex_tax * NUMERIC '1.13', 2)
            """,
            name="ck_maintenance_manual_cost_tax_amounts_match",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_maintenance_manual_cost_version",
        ),
        sa.ForeignKeyConstraint(
            ["line_id"],
            ["f_maintenance_line.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("line_id"),
    )
    op.create_index(
        "ix_maintenance_manual_cost_override_active",
        "maintenance_manual_cost_override",
        ["active", "line_id"],
        unique=False,
    )

    op.create_table(
        "maintenance_contract_workbook_state",
        sa.Column("contract_no", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("expense_complete_through", sa.Date(), nullable=True),
        sa.Column(
            "expense_snapshot_complete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("last_export_id", sa.String(length=64), nullable=True),
        sa.Column("last_import_batch_id", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_maintenance_contract_workbook_state_revision",
        ),
        sa.ForeignKeyConstraint(
            ["last_import_batch_id"],
            ["sys_import_batch.id"],
        ),
        sa.PrimaryKeyConstraint("contract_no"),
    )
    op.create_index(
        "ix_maintenance_contract_workbook_state_complete",
        "maintenance_contract_workbook_state",
        ["expense_snapshot_complete", "contract_no"],
        unique=False,
    )

    op.create_table(
        "maintenance_roundtrip_operation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("export_id", sa.String(length=36), nullable=False),
        sa.Column("sheet_code", sa.String(length=32), nullable=False),
        sa.Column("client_row_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=8), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("applied_by", sa.String(length=64), nullable=False),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'VOID')",
            name="ck_maintenance_roundtrip_operation_kind",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maintenance_roundtrip_operation_payload_hash",
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["sys_import_batch.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "export_id",
            "sheet_code",
            "client_row_id",
            name="uq_maintenance_roundtrip_operation_key",
        ),
    )
    op.create_index(
        "ix_maintenance_roundtrip_operation_batch",
        "maintenance_roundtrip_operation",
        ["import_batch_id", "id"],
        unique=False,
    )

    # ``manual`` is a first-class, audited actual-cost source. PostgreSQL cannot
    # alter a generated expression in place, so rebuild the cached bucket.
    op.drop_column("f_maintenance_line", "cost_bucket")
    op.add_column(
        "f_maintenance_line",
        sa.Column(
            "cost_bucket",
            sa.SmallInteger(),
            sa.Computed(
                _cost_bucket_sql(_ACTUAL_WITH_MANUAL_SQL),
                persisted=True,
            ),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # DEV-15 adds business facts that have no lossless representation in e5.
    # Refuse the schema downgrade before the first destructive DDL whenever
    # those facts (or their audited workflow) have been used. A release
    # operator must export/replay them explicitly or prefer a forward fix.
    op.execute(
        sa.text(
            """
            DO $migration$
            BEGIN
              IF
                EXISTS (
                    SELECT 1
                    FROM maintenance_manual_cost_override
                )
                OR EXISTS (
                    SELECT 1
                    FROM maintenance_roundtrip_operation
                )
                OR EXISTS (
                    SELECT 1
                    FROM maintenance_contract_workbook_state
                )
                OR EXISTS (
                    SELECT 1
                    FROM f_maintenance_line
                    WHERE cost_source = 'manual'
                )
                OR EXISTS (
                    SELECT 1
                    FROM sys_import_batch
                    WHERE file_type = 'maint_roundtrip'
                      AND status = 'success'
                )
                OR EXISTS (
                    SELECT 1
                    FROM sys_import_batch
                    WHERE status = 'success'
                    GROUP BY file_hash
                    HAVING count(DISTINCT file_type) > 1
                )
                OR EXISTS (
                    SELECT 1
                    FROM sys_audit_log
                    WHERE action = 'business_display_basis_update'
                )
                OR EXISTS (
                    SELECT 1
                    FROM sys_business_setting
                    WHERE purchase_display_basis IS DISTINCT FROM 'both'
                       OR sales_display_basis IS DISTINCT FROM 'ex'
                )
                OR EXISTS (
                    SELECT 1
                    FROM f_project_expense
                    WHERE tax_basis IS DISTINCT FROM 'default_ex'
                       OR tax_rate_used IS DISTINCT FROM 0.13
                       OR amount_ex_tax IS DISTINCT FROM amount
                       OR amount_inc_tax IS DISTINCT FROM CASE
                            WHEN amount IS NULL THEN NULL
                            ELSE round(amount * 1.13, 2)
                          END
                )
                OR EXISTS (
                    SELECT 1 FROM sys_role_template
                    WHERE permissions IS NOT NULL
                      AND permissions <> 'null'::jsonb
                      AND jsonb_typeof(permissions)
                          IS DISTINCT FROM 'object'
                )
                OR EXISTS (
                    SELECT 1 FROM sys_user
                    WHERE template_perms IS NOT NULL
                      AND template_perms <> 'null'::jsonb
                      AND jsonb_typeof(template_perms)
                          IS DISTINCT FROM 'object'
                )
                OR EXISTS (
                    SELECT 1 FROM sys_user
                    WHERE permissions IS NOT NULL
                      AND permissions <> 'null'::jsonb
                      AND jsonb_typeof(permissions)
                          IS DISTINCT FROM 'object'
                )
                OR EXISTS (
                    SELECT 1 FROM sys_user
                    WHERE perm_overrides IS NOT NULL
                      AND perm_overrides <> 'null'::jsonb
                      AND jsonb_typeof(perm_overrides)
                          IS DISTINCT FROM 'object'
                )
              THEN
                RAISE EXCEPTION
                    'f1c8e4a7b2d9 downgrade blocked: DEV-15 business writes would be lost; freeze writes and use the documented export/replay or forward-fix path';
              END IF;
            END
            $migration$;
            """
        )
    )

    op.drop_column("f_maintenance_line", "cost_bucket")
    op.add_column(
        "f_maintenance_line",
        sa.Column(
            "cost_bucket",
            sa.SmallInteger(),
            sa.Computed(_cost_bucket_sql(_ACTUAL_BEFORE_SQL), persisted=True),
            nullable=False,
        ),
    )

    op.drop_index(
        "ix_maintenance_roundtrip_operation_batch",
        table_name="maintenance_roundtrip_operation",
    )
    op.drop_table("maintenance_roundtrip_operation")
    op.drop_index(
        "ix_maintenance_contract_workbook_state_complete",
        table_name="maintenance_contract_workbook_state",
    )
    op.drop_table("maintenance_contract_workbook_state")
    op.drop_index(
        "ix_maintenance_manual_cost_override_active",
        table_name="maintenance_manual_cost_override",
    )
    op.drop_table("maintenance_manual_cost_override")

    for constraint_name in (
        "ck_project_expense_amount_matches_basis",
        "ck_project_expense_tax_amounts_match",
        "ck_project_expense_tax_amount_presence",
        "ck_project_expense_tax_rate_used",
        "ck_project_expense_tax_basis",
    ):
        op.drop_constraint(
            constraint_name,
            "f_project_expense",
            type_="check",
        )
    for name in (
        "tax_rate_used",
        "tax_basis",
        "amount_inc_tax",
        "amount_ex_tax",
    ):
        op.drop_column("f_project_expense", name)

    op.drop_constraint(
        "ck_sys_business_setting_sales_display_basis",
        "sys_business_setting",
        type_="check",
    )
    op.drop_constraint(
        "ck_sys_business_setting_purchase_display_basis",
        "sys_business_setting",
        type_="check",
    )
    op.drop_column("sys_business_setting", "sales_display_basis")
    op.drop_column("sys_business_setting", "purchase_display_basis")

    op.execute(
        """
        UPDATE sys_user
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END - 'action_maintenance_roundtrip_apply'
        WHERE permissions IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms =
                template_perms - 'action_maintenance_roundtrip_apply',
            perm_overrides =
                CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides
                    ELSE '{}'::jsonb
                END - 'action_maintenance_roundtrip_apply'
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions =
            CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END - 'action_maintenance_roundtrip_apply'
        """
    )
    op.drop_index("ux_batch_success_hash", table_name="sys_import_batch")
    op.create_index(
        "ux_batch_success_hash",
        "sys_import_batch",
        ["file_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'success'"),
    )
