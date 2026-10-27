"""采购逐行沿用报价或说明不报原因。

Revision ID: a1c3e5f7b9d2
Revises: f9a1c3e5b7d9
"""
from alembic import op
import sqlalchemy as sa

revision = "a1c3e5f7b9d2"
down_revision = "f9a1c3e5b7d9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("presales_offer_line_link",
        sa.Column("line_id", sa.Integer(), sa.ForeignKey("presales_inquiry_line.id"), primary_key=True),
        sa.Column("offer_id", sa.Integer(), sa.ForeignKey("presales_offer.id"), primary_key=True),
    )
    op.create_table("presales_line_response",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("line_id", sa.Integer(), sa.ForeignKey("presales_inquiry_line.id"), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("sys_user.id"), nullable=False),
        sa.Column("work_item_id", sa.BigInteger(), sa.ForeignKey("work_item.id")),
        sa.Column("line_version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("ref_offer_id", sa.Integer(), sa.ForeignKey("presales_offer.id")),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("line_id", "created_by", name="uq_presales_line_response_author"),
        sa.CheckConstraint("line_version >= 1", name="ck_presales_line_response_version"),
        sa.CheckConstraint("(mode = 'reuse' AND ref_offer_id IS NOT NULL AND reason IS NULL) OR "
                           "(mode = 'skip' AND ref_offer_id IS NULL AND reason IS NOT NULL AND length(trim(reason)) > 0)",
                           name="ck_presales_line_response_mode"),
    )


def downgrade():
    op.drop_table("presales_line_response")
    op.drop_table("presales_offer_line_link")
