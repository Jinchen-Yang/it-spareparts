"""D-0064 销售订单待收款快照。"""
from alembic import op
import sqlalchemy as sa

revision = "e8b4c1a6d925"
down_revision = "a7e2f9c4d1b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("f_sales_order", sa.Column("sales_receivable_amount", sa.Numeric(14, 2)))
    op.add_column("f_sales_order", sa.Column("sales_receivable_batch_id", sa.Integer()))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("f_sales_order", "sales_receivable_batch_id")
    op.drop_column("f_sales_order", "sales_receivable_amount")
