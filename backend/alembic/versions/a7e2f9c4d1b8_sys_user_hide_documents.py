"""D-0063: sys_user.hide_documents —— 离职销售删除账号时可选隐藏其名下单据。

纯加法：1 个 bool 列、server_default false、无索引无约束。
隐藏集合由「deleted_at IS NOT NULL AND hide_documents」派生，读路径过滤用，
不触碰 f_sales_order / f_purchase_order 任何数据。

Revision ID: a7e2f9c4d1b8
Revises: c3e8a1d94b27
"""

from alembic import op
import sqlalchemy as sa

revision = "a7e2f9c4d1b8"
down_revision = "c3e8a1d94b27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("sys_user", sa.Column("hide_documents", sa.Boolean(), nullable=False,
                                        server_default="false"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("sys_user", "hide_documents")
