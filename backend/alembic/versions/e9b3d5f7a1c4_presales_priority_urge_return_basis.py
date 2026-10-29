"""售前询价优先级、催办、坏件是否返回与报价价格性质（D-0047）。

Revision ID: e9b3d5f7a1c4
Revises: d6e8f0a2b4c6
"""

from alembic import op
import sqlalchemy as sa


revision = "e9b3d5f7a1c4"
down_revision = "d6e8f0a2b4c6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("presales_inquiry", sa.Column("priority", sa.String(8), server_default="normal", nullable=False))
    op.add_column("presales_inquiry", sa.Column("urge_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("presales_inquiry", sa.Column("last_urged_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_presales_inquiry_priority", "presales_inquiry", "priority IN ('normal', 'urgent')")
    op.add_column("presales_inquiry_line", sa.Column("return_defective", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("presales_offer", sa.Column("price_basis", sa.String(8), server_default="sale", nullable=False))
    op.create_check_constraint("ck_presales_offer_price_basis", "presales_offer", "price_basis IN ('sale', 'cost')")


def downgrade():
    op.drop_constraint("ck_presales_offer_price_basis", "presales_offer", type_="check")
    op.drop_column("presales_offer", "price_basis")
    op.drop_column("presales_inquiry_line", "return_defective")
    op.drop_constraint("ck_presales_inquiry_priority", "presales_inquiry", type_="check")
    op.drop_column("presales_inquiry", "last_urged_at")
    op.drop_column("presales_inquiry", "urge_count")
    op.drop_column("presales_inquiry", "priority")
