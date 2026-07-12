"""通用号池稳定 group_id 序列（复审 P0-2：退役 ID 永不复用）

Revision ID: d3e8f1a6b2c4
Revises: c1a7f0d5e2b9
Create Date: 2026-07-11

pool.rebuild 用 nextval('part_pool_group_id_seq') 取新池 ID。单调递增，池退役后其 ID
不会被无关新池复用（旧实现用 max(存活ID)+1 会复用退役 ID → "稳定池 ID"名不副实）。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d3e8f1a6b2c4"
down_revision: Union[str, Sequence[str], None] = "c1a7f0d5e2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS part_pool_group_id_seq START 1")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS part_pool_group_id_seq")
