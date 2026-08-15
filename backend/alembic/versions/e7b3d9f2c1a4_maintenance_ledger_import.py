"""maintenance ledger import: raw batch tables, project ledger fields, contract tax-inclusive amount

Revision ID: e7b3d9f2c1a4
Revises: c8e2a4f6b1d3
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e7b3d9f2c1a4"
down_revision = "c8e2a4f6b1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- maintenance_project：台账拥有字段 ----
    op.add_column(
        "maintenance_project",
        sa.Column("business_type", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "maintenance_project",
        sa.Column("cmo_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "maintenance_project",
        sa.Column("salesperson", sa.String(length=64), nullable=True),
    )

    # ---- maintenance_project_contract：台账含税合同额 ----
    op.add_column(
        "maintenance_project_contract",
        sa.Column("amount_inc_tax", sa.Numeric(14, 2), nullable=True),
    )
    op.create_check_constraint(
        "ck_maintenance_project_contract_amount_inc_tax",
        "maintenance_project_contract",
        "amount_inc_tax IS NULL OR (amount_inc_tax >= 0 AND amount_inc_tax < 1000000000000)",
    )

    # ---- milestone：台账批次引用（source=project_manager_xls_v1 二选一必填；
    # 列与 FK 在台账批次表创建后添加，见下方 ----） ----
    op.drop_constraint(
        "ck_maintenance_collection_milestone_batch_source",
        "maintenance_collection_milestone",
        type_="check",
    )

    # ---- 台账导入批次 ----
    op.create_table(
        "maintenance_ledger_import_batch",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("uploaded_by", sa.String(length=64), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("contract_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("plan_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expense_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("issue_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="'pending'", nullable=False),
        sa.Column("report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("applied_by", sa.String(length=64), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_kind IN ('project_manager_xls_v1', 'ledger_template_v1')",
            name="ck_maintenance_ledger_import_source_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_maintenance_ledger_import_status",
        ),
        sa.CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL AND applied_by IS NOT NULL)",
            name="ck_maintenance_ledger_import_applied",
        ),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    op.create_index(
        "ix_maintenance_ledger_import_hash",
        "maintenance_ledger_import_batch",
        ["file_hash"],
    )
    op.create_index(
        "ix_maintenance_ledger_import_uploaded",
        "maintenance_ledger_import_batch",
        ["uploaded_at"],
    )

    # ---- 台账合同行 ----
    op.create_table(
        "maintenance_ledger_contract_row",
        sa.Column("row_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("order_no_raw", sa.String(length=64), nullable=True),
        sa.Column("order_date_raw", sa.String(length=64), nullable=True),
        sa.Column("salesperson_raw", sa.String(length=64), nullable=True),
        sa.Column("business_type_raw", sa.String(length=64), nullable=True),
        sa.Column("project_name_raw", sa.String(length=256), nullable=True),
        sa.Column("maint_start_raw", sa.String(length=64), nullable=True),
        sa.Column("maint_end_raw", sa.String(length=64), nullable=True),
        sa.Column("cmo_raw", sa.String(length=128), nullable=True),
        sa.Column("manager_raw", sa.String(length=128), nullable=True),
        sa.Column("amount_raw", sa.String(length=64), nullable=True),
        sa.Column("collected_raw", sa.String(length=64), nullable=True),
        sa.Column("receivable_raw", sa.String(length=64), nullable=True),
        sa.Column("acceptance_material_raw", sa.Text(), nullable=True),
        sa.Column("acceptance_done_raw", sa.String(length=16), nullable=True),
        sa.Column("acceptance_attachment_raw", sa.String(length=255), nullable=True),
        sa.Column("inspection_time_raw", sa.String(length=64), nullable=True),
        sa.Column("inspection_done_raw", sa.String(length=16), nullable=True),
        sa.Column("order_no", sa.String(length=64), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("business_type", sa.String(length=64), nullable=True),
        sa.Column("project_name", sa.String(length=256), nullable=True),
        sa.Column("project_period_from", sa.Date(), nullable=True),
        sa.Column("project_period_to", sa.Date(), nullable=True),
        sa.Column("cmo", sa.String(length=128), nullable=True),
        sa.Column("manager", sa.String(length=128), nullable=True),
        sa.Column("amount_inc_tax", sa.Numeric(14, 2), nullable=True),
        sa.Column("collected_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("receivable_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("issues", postgresql.ARRAY(sa.String(length=128)), nullable=True),
        sa.CheckConstraint("row_no >= 1", name="ck_maintenance_ledger_contract_row_no"),
        sa.ForeignKeyConstraint(["batch_id"], ["maintenance_ledger_import_batch.batch_id"]),
        sa.PrimaryKeyConstraint("row_id"),
    )
    op.create_index(
        "ix_maintenance_ledger_contract_batch",
        "maintenance_ledger_contract_row",
        ["batch_id"],
    )
    op.create_index(
        "ix_maintenance_ledger_contract_order_no",
        "maintenance_ledger_contract_row",
        ["order_no"],
    )

    # ---- 台账回款计划行 ----
    op.create_table(
        "maintenance_ledger_plan_row",
        sa.Column("row_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("order_no_raw", sa.String(length=64), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("time_raw", sa.String(length=64), nullable=True),
        sa.Column("amount_raw", sa.String(length=64), nullable=True),
        sa.Column("order_no", sa.String(length=64), nullable=True),
        sa.Column("planned_date", sa.Date(), nullable=True),
        sa.Column("date_precision", sa.String(length=8), nullable=True),
        sa.Column("planned_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("issues", postgresql.ARRAY(sa.String(length=128)), nullable=True),
        sa.CheckConstraint(
            "sequence BETWEEN 0 AND 24", name="ck_maintenance_ledger_plan_seq"
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["maintenance_ledger_import_batch.batch_id"]),
        sa.PrimaryKeyConstraint("row_id"),
    )
    op.create_index(
        "ix_maintenance_ledger_plan_batch", "maintenance_ledger_plan_row", ["batch_id"]
    )
    op.create_index(
        "ix_maintenance_ledger_plan_order",
        "maintenance_ledger_plan_row",
        ["order_no", "sequence"],
    )

    # ---- 台账报销归集行 ----
    op.create_table(
        "maintenance_ledger_expense_row",
        sa.Column("row_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("bxd_no_raw", sa.String(length=64), nullable=True),
        sa.Column("person_raw", sa.String(length=64), nullable=True),
        sa.Column("expense_type_raw", sa.String(length=64), nullable=True),
        sa.Column("reason_raw", sa.Text(), nullable=True),
        sa.Column("sales_order_raw", sa.String(length=64), nullable=True),
        sa.Column("project_name_raw", sa.String(length=256), nullable=True),
        sa.Column("sales_order_dup_raw", sa.String(length=64), nullable=True),
        sa.Column("salesperson_raw", sa.String(length=64), nullable=True),
        sa.Column("fee_category_raw", sa.String(length=64), nullable=True),
        sa.Column("amount_raw", sa.String(length=64), nullable=True),
        sa.Column("remark_raw", sa.Text(), nullable=True),
        sa.Column("bxd_no", sa.String(length=64), nullable=True),
        sa.Column("sales_order", sa.String(length=64), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("issues", postgresql.ARRAY(sa.String(length=128)), nullable=True),
        sa.CheckConstraint("row_no >= 1", name="ck_maintenance_ledger_expense_row_no"),
        sa.ForeignKeyConstraint(["batch_id"], ["maintenance_ledger_import_batch.batch_id"]),
        sa.PrimaryKeyConstraint("row_id"),
    )
    op.create_index(
        "ix_maintenance_ledger_expense_batch", "maintenance_ledger_expense_row", ["batch_id"]
    )
    op.create_index(
        "ix_maintenance_ledger_expense_bxd", "maintenance_ledger_expense_row", ["bxd_no"]
    )

    # ---- milestone 引用台账批次（批次表已存在）----
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column("ledger_batch_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_maintenance_collection_milestone_ledger_batch",
        "maintenance_collection_milestone",
        "maintenance_ledger_import_batch",
        ["ledger_batch_id"],
        ["batch_id"],
    )
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_batch_source",
        "maintenance_collection_milestone",
        "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL "
        "AND collection_plan_import_batch_id IS NULL AND ledger_batch_id IS NULL) OR "
        "(source = 'project_manager_xls_v1' AND ("
        "(collection_plan_import_batch_id IS NOT NULL AND source_batch_id IS NULL "
        "AND ledger_batch_id IS NULL) OR "
        "(ledger_batch_id IS NOT NULL AND collection_plan_import_batch_id IS NULL "
        "AND source_batch_id IS NULL))) OR "
        "(source = 'direct_api' AND source_batch_id IS NULL "
        "AND collection_plan_import_batch_id IS NULL AND ledger_batch_id IS NULL)",
    )

    # ---- 权限：台账导入应用（admin/boss 默认开启，其余失败关闭） ----
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END
            || jsonb_build_object(
                'action_maintenance_ledger_import',
                code IN ('admin', 'boss')
            )
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
                'action_maintenance_ledger_import',
                COALESCE(template_code, role) IN ('admin', 'boss')
            ),
            perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides
                    ELSE '{}'::jsonb
                END
                - 'action_maintenance_ledger_import'
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
                'action_maintenance_ledger_import',
                role IN ('admin', 'boss')
            )
        WHERE permissions IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = permissions - 'action_maintenance_ledger_import'
        WHERE jsonb_typeof(permissions) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms - 'action_maintenance_ledger_import',
            perm_overrides = CASE
                WHEN jsonb_typeof(perm_overrides) = 'object'
                THEN perm_overrides - 'action_maintenance_ledger_import'
                ELSE perm_overrides
            END
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET permissions = permissions - 'action_maintenance_ledger_import'
        WHERE permissions IS NOT NULL
        """
    )
    op.drop_constraint(
        "ck_maintenance_collection_milestone_batch_source",
        "maintenance_collection_milestone",
        type_="check",
    )
    op.drop_constraint(
        "fk_maintenance_collection_milestone_ledger_batch",
        "maintenance_collection_milestone",
        type_="foreignkey",
    )
    op.drop_column("maintenance_collection_milestone", "ledger_batch_id")
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_batch_source",
        "maintenance_collection_milestone",
        "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL "
        "AND collection_plan_import_batch_id IS NULL) OR "
        "(source = 'project_manager_xls_v1' AND collection_plan_import_batch_id IS NOT NULL "
        "AND source_batch_id IS NULL) OR "
        "(source = 'direct_api' AND source_batch_id IS NULL "
        "AND collection_plan_import_batch_id IS NULL)",
    )
    op.drop_index("ix_maintenance_ledger_expense_bxd", table_name="maintenance_ledger_expense_row")
    op.drop_index("ix_maintenance_ledger_expense_batch", table_name="maintenance_ledger_expense_row")
    op.drop_table("maintenance_ledger_expense_row")
    op.drop_index("ix_maintenance_ledger_plan_order", table_name="maintenance_ledger_plan_row")
    op.drop_index("ix_maintenance_ledger_plan_batch", table_name="maintenance_ledger_plan_row")
    op.drop_table("maintenance_ledger_plan_row")
    op.drop_index("ix_maintenance_ledger_contract_order_no", table_name="maintenance_ledger_contract_row")
    op.drop_index("ix_maintenance_ledger_contract_batch", table_name="maintenance_ledger_contract_row")
    op.drop_table("maintenance_ledger_contract_row")
    op.drop_index("ix_maintenance_ledger_import_uploaded", table_name="maintenance_ledger_import_batch")
    op.drop_index("ix_maintenance_ledger_import_hash", table_name="maintenance_ledger_import_batch")
    op.drop_table("maintenance_ledger_import_batch")
    op.drop_constraint(
        "ck_maintenance_project_contract_amount_inc_tax",
        "maintenance_project_contract",
        type_="check",
    )
    op.drop_column("maintenance_project_contract", "amount_inc_tax")
    op.drop_column("maintenance_project", "salesperson")
    op.drop_column("maintenance_project", "cmo_name")
    op.drop_column("maintenance_project", "business_type")
