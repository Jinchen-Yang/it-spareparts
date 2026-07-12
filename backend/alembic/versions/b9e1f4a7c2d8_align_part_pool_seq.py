"""对齐 part_pool_group_id_seq 到存活 ID 与历史序列高水位的较大值。

Revision ID: b9e1f4a7c2d8
Revises: d3e8f1a6b2c4
Create Date: 2026-07-12

d3e8f1a6b2c4 建序列时固定 START 1，未对齐现存 group_id。**已执行过 d3 的环境**
（序列停在 1、但 part_pool 可能已有更大 group_id）无法靠改 d3 修复——Alembic 不会
重跑已应用的 revision。故新增本**后续**迁移，作为独立 revision 执行 setval 对齐：
- 空表 → 下一个 nextval=1；
- 有数据（max=N>0）→ 下一个 nextval=N+1，杜绝与现存 group_id 碰撞。
在已升级过 d3、且已写入池数据的库上升级后首次 nextval 必然 > 现存最大 group_id；
即使退役池让存活 MAX 变小，也不会复用历史已分配的序列值。
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
    # 只把 is_called=true 的 last_value 视作“已分配”高水位；false 表示该值尚未
    # 被 nextval 分配（例如刚 CREATE/RESTART 的空序列）。空表因此保持 nextval=1。
    # 对非空序列用 setval(high_watermark, true)，保证下一值严格大于存活 MAX
    # 与历史已分配最高值，避免退役池 ID 被复用。
    op.execute(
        """
        DO $$
        DECLARE
            live_max bigint;
            sequence_last bigint;
            sequence_called boolean;
            high_watermark bigint;
        BEGIN
            SELECT MAX(group_id) INTO live_max FROM part_pool;
            SELECT last_value, is_called
              INTO sequence_last, sequence_called
              FROM part_pool_group_id_seq;
            high_watermark := GREATEST(
                COALESCE(live_max, 0),
                CASE WHEN sequence_called THEN sequence_last ELSE 0 END
            );
            IF high_watermark > 0 THEN
                PERFORM setval('part_pool_group_id_seq', high_watermark, true);
            ELSE
                PERFORM setval('part_pool_group_id_seq', 1, false);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # 对齐不可逆（无法还原"未对齐"），且回退无业务意义。序列本身由 d3 的 downgrade 负责删除。
    pass
