"""master data governance: alter existing tables

整改 P1（其二）：存量表 additive 扩展 + 小量数据修正。
- dim_part：status/merged_into_id/brand_id/category_id/data_quality_score/reviewed_at
  + (id, pn_std) 冗余唯一（供 part_alias 复合外键防漂移）
- part_alias：part_id/status + 复合外键 (part_id, pn_std) → dim_part(id, pn_std)
- part_substitute：direction/substitute_type/status/reviewed_at（保留 a<b 规范序 CHECK）
- 事实表 part_id 收紧 NOT NULL（活库实测 0 空值）；f_part_inquiry 补 part_id

锁说明：均为 ADD COLUMN 默认值/约束校验级操作，dim_part 2.3 万行、事实表共约
10 万行，秒级完成；env.py 已开 transaction_per_migration。

Revision ID: b2e7d5c1f9a3
Revises: a1f3c9d2e8b4
Create Date: 2026-06-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2e7d5c1f9a3'
down_revision: Union[str, Sequence[str], None] = 'a1f3c9d2e8b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ---- dim_part ----
    op.add_column('dim_part', sa.Column('status', sa.String(length=16), server_default='active', nullable=False))
    op.add_column('dim_part', sa.Column('merged_into_id', sa.Integer(), nullable=True))
    op.add_column('dim_part', sa.Column('brand_id', sa.Integer(), nullable=True))
    op.add_column('dim_part', sa.Column('category_id', sa.Integer(), nullable=True))
    op.add_column('dim_part', sa.Column('data_quality_score', sa.Numeric(precision=5, scale=2), nullable=True))
    op.add_column('dim_part', sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key('fk_part_merged_into', 'dim_part', 'dim_part', ['merged_into_id'], ['id'])
    op.create_foreign_key('fk_part_brand', 'dim_part', 'brands', ['brand_id'], ['id'])
    op.create_foreign_key('fk_part_category', 'dim_part', 'product_categories', ['category_id'], ['id'])
    op.create_unique_constraint('uq_part_id_pn', 'dim_part', ['id', 'pn_std'])
    op.create_check_constraint('ck_part_status', 'dim_part', "status IN ('active','merged')")
    op.create_check_constraint('ck_part_no_self_merge', 'dim_part',
                               'merged_into_id IS NULL OR merged_into_id <> id')
    op.create_check_constraint('ck_part_merged_pair', 'dim_part',
                               "(status = 'merged') = (merged_into_id IS NOT NULL)")
    op.create_index('ix_part_brand', 'dim_part', ['brand_id'])
    op.create_index('ix_part_category', 'dim_part', ['category_id'])
    op.create_index('ix_part_merged_into', 'dim_part', ['merged_into_id'],
                    postgresql_where=sa.text('merged_into_id IS NOT NULL'))

    # ---- part_alias ----
    op.add_column('part_alias', sa.Column('part_id', sa.Integer(), nullable=True))
    op.add_column('part_alias', sa.Column('status', sa.String(length=16), nullable=True))
    op.create_foreign_key('fk_alias_part_pn', 'part_alias', 'dim_part',
                          ['part_id', 'pn_std'], ['id', 'pn_std'])
    op.create_check_constraint('ck_alias_status', 'part_alias',
                               "status IS NULL OR status IN ('pending','active','rejected')")
    op.create_index('ix_alias_part', 'part_alias', ['part_id'])

    # ---- part_substitute ----
    op.add_column('part_substitute', sa.Column('direction', sa.String(length=8),
                                               server_default='both', nullable=False))
    op.add_column('part_substitute', sa.Column('substitute_type', sa.String(length=32), nullable=True))
    op.add_column('part_substitute', sa.Column('status', sa.String(length=16),
                                               server_default='pending', nullable=False))
    op.add_column('part_substitute', sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint('ck_substitute_direction', 'part_substitute',
                               "direction IN ('both','a_to_b','b_to_a')")
    op.create_check_constraint('ck_substitute_status', 'part_substitute',
                               "status IN ('pending','active','rejected')")
    # 存量替代关系均为人工录入 → 视为已生效，不打回待审
    op.execute("UPDATE part_substitute SET status = 'active' WHERE source = 'manual'")

    # ---- 事实表 part_id 收紧 + 询价补列 ----
    op.alter_column('f_purchase_line', 'part_id', existing_type=sa.Integer(), nullable=False)
    op.alter_column('f_sales_line', 'part_id', existing_type=sa.Integer(), nullable=False)
    op.alter_column('inventory', 'part_id', existing_type=sa.Integer(), nullable=False)
    op.add_column('f_part_inquiry', sa.Column('part_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_inquiry_part', 'f_part_inquiry', 'dim_part', ['part_id'], ['id'])
    op.create_index('ix_inq_part', 'f_part_inquiry', ['part_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_inq_part', table_name='f_part_inquiry')
    op.drop_constraint('fk_inquiry_part', 'f_part_inquiry', type_='foreignkey')
    op.drop_column('f_part_inquiry', 'part_id')
    op.alter_column('inventory', 'part_id', existing_type=sa.Integer(), nullable=True)
    op.alter_column('f_sales_line', 'part_id', existing_type=sa.Integer(), nullable=True)
    op.alter_column('f_purchase_line', 'part_id', existing_type=sa.Integer(), nullable=True)

    op.drop_constraint('ck_substitute_status', 'part_substitute', type_='check')
    op.drop_constraint('ck_substitute_direction', 'part_substitute', type_='check')
    op.drop_column('part_substitute', 'reviewed_at')
    op.drop_column('part_substitute', 'status')
    op.drop_column('part_substitute', 'substitute_type')
    op.drop_column('part_substitute', 'direction')

    op.drop_index('ix_alias_part', table_name='part_alias')
    op.drop_constraint('ck_alias_status', 'part_alias', type_='check')
    op.drop_constraint('fk_alias_part_pn', 'part_alias', type_='foreignkey')
    op.drop_column('part_alias', 'status')
    op.drop_column('part_alias', 'part_id')

    op.drop_index('ix_part_merged_into', table_name='dim_part')
    op.drop_index('ix_part_category', table_name='dim_part')
    op.drop_index('ix_part_brand', table_name='dim_part')
    op.drop_constraint('ck_part_merged_pair', 'dim_part', type_='check')
    op.drop_constraint('ck_part_no_self_merge', 'dim_part', type_='check')
    op.drop_constraint('ck_part_status', 'dim_part', type_='check')
    op.drop_constraint('uq_part_id_pn', 'dim_part', type_='unique')
    op.drop_constraint('fk_part_category', 'dim_part', type_='foreignkey')
    op.drop_constraint('fk_part_brand', 'dim_part', type_='foreignkey')
    op.drop_constraint('fk_part_merged_into', 'dim_part', type_='foreignkey')
    op.drop_column('dim_part', 'reviewed_at')
    op.drop_column('dim_part', 'data_quality_score')
    op.drop_column('dim_part', 'category_id')
    op.drop_column('dim_part', 'brand_id')
    op.drop_column('dim_part', 'merged_into_id')
    op.drop_column('dim_part', 'status')
