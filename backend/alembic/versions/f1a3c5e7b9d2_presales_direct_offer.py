"""D-0048 主动报价不生成业务单据。

Revision ID: f1a3c5e7b9d2
Revises: e9b3d5f7a1c4
"""
from alembic import op
import sqlalchemy as sa

revision = "f1a3c5e7b9d2"
down_revision = "e9b3d5f7a1c4"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("presales_offer", "inquiry_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("presales_offer", "line_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("presales_offer", sa.Column("offer_audience_rule_id", sa.Integer(),
        sa.ForeignKey("audience_rule.id", name="fk_presales_offer_audience"), nullable=True))
    op.create_check_constraint("ck_presales_offer_origin_pair", "presales_offer",
        "(inquiry_id IS NULL) = (line_id IS NULL)")


def downgrade():
    # 不删除报价或伪造 BJ 单来满足旧约束。已产生无单据报价时，需保留新schema
    # 或按经授权的备份恢复方案回滚，不能破坏业务数据。
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM presales_offer WHERE inquiry_id IS NULL")):
        raise RuntimeError("存在无单据主动报价；禁止破坏性降级，请保留schema或使用发布备份恢复方案")
    op.drop_constraint("ck_presales_offer_origin_pair", "presales_offer", type_="check")
    op.drop_column("presales_offer", "offer_audience_rule_id")
    op.alter_column("presales_offer", "line_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column("presales_offer", "inquiry_id", existing_type=sa.Integer(), nullable=False)
