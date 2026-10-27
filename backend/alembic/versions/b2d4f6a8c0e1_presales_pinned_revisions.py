"""引用、沿用和成交保存报价版本。

Revision ID: b2d4f6a8c0e1
Revises: a1c3e5f7b9d2
"""
from alembic import op
import sqlalchemy as sa

revision = "b2d4f6a8c0e1"
down_revision = "a1c3e5f7b9d2"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("presales_inquiry", "won_offer_revision_no"),
    ("presales_inquiry_line", "ref_offer_revision_no"),
    ("presales_line_response", "ref_offer_revision_no"),
    ("presales_offer_line_link", "offer_revision_no"),
)


def upgrade():
    # 旧关联没有可靠的引用时刻，不能把当前版本冒充当时版本；留空待核对。
    for table, column in _COLUMNS:
        op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))


def downgrade():
    for table, column in reversed(_COLUMNS):
        op.drop_column(table, column)
