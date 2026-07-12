"""通用号池稳定 group_id 序列（复审 P0-2：退役 ID 永不复用）

Revision ID: d3e8f1a6b2c4
Revises: c1a7f0d5e2b9
Create Date: 2026-07-11

pool.rebuild 用 nextval('part_pool_group_id_seq') 取新池 ID。单调递增，池退役后其 ID
不会被无关新池复用（旧实现用 max(存活ID)+1 会复用退役 ID → "稳定池 ID"名不副实）。

复审二轮 Hard/P0-2：序列不能固定 START 1。若 part_pool 已有数据（中间版本、重跑、
已存在环境），nextval 从 1 起会与现存 group_id 碰撞（reviewer 复现：max=1 时 nextval=1）。
故建表后立刻 setval 对齐到现存 MAX(group_id)：空表→首个 nextval=1；有数据→从 max+1 起。
幂等：CREATE IF NOT EXISTS + 每次 setval 都按当前 max 重新对齐。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d3e8f1a6b2c4"
down_revision: Union[str, Sequence[str], None] = "c1a7f0d5e2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS part_pool_group_id_seq START 1")
    # 对齐到现存最大 group_id，杜绝与已有池 ID 碰撞。
    # setval(seq, v, is_called)：is_called=false → 下个 nextval=v；true → =v+1。
    # 空表 MAX→NULL→COALESCE 0：setval(...,1,false) 使首个 nextval=1；
    # 有数据 max=N>0：setval(...,N,true) 使首个 nextval=N+1。
    op.execute(
        "SELECT setval('part_pool_group_id_seq', "
        "GREATEST(COALESCE((SELECT MAX(group_id) FROM part_pool), 0), 1), "
        "COALESCE((SELECT MAX(group_id) FROM part_pool), 0) > 0)"
    )


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS part_pool_group_id_seq")
