"""售前询价、报价历史及权限默认值。

Revision ID: f9a1c3e5b7d9
Revises: e7a9c1d3f5b7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f9a1c3e5b7d9'
down_revision: Union[str, Sequence[str], None] = 'e7a9c1d3f5b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEYS = (
    'page_presales', 'action_presales_inquire', 'action_presales_quote',
    'action_presales_offer', 'action_presales_bind_part', 'action_presales_supervise',
)
_ENABLED = {
    'admin': _KEYS,
    'boss': _KEYS,
    'sales': ('page_presales', 'action_presales_inquire'),
    'purchaser': ('page_presales', 'action_presales_quote',
                  'action_presales_offer', 'action_presales_bind_part'),
}


def _permission_defaults() -> None:
    # 新增能力按角色回填；已有稀疏覆盖保留，避免吞掉管理员的显式选择。
    for key in _KEYS:
        enabled_roles = tuple(role for role, keys in _ENABLED.items() if key in keys)
        role_list = ', '.join(f"'{role}'" for role in enabled_roles)
        for table, column, role_column in (
            ('sys_role_template', 'permissions', 'code'),
            ('sys_user', 'template_perms', 'role'),
            ('sys_user', 'permissions', 'role'),
        ):
            condition = '' if table == 'sys_role_template' else (
                f" WHERE jsonb_typeof({column}) = 'object'"
            )
            op.execute(sa.text(f"""
                UPDATE {table}
                SET {column} = CASE WHEN jsonb_typeof({column}) = 'object'
                                    THEN {column} ELSE '{{}}'::jsonb END
                    || jsonb_build_object(:key, {role_column} IN ({role_list}))
                {condition}
            """).bindparams(key=key))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table('presales_inquiry',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('inquiry_no', sa.String(length=16), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='draft', nullable=False),
    sa.Column('revision', sa.Integer(), server_default='1', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('offer_audience_rule_id', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('notes_audience_rule_id', sa.Integer(), nullable=True),
    sa.Column('assignee_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('submitted_snapshot', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_outcome', sa.String(length=16), nullable=True),
    sa.Column('won_offer_id', sa.Integer(), nullable=True),
    sa.CheckConstraint("closed_outcome IS NULL OR closed_outcome IN ('won', 'lost', 'cancelled')", name='ck_presales_inquiry_outcome'),
    sa.CheckConstraint("kind IN ('sales_inquiry', 'proactive_offer')", name='ck_presales_inquiry_kind'),
    sa.CheckConstraint("status IN ('draft', 'submitted', 'revising', 'closed')", name='ck_presales_inquiry_status'),
    sa.CheckConstraint('revision >= 1 AND version >= 1', name='ck_presales_inquiry_version'),
    sa.ForeignKeyConstraint(['created_by'], ['sys_user.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['dim_customer.id'], ),
    sa.ForeignKeyConstraint(['notes_audience_rule_id'], ['audience_rule.id'], ),
    sa.ForeignKeyConstraint(['offer_audience_rule_id'], ['audience_rule.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('inquiry_no')
    )
    op.create_index('ix_presales_inquiry_kind_status', 'presales_inquiry', ['kind', 'status'], unique=False)
    op.create_index('ix_presales_inquiry_owner_updated', 'presales_inquiry', ['created_by', 'updated_at'], unique=False)
    op.create_table('offer_price_daily',
    sa.Column('part_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('min', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('max', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('avg', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.CheckConstraint('count > 0', name='ck_offer_price_daily_count'),
    sa.ForeignKeyConstraint(['part_id'], ['dim_part.id'], ),
    sa.PrimaryKeyConstraint('part_id', 'date', 'source')
    )
    op.create_table('presales_inquiry_line',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('inquiry_id', sa.Integer(), nullable=False),
    sa.Column('line_key', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('part_id', sa.Integer(), nullable=True),
    sa.Column('pn_raw', sa.String(length=256), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('qty', sa.Numeric(precision=14, scale=3), nullable=False),
    sa.Column('require_exact_pn', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('ref_offer_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('notes_audience_rule_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'deleted')", name='ck_presales_line_status'),
    sa.CheckConstraint('seq >= 1 AND version >= 1 AND qty > 0', name='ck_presales_line_values'),
    sa.ForeignKeyConstraint(['inquiry_id'], ['presales_inquiry.id'], ),
    sa.ForeignKeyConstraint(['notes_audience_rule_id'], ['audience_rule.id'], ),
    sa.ForeignKeyConstraint(['part_id'], ['dim_part.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('inquiry_id', 'line_key', name='uq_presales_line_key')
    )
    op.create_index('ix_presales_line_inquiry_seq', 'presales_inquiry_line', ['inquiry_id', 'seq'], unique=False)
    op.create_table('presales_offer',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('inquiry_id', sa.Integer(), nullable=False),
    sa.Column('line_id', sa.Integer(), nullable=False),
    sa.Column('work_item_id', sa.BigInteger(), nullable=True),
    sa.Column('part_id', sa.Integer(), nullable=True),
    sa.Column('unit_price', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('is_tax_inclusive', sa.Boolean(), nullable=True),
    sa.Column('no_stock', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('qty_available', sa.Numeric(precision=14, scale=3), nullable=True),
    sa.Column('stock_location', sa.String(length=256), nullable=True),
    sa.Column('lead_time', sa.String(length=256), nullable=True),
    sa.Column('source_key', sa.String(length=128), server_default='', nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=True),
    sa.Column('validity_days', sa.Integer(), server_default='1', nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='draft', nullable=False),
    sa.Column('supplier_audience_rule_id', sa.Integer(), nullable=True),
    sa.Column('reporter_audience_rule_id', sa.Integer(), nullable=True),
    sa.Column('notes_audience_rule_id', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('revision_no', sa.Integer(), server_default='0', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'published', 'withdrawn', 'stale')", name='ck_presales_offer_status'),
    sa.CheckConstraint('qty_available IS NULL OR qty_available >= 0', name='ck_presales_offer_qty'),
    sa.CheckConstraint('revision_no >= 0 AND version >= 1 AND validity_days >= 1', name='ck_presales_offer_version'),
    sa.CheckConstraint('unit_price IS NULL OR unit_price >= 0', name='ck_presales_offer_price'),
    sa.ForeignKeyConstraint(['created_by'], ['sys_user.id'], ),
    sa.ForeignKeyConstraint(['inquiry_id'], ['presales_inquiry.id'], ),
    sa.ForeignKeyConstraint(['line_id'], ['presales_inquiry_line.id'], ),
    sa.ForeignKeyConstraint(['notes_audience_rule_id'], ['audience_rule.id'], ),
    sa.ForeignKeyConstraint(['part_id'], ['dim_part.id'], ),
    sa.ForeignKeyConstraint(['reporter_audience_rule_id'], ['audience_rule.id'], ),
    sa.ForeignKeyConstraint(['supplier_audience_rule_id'], ['audience_rule.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['dim_supplier.id'], ),
    sa.ForeignKeyConstraint(['work_item_id'], ['work_item.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_presales_offer_handler_source', 'presales_offer', ['created_by', 'part_id', 'source_key'], unique=False)
    op.create_index('ix_presales_offer_inquiry_handler', 'presales_offer', ['inquiry_id', 'created_by'], unique=False)
    op.create_index('ix_presales_offer_part_validity', 'presales_offer', ['part_id', 'status', 'valid_until'], unique=False)
    # 双向引用待报价表创建后补 FK，降级时也先解除循环。
    op.create_foreign_key('fk_presales_inquiry_won_offer', 'presales_inquiry', 'presales_offer', ['won_offer_id'], ['id'])
    op.create_foreign_key('fk_presales_line_ref_offer', 'presales_inquiry_line', 'presales_offer', ['ref_offer_id'], ['id'])
    op.create_table('presales_offer_revision',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('offer_id', sa.Integer(), nullable=False),
    sa.Column('revision_no', sa.Integer(), nullable=False),
    sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('changed_fields', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('actor', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('revision_no >= 1', name='ck_presales_offer_revision_no'),
    sa.ForeignKeyConstraint(['actor'], ['sys_user.id'], ),
    sa.ForeignKeyConstraint(['offer_id'], ['presales_offer.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('offer_id', 'revision_no', name='uq_presales_offer_revision')
    )
    op.create_index('ix_presales_offer_revision_created', 'presales_offer_revision', ['created_at', 'offer_id'], unique=False)
    op.execute("""
        CREATE FUNCTION reject_presales_offer_revision_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'presales_offer_revision is append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_presales_offer_revision_append_only
        BEFORE UPDATE OR DELETE ON presales_offer_revision
        FOR EACH ROW EXECUTE FUNCTION reject_presales_offer_revision_mutation()
    """)
    op.execute('REVOKE UPDATE, DELETE ON presales_offer_revision FROM PUBLIC')
    # 当前部署共用连接角色；即使它是表所有者，触发器也会拒绝删改。
    op.execute('REVOKE UPDATE, DELETE ON presales_offer_revision FROM CURRENT_USER')
    _permission_defaults()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for key in _KEYS:
        for table, column in (
            ('sys_role_template', 'permissions'), ('sys_user', 'template_perms'),
            ('sys_user', 'permissions'), ('sys_user', 'perm_overrides'),
        ):
            op.execute(sa.text(
                f"UPDATE {table} SET {column} = {column} - :key "
                f"WHERE jsonb_typeof({column}) = 'object'"
            ).bindparams(key=key))
    op.execute('DROP TRIGGER trg_presales_offer_revision_append_only ON presales_offer_revision')
    op.execute('DROP FUNCTION reject_presales_offer_revision_mutation()')
    op.drop_constraint('fk_presales_inquiry_won_offer', 'presales_inquiry', type_='foreignkey')
    op.drop_constraint('fk_presales_line_ref_offer', 'presales_inquiry_line', type_='foreignkey')
    op.drop_index('ix_presales_offer_revision_created', table_name='presales_offer_revision')
    op.drop_table('presales_offer_revision')
    op.drop_index('ix_presales_offer_part_validity', table_name='presales_offer')
    op.drop_index('ix_presales_offer_inquiry_handler', table_name='presales_offer')
    op.drop_index('ix_presales_offer_handler_source', table_name='presales_offer')
    op.drop_table('presales_offer')
    op.drop_index('ix_presales_line_inquiry_seq', table_name='presales_inquiry_line')
    op.drop_table('presales_inquiry_line')
    op.drop_table('offer_price_daily')
    op.drop_index('ix_presales_inquiry_owner_updated', table_name='presales_inquiry')
    op.drop_index('ix_presales_inquiry_kind_status', table_name='presales_inquiry')
    op.drop_table('presales_inquiry')
