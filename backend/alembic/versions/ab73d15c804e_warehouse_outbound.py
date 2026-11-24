"""D-0066 仓单分析保留 CKD 单头的出库仓库。"""
import sqlalchemy as sa
from alembic import op

revision = "ab73d15c804e"
down_revision = "f261fbad9e04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("warehouse_analytics_document", sa.Column("outbound_warehouse", sa.Text()))


def downgrade() -> None:
    op.drop_column("warehouse_analytics_document", "outbound_warehouse")
