"""维保出库成本核算：f_maintenance_order/line 新表 + 采购头加维保关联列

Revision ID: a3f8c1d6e2b9
Revises: a8f3c1d5e7b9
Create Date: 2026-07-02 23:30:00.000000

口径已与客户确认（docs/维保出库成本核算-开发方案.md §0）：
- 新表承载 WBDD 维保出库明细；成本字段由 maintenance_cost.recompute 回填。
- f_purchase_order.linked_maintenance_order_no：采购单「维保需求单」列（WBDD），
  维保需求类采购 100% 回填（2026 实测）——成本"专属采购直配"层的关联键。
  存量采购需用原文件 upsert 模式重导一次回填该列（上线 runbook §11）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3f8c1d6e2b9"
down_revision: Union[str, Sequence[str], None] = "a8f3c1d5e7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("f_purchase_order",
                  sa.Column("linked_maintenance_order_no", sa.String(64), nullable=True))
    op.create_index("ix_po_linked_maint", "f_purchase_order", ["linked_maintenance_order_no"])

    op.create_table(
        "f_maintenance_order",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("raw_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("order_no", sa.String(64), nullable=False),
        sa.Column("order_date", sa.Date),
        sa.Column("linked_sales_order_no", sa.String(64)),
        sa.Column("project_raw", sa.String(256)),
        sa.Column("project_std", sa.String(256)),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("dim_customer.id")),
        sa.Column("end_customer", sa.String(256)),
        sa.Column("demand_type", sa.String(16)),
        sa.Column("business_type", sa.String(16)),
        sa.Column("salesperson", sa.String(64)),
        sa.Column("warehouse", sa.String(64)),
        sa.Column("maint_start", sa.Date),
        sa.Column("maint_end", sa.Date),
        sa.Column("data_status", sa.String(16)),
        sa.Column("import_batch_id", sa.Integer,
                  sa.ForeignKey("sys_import_batch.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_mo_order_no", "f_maintenance_order", ["order_no"])
    op.create_index("ix_mo_linked", "f_maintenance_order", ["linked_sales_order_no"])
    op.create_index("ix_mo_project", "f_maintenance_order", ["project_std"])
    op.create_index("ix_mo_status_date", "f_maintenance_order", ["data_status", "order_date"])

    op.create_table(
        "f_maintenance_line",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("raw_line_id", sa.String(64), nullable=False, unique=True),
        sa.Column("order_id", sa.Integer,
                  sa.ForeignKey("f_maintenance_order.id"), nullable=False),
        sa.Column("line_no", sa.Integer),
        sa.Column("part_id", sa.Integer, sa.ForeignKey("dim_part.id"), nullable=False),
        sa.Column("pn_std", sa.String(128)),
        sa.Column("pn_raw", sa.String(256)),
        sa.Column("description", sa.Text),
        sa.Column("qty", sa.Numeric(14, 3)),
        sa.Column("return_qty", sa.Numeric(14, 3)),
        sa.Column("serial_numbers", sa.Text),
        sa.Column("unit_cost", sa.Numeric(14, 2)),
        sa.Column("cost_amount", sa.Numeric(14, 2)),
        sa.Column("cost_source", sa.String(16)),
        sa.Column("cost_tax_basis", sa.String(4)),
        sa.Column("price_month", sa.String(7)),
        sa.Column("trace_months", sa.SmallInteger),
        sa.Column("linked_purchase_order_no", sa.String(64)),
        sa.Column("anomaly_flags", sa.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("import_batch_id", sa.Integer,
                  sa.ForeignKey("sys_import_batch.id"), nullable=False),
    )
    op.create_index("ix_ml_order", "f_maintenance_line", ["order_id"])
    op.create_index("ix_ml_part", "f_maintenance_line", ["part_id"])


def downgrade() -> None:
    op.drop_table("f_maintenance_line")
    op.drop_table("f_maintenance_order")
    op.drop_index("ix_po_linked_maint", table_name="f_purchase_order")
    op.drop_column("f_purchase_order", "linked_maintenance_order_no")
