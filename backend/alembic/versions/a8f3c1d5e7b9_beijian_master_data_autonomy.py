"""备件主数据自治（WP1）：dim_part 加 master_source / locked_fields / category_source

采购可新建/编辑型号，被人工维护过的字段进 locked_fields，loader 重导时一律不覆盖
（"和氚云无 API、把服务器 PN 做成自治主数据"的地基）。

Revision ID: a8f3c1d5e7b9
Revises: e9d4c2b7a1f5
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a8f3c1d5e7b9"
down_revision = "e9d4c2b7a1f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 建档来源：import=氚云导入 / manual=采购手工新建
    op.add_column("dim_part", sa.Column(
        "master_source", sa.String(16), nullable=False, server_default="import"))
    # 采购人工维护过的字段名集合；loader 对其中字段保留人工值，绝不覆盖
    op.add_column("dim_part", sa.Column(
        "locked_fields", postgresql.ARRAY(sa.Text()), nullable=False,
        server_default=sa.text("'{}'::text[]")))
    # 品类来源：AUTO(轻量分类引擎)/MANUAL(人工)/IMPORT(氚云)；空=未分类
    op.add_column("dim_part", sa.Column("category_source", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("dim_part", "category_source")
    op.drop_column("dim_part", "locked_fields")
    op.drop_column("dim_part", "master_source")
