"""收款单（SKD）导入台账与快照来源扩展（D-16，issue #288）

1. 新表 ``maintenance_collection_receipt``：逐笔实收台账，
   ``UNIQUE (contract_no, receipt_no)`` 让同一收款单跨批次幂等；
2. ``maintenance_collection_snapshot`` 来源新增 ``bulk_import``，与 ``workbook``
   一样必须带 ``import_batch_id``，批量导入写出/覆盖的快照直接指向批次；
3. ``ux_batch_success_hash`` 偏唯一索引排除 ``maint_bulk``：该网关的
   ``file_hash`` 现在保存原件 sha256（原件同时归档到 ``sys_raw_file``），
   同一原件分次勾选提交是合法流程，不能被"同 hash 只许成功一次"挡住。

downgrade 有损：``bulk_import`` 来源的快照退回 ``direct_api``/无批次
（旧 CHECK 不认识新来源），台账表整体删除。

Revision ID: b7d3f9a1c5e2
Revises: a8e4f1c7d3b9
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b7d3f9a1c5e2"
down_revision: str | None = "a8e4f1c7d3b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_collection_receipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_contract_id", sa.String(length=36), nullable=False),
        sa.Column("contract_no", sa.String(length=64), nullable=False),
        sa.Column("receipt_no", sa.String(length=64), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("actual_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "actual_amount >= 0 AND actual_amount < 1000000000000",
            name="ck_maintenance_collection_receipt_amount",
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["sys_import_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_contract_id"],
            ["maintenance_project_contract.project_contract_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "contract_no",
            "receipt_no",
            name="uq_maintenance_collection_receipt_contract_receipt",
        ),
    )
    op.create_index(
        "ix_maintenance_collection_receipt_contract",
        "maintenance_collection_receipt",
        ["project_contract_id", "receipt_date"],
    )
    op.create_index(
        "ix_maintenance_collection_receipt_batch",
        "maintenance_collection_receipt",
        ["import_batch_id"],
    )

    op.drop_constraint(
        "ck_maintenance_collection_import_batch",
        "maintenance_collection_snapshot",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_collection_source",
        "maintenance_collection_snapshot",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_source",
        "maintenance_collection_snapshot",
        "source IN ('legacy', 'direct_api', 'workbook', 'bulk_import')",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_import_batch",
        "maintenance_collection_snapshot",
        "(source IN ('workbook', 'bulk_import') AND import_batch_id IS NOT NULL) OR "
        "(source IN ('legacy', 'direct_api') AND import_batch_id IS NULL)",
    )

    op.drop_index("ux_batch_success_hash", table_name="sys_import_batch")
    op.create_index(
        "ux_batch_success_hash",
        "sys_import_batch",
        ["file_type", "file_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'success' AND file_type <> 'maint_bulk'"),
    )


def downgrade() -> None:
    op.drop_index("ux_batch_success_hash", table_name="sys_import_batch")
    op.create_index(
        "ux_batch_success_hash",
        "sys_import_batch",
        ["file_type", "file_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'success'"),
    )

    op.execute(
        "UPDATE maintenance_collection_snapshot "
        "SET source = 'direct_api', import_batch_id = NULL "
        "WHERE source = 'bulk_import'"
    )
    op.drop_constraint(
        "ck_maintenance_collection_import_batch",
        "maintenance_collection_snapshot",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_collection_source",
        "maintenance_collection_snapshot",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_source",
        "maintenance_collection_snapshot",
        "source IN ('legacy', 'direct_api', 'workbook')",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_import_batch",
        "maintenance_collection_snapshot",
        "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
        "(source IN ('legacy', 'direct_api') AND import_batch_id IS NULL)",
    )

    op.drop_index(
        "ix_maintenance_collection_receipt_batch",
        table_name="maintenance_collection_receipt",
    )
    op.drop_index(
        "ix_maintenance_collection_receipt_contract",
        table_name="maintenance_collection_receipt",
    )
    op.drop_table("maintenance_collection_receipt")
