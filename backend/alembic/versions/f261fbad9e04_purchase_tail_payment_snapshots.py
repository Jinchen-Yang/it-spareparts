"""D-0074 采购订单已付、待付尾款快照。"""
import sqlalchemy as sa
from alembic import op

revision = "f261fbad9e04"
down_revision = "b6285fa3c918"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for prefix in ("purchase_paid_tail", "purchase_pending_tail"):
        op.add_column("f_purchase_order", sa.Column(f"{prefix}_amount", sa.Numeric(14, 2)))
        op.add_column("f_purchase_order", sa.Column(f"{prefix}_batch_id", sa.Integer()))


def downgrade() -> None:
    for prefix in ("purchase_pending_tail", "purchase_paid_tail"):
        op.drop_column("f_purchase_order", f"{prefix}_batch_id")
        op.drop_column("f_purchase_order", f"{prefix}_amount")
