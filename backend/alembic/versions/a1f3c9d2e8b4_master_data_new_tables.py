"""master data governance: new tables

整改 P1（其一）：纯建表，不碰存量表。
brands / product_categories / product_specs / product_match_candidates /
product_merge_logs / product_data_quality_issues。

Revision ID: a1f3c9d2e8b4
Revises: bad10309d7ec
Create Date: 2026-06-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = 'a1f3c9d2e8b4'
down_revision: Union[str, Sequence[str], None] = 'bad10309d7ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'brands',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('brand_name', sa.String(length=128), nullable=False),
        sa.Column('normalized_name', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('normalized_name'),
    )

    op.create_table(
        'product_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('category_major', sa.String(length=64), nullable=False),
        sa.Column('category_minor', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category_major', 'category_minor',
                            name='uq_category_major_minor',
                            postgresql_nulls_not_distinct=True),
    )

    op.create_table(
        'product_specs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('part_id', sa.Integer(), nullable=False),
        sa.Column('spec_key', sa.String(length=32), nullable=False),
        sa.Column('spec_value', sa.String(length=128), nullable=False),
        sa.Column('spec_unit', sa.String(length=16), nullable=True),
        sa.Column('numeric_value', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('source', sa.String(length=16), server_default='auto', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['part_id'], ['dim_part.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('part_id', 'spec_key', name='uq_spec_part_key'),
    )
    op.create_index('ix_spec_key_value', 'product_specs', ['spec_key', 'spec_value'])
    op.create_index('ix_spec_key_numeric', 'product_specs', ['spec_key', 'numeric_value'])

    op.create_table(
        'product_match_candidates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('raw_text', sa.Text(), nullable=False),
        sa.Column('normalized_text', sa.Text(), nullable=True),
        sa.Column('source_part_id', sa.Integer(), nullable=True),
        sa.Column('candidate_part_id', sa.Integer(), nullable=True),
        sa.Column('match_score', sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column('match_reason', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
        sa.Column('review_action', sa.String(length=32), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['source_part_id'], ['dim_part.id']),
        sa.ForeignKeyConstraint(['candidate_part_id'], ['dim_part.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("status IN ('pending','accepted','rejected','obsolete')", name='ck_pmc_status'),
    )
    op.create_index('ux_pmc_pending', 'product_match_candidates',
                    ['source_part_id', 'candidate_part_id'], unique=True,
                    postgresql_where=sa.text("status = 'pending'"))
    op.create_index('ix_pmc_status', 'product_match_candidates', ['status'])
    op.create_index('ix_pmc_source', 'product_match_candidates', ['source_part_id'])
    op.create_index('ix_pmc_candidate', 'product_match_candidates', ['candidate_part_id'])

    op.create_table(
        'product_merge_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_part_id', sa.Integer(), nullable=False),
        sa.Column('target_part_id', sa.Integer(), nullable=False),
        sa.Column('merge_reason', sa.Text(), nullable=True),
        sa.Column('source_snapshot', JSONB(), nullable=True),
        sa.Column('merged_fields', JSONB(), nullable=True),
        sa.Column('affected_records', JSONB(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['source_part_id'], ['dim_part.id']),
        sa.ForeignKeyConstraint(['target_part_id'], ['dim_part.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_merge_source', 'product_merge_logs', ['source_part_id'])
    op.create_index('ix_merge_target', 'product_merge_logs', ['target_part_id'])

    op.create_table(
        'product_data_quality_issues',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('issue_type', sa.String(length=64), nullable=False),
        sa.Column('entity_type', sa.String(length=16), server_default='part', nullable=False),
        sa.Column('entity_id', sa.BigInteger(), nullable=False),
        sa.Column('part_id', sa.Integer(), nullable=True),
        sa.Column('issue_level', sa.String(length=16), server_default='warning', nullable=False),
        sa.Column('issue_description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='open', nullable=False),
        sa.Column('detected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('resolved_by', sa.String(length=64), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['part_id'], ['dim_part.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("status IN ('open','resolved','dismissed')", name='ck_dqi_status'),
    )
    op.create_index('ux_dqi_open', 'product_data_quality_issues',
                    ['issue_type', 'entity_type', 'entity_id'], unique=True,
                    postgresql_where=sa.text("status = 'open'"))
    op.create_index('ix_dqi_status_type', 'product_data_quality_issues', ['status', 'issue_type'])
    op.create_index('ix_dqi_part', 'product_data_quality_issues', ['part_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('product_data_quality_issues')
    op.drop_table('product_merge_logs')
    op.drop_table('product_match_candidates')
    op.drop_table('product_specs')
    op.drop_table('product_categories')
    op.drop_table('brands')
