"""对齐 part_pool_group_id_seq 到现存 MAX(group_id)（复审三轮 P0-2）

Revision ID: b9e1f4a7c2d8
Revises: d3e8f1a6b2c4
Create Date: 2026-07-12

d3e8f1a6b2c4 建序列时固定 START 1，未对齐现存 group_id。**已执行过 d3 的环境**
（序列停在 1、但 part_pool 可能已有更大 group_id）无法靠改 d3 修复——Alembic 不会
重跑已应用的 revision。故新增本**后续**迁移，作为独立 revision 执行 setval 对齐：
- 空表 → 下一个 nextval=1；
- 有数据（max=N>0）→ 下一个 nextval=N+1，杜绝与现存 group_id 碰撞。
在已升级过 d3、且已写入池数据的库上升级后首次 nextval 必然 > 现存最大 group_id。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b9e1f4a7c2d8"
down_revision: Union[str, Sequence[str], None] = "d3e8f1a6b2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 序列一般已由 d3 建好；IF NOT EXISTS 兜底 d3 因故未执行的环境。
    op.execute("CREATE SEQUENCE IF NOT EXISTS part_pool_group_id_seq START 1")
    # setval(seq, v, is_called)：is_called=false → 下个 nextval=v；true → =v+1。
    # 空表 MAX→NULL→COALESCE 0：setval(...,1,false) → 首个 nextval=1；
    # 有数据 max=N>0：setval(...,N,true) → 首个 nextval=N+1。
    op.execute(
        "SELECT setval('part_pool_group_id_seq', "
        "GREATEST(COALESCE((SELECT MAX(group_id) FROM part_pool), 0), 1), "
        "COALESCE((SELECT MAX(group_id) FROM part_pool), 0) > 0)"
    )


def downgrade() -> None:
    # 对齐不可逆（无法还原"未对齐"），且回退无业务意义。序列本身由 d3 的 downgrade 负责删除。
    pass
