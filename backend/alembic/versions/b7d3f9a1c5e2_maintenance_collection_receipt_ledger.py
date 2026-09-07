"""收款单（SKD）导入台账与快照来源扩展（D-16，issue #288）

1. 新表 ``maintenance_collection_receipt``：逐笔实收台账。唯一键只约束**生效行**
   （偏唯一索引 ``ux_maintenance_collection_receipt_active``），让同一收款单跨批次
   幂等，同时允许人工裁决把旧行留档（``is_active=false`` + ``superseded_by``）、
   另插一条更正行（``ruling_id`` 非空、无批次、无原件 sha256）；
2. ``maintenance_collection_snapshot`` 来源新增 ``bulk_import``，与 ``workbook``
   一样必须带 ``import_batch_id``，批量导入写出/覆盖的快照直接指向批次；
3. ``ux_batch_success_hash`` 偏唯一索引排除 ``maint_bulk``：该网关的
   ``file_hash`` 现在保存原件 sha256（原件同时归档到 ``sys_raw_file``），
   同一原件分次勾选提交是合法流程，不能被"同 hash 只许成功一次"挡住；
4. ``ix_batch_success_selection_hash``：应用幂等按 ``report_json->>'selection_hash'``
   找已成功批次，偏唯一表达式索引（``status='success' AND file_type='maint_bulk'``）
   避免随批次表增长退化成全表扫，同时把"同一选择只成功一次"落成约束。

downgrade 有损：``bulk_import`` 来源的快照退回 ``direct_api``/无批次
（旧 CHECK 不认识新来源），台账表整体删除。**受保护**，任一情况都拒绝降级
（``downgrade refused``）而不是半途崩掉或静默丢事实：
* 已经存在同一原件分次提交的多条 success ``maint_bulk`` 批次——旧唯一索引根本
  建不回来；
* 台账表有任何行（含裁决留档行）——逐笔实收事实与裁决链会随表一起消失；
* 存在 ``source='bulk_import'`` 的快照——来源 / 批次溯源会被抹平。
运维须先导出台账与快照溯源（或确认可弃）再降级。

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
    op.execute("SET LOCAL lock_timeout = '5s'")
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
        # 导入行必带原件 sha256；裁决更正行没有原件。
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
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
        # 人工裁决链：被取代行指向更正行；更正行带裁决号。
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column("ruling_id", sa.String(length=36), nullable=True),
        sa.Column("ruling_reason", sa.Text(), nullable=True),
        sa.Column("ruled_by", sa.String(length=64), nullable=True),
        sa.Column("ruled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["maintenance_collection_receipt.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_maintenance_collection_receipt_active",
        "maintenance_collection_receipt",
        ["contract_no", "receipt_no"],
        unique=True,
        postgresql_where=sa.text("is_active"),
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
    op.create_index(
        "ix_batch_success_selection_hash",
        "sys_import_batch",
        ["file_type", sa.text("(report_json ->> 'selection_hash')")],
        unique=True,
        postgresql_where=sa.text("status = 'success' AND file_type = 'maint_bulk'"),
    )


def downgrade() -> None:
    # 旧 ux_batch_success_hash 对 maint_bulk 也要求 (file_type, file_hash) 唯一。
    # 同一原件分次勾选提交会留下多条 success 批次；那样的库建不回旧索引，
    # 与其在 CREATE INDEX 时以 UniqueViolation 半途崩掉，不如在任何改动之前
    # 明确拒绝，让运维先归档/清理再降级（同 a9c4e7b2d6f1 的受保护降级形态）。
    # 台账行（含裁决留档）与 bulk_import 来源快照同理：降级会把它们连同溯源一起
    # 抹掉，运维必须先导出再降级。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE sys_import_batch, maintenance_collection_receipt, "
        "maintenance_collection_snapshot IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM sys_import_batch
                WHERE status = 'success'
                GROUP BY file_type, file_hash
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'success import batches share a (file_type, file_hash); '
                    'downgrade refused';
            END IF;
            IF EXISTS (SELECT 1 FROM maintenance_collection_receipt) THEN
                RAISE EXCEPTION
                    'maintenance_collection_receipt has rows (rulings included); '
                    'export the receipt ledger before downgrading; downgrade refused';
            END IF;
            IF EXISTS (
                SELECT 1 FROM maintenance_collection_snapshot
                WHERE source = 'bulk_import'
            ) THEN
                RAISE EXCEPTION
                    'maintenance_collection_snapshot has bulk_import rows; '
                    'export snapshot provenance before downgrading; downgrade refused';
            END IF;
        END;
        $$
        """
    )

    op.drop_index("ix_batch_success_selection_hash", table_name="sys_import_batch")
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
    op.drop_index(
        "ux_maintenance_collection_receipt_active",
        table_name="maintenance_collection_receipt",
    )
    op.drop_table("maintenance_collection_receipt")
