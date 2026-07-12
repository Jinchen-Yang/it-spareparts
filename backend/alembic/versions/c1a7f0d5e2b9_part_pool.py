"""通用号数据池：稳定 group_id 表 + 成员映射（老板看板池化分析）

Revision ID: c1a7f0d5e2b9
Revises: b7e3d9c4a2f1
Create Date: 2026-07-11

池成员由 pool.rebuild() 从「已生效双向互替」关系的连通分量算出并写入，非导入产物。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1a7f0d5e2b9"
down_revision: Union[str, Sequence[str], None] = "b7e3d9c4a2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "part_pool",
        sa.Column("group_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("needs_calibration", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("oversized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("group_id"),
    )
    op.create_table(
        "part_pool_member",
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["group_id"], ["part_pool.group_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("part_id"),
    )
    op.create_index("ix_pool_member_group", "part_pool_member", ["group_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_pool_member_group", table_name="part_pool_member")
    op.drop_table("part_pool_member")
    op.drop_table("part_pool")
