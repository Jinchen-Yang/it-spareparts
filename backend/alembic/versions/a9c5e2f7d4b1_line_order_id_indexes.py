"""销售/采购行表 order_id 复合索引（看板 v2 审计 P2：parts 批量装配热路径）

Revision ID: a9c5e2f7d4b1
Revises: a3f8c1d9e5b2
Create Date: 2026-07-14 12:00:00.000000

看板 v2 起，订单列表每次请求都按 `order_id IN (当页≤200)` 批量取行装配 parts；
两张行表此前对 order_id 均无索引（FK 不自动建索引），逐请求顺序扫全行表。
建 (order_id, id) 复合索引，同时覆盖 `ORDER BY order_id, id` 的确定性行序。
纯加索引、不改数据，幂等可重跑。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a9c5e2f7d4b1"
down_revision: Union[str, Sequence[str], None] = "a3f8c1d9e5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_sl_order", "f_sales_line", ["order_id", "id"])
    op.create_index("ix_pl_order", "f_purchase_line", ["order_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_pl_order", table_name="f_purchase_line")
    op.drop_index("ix_sl_order", table_name="f_sales_line")
