"""f_sales_reconciliation_snapshot 快照表 + f_sales_order 含税金额三列（D-40/D-41 PR-1）

氚云销售导出单头的 已对账/待对账/退货已对账 是订单级累计快照，同一订单多次上传、
内容会反复出现（含 A→B→A 改回旧值）。新表按 D-40 §3 对每次导入/回填的每个订单
记**一条观测**，(raw_order_id, import_batch_id) 唯一：同一批次重放/回填重跑不产生
重复行（ON CONFLICT 写入由 PR-2 落实），不同批次各记一条（即使内容相同）——内容
去重会吞掉 A→B→A 的最后一次 A，故 content_hash 只保留用于识别内容变化/报告，
不参与唯一约束。快照可从归档原件回放重建（D-40 §7），因此 downgrade 直接 drop
本迁移创建的对象，不加数据守卫；生产回滚仍以恢复迁移前备份为准。

f_sales_order 只加可空列 amount_inc_tax / is_tax_inclusive / tax_amount（D-41 §3
三金额并存），不改既有列语义；loader 写入在 PR-2。

索引与约束和 ORM 元数据严格对齐（alembic check 零漂移）：
- uq_sales_recon_snapshot_order_batch (raw_order_id, import_batch_id) UNIQUE；
- ix_sales_recon_snapshot_latest (raw_order_id, source_modified_at DESC NULLS LAST,
  import_batch_id DESC, id DESC) 供"取每单最新"（PG 索引列裸 DESC 默认 NULLS FIRST，
  与查询排序键 source_modified_at DESC NULLS LAST 对齐必须显式写 NULLS LAST；
  import_batch_id 非空且 id 为 PK 非空，裸 DESC 与 NULLS LAST 等价）；
- ix_sales_recon_snapshot_batch (import_batch_id)：FK 查询路径，PG 不自动为 FK 建索引。

Revision ID: b8f2d6c4a1e9
Revises: 7f73424a7f65
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8f2d6c4a1e9"
down_revision: str | None = "7f73424a7f65"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 按仓库惯例带 lock_timeout，避免极端情况下等锁堵住迁移事务。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "f_sales_reconciliation_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("raw_order_id", sa.String(64), nullable=False),
        sa.Column("order_no", sa.String(64), nullable=True),
        # 可空：出现快照时订单可能尚未入库/已被其他流程处理
        sa.Column("sales_order_id", sa.Integer(), nullable=True),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        # ↑ 该观测所属的导入/回填批次（每批次每单至多一条观测，见唯一约束）
        sa.Column("source", sa.String(16), nullable=False),
        # 氚云「修改时间」；旧格式文件没有该列 → NULL
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        # 四金额：空单元格=NULL，绝不当 0
        sa.Column("order_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("reconciled_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("pending_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("return_reconciled_amount", sa.Numeric(14, 2), nullable=True),
        # 规范化 sha256，规则在 app/services/sales_reconciliation.snapshot_content_hash
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["sales_order_id"], ["f_sales_order.id"]),
        sa.ForeignKeyConstraint(["import_batch_id"], ["sys_import_batch.id"]),
        sa.CheckConstraint(
            "source IN ('import', 'backfill')",
            name="ck_sales_recon_snapshot_source",
        ),
        sa.UniqueConstraint(
            "raw_order_id", "import_batch_id", name="uq_sales_recon_snapshot_order_batch"
        ),
    )
    op.create_index(
        "ix_sales_recon_snapshot_latest",
        "f_sales_reconciliation_snapshot",
        [
            "raw_order_id",
            sa.text("source_modified_at DESC NULLS LAST"),
            sa.text("import_batch_id DESC"),
            sa.text("id DESC"),
        ],
    )
    op.create_index(
        "ix_sales_recon_snapshot_batch",
        "f_sales_reconciliation_snapshot",
        ["import_batch_id"],
    )
    op.add_column(
        "f_sales_order", sa.Column("amount_inc_tax", sa.Numeric(14, 2), nullable=True)
    )
    op.add_column(
        "f_sales_order", sa.Column("is_tax_inclusive", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "f_sales_order", sa.Column("tax_amount", sa.Numeric(14, 2), nullable=True)
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("f_sales_order", "tax_amount")
    op.drop_column("f_sales_order", "is_tax_inclusive")
    op.drop_column("f_sales_order", "amount_inc_tax")
    op.drop_index(
        "ix_sales_recon_snapshot_batch", table_name="f_sales_reconciliation_snapshot"
    )
    op.drop_index(
        "ix_sales_recon_snapshot_latest", table_name="f_sales_reconciliation_snapshot"
    )
    op.drop_table("f_sales_reconciliation_snapshot")
