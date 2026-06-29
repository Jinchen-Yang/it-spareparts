"""order status+date 复合索引（架构体检 2026-06-29：唯一效率真瓶颈）

Revision ID: e9d4c2b7a1f5
Revises: d7a4f1c9b3e2
Create Date: 2026-06-29 12:00:00.000000

order_date / data_status 是全站最高频的过滤+排序列（recent_purchases / 采购分析窗口 /
profit 聚合与重算 / 各列表 order_date desc），此前全表无索引，4.5万/4.8万行上退化为
seq scan + sort。加 (data_status, order_date) 复合索引，把核心列表/面板从 O(全表) 降到
O(窗口结果)。纯加索引、不改数据，幂等可重跑。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e9d4c2b7a1f5"
down_revision: Union[str, Sequence[str], None] = "d7a4f1c9b3e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_po_status_date", "f_purchase_order", ["data_status", "order_date"])
    op.create_index("ix_so_status_date", "f_sales_order", ["data_status", "order_date"])


def downgrade() -> None:
    op.drop_index("ix_so_status_date", table_name="f_sales_order")
    op.drop_index("ix_po_status_date", table_name="f_purchase_order")
