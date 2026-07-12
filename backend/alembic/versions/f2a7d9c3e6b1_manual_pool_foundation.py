"""人工池地基（互通PN池价格分析 §15/§21 Slice 1）。

Revision ID: f2a7d9c3e6b1
Revises: b9e1f4a7c2d8
Create Date: 2026-07-13

池从「自动重算的连通分量」转为「人工创建维护的唯一真值」：
1. part_pool 增加人可读池名/说明/状态/来源/乐观锁版本/维护人字段；
   存量池回填 name='互通池-{ID}'、source='legacy_generated'，ID 与成员集合零变化。
2. part_pool_member 主键 part_id → (group_id, part_id)：池只归档不硬删除，
   归档池保留成员集合、其成员可再加入新的有效池；「一个有效 PN 只属一个有效池」
   由 pool_catalog 写路径保证。
3. 新建 part_pool_price_policy 约束价历史表：统一未税上限/下限 + 原始录入值口径，
   部分唯一索引保证每池仅一条 valid_to IS NULL 的当前策略。
4. 复核 part_pool_group_id_seq 高水位（同 b9e1f4a7c2d8，幂等）：
   下一 nextval 严格大于所有历史 group_id。
只增加池与约束配置，不改任何采购/销售事实数据。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a7d9c3e6b1"
down_revision: Union[str, Sequence[str], None] = "b9e1f4a7c2d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TZ = sa.DateTime(timezone=True)
_MONEY = sa.Numeric(14, 2)


def upgrade() -> None:
    # ---- 1. part_pool 人工池字段（先加可空 → 回填 → 收紧非空） ----
    op.add_column("part_pool", sa.Column("name", sa.String(128), nullable=True))
    op.add_column("part_pool", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("part_pool", sa.Column(
        "status", sa.String(16), nullable=False, server_default="active"))
    # server_default 先设 legacy_generated 让存量行回填，随后改成 manual 作为未来默认
    op.add_column("part_pool", sa.Column(
        "source", sa.String(32), nullable=False, server_default="legacy_generated"))
    op.add_column("part_pool", sa.Column(
        "version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("part_pool", sa.Column("created_by", sa.String(64), nullable=True))
    op.add_column("part_pool", sa.Column("updated_by", sa.String(64), nullable=True))
    op.add_column("part_pool", sa.Column("created_at", _TZ, nullable=True))

    op.execute("UPDATE part_pool SET name = '互通池-' || group_id WHERE name IS NULL")
    op.execute("UPDATE part_pool SET created_at = COALESCE(updated_at, now()) WHERE created_at IS NULL")
    op.alter_column("part_pool", "name", nullable=False)
    op.alter_column("part_pool", "created_at", nullable=False, server_default=sa.text("now()"))
    op.alter_column("part_pool", "source", server_default="manual")

    op.create_check_constraint(
        "ck_part_pool_status", "part_pool", "status IN ('active','archived')")
    op.create_index("ix_part_pool_status_updated", "part_pool", ["status", "updated_at"])

    # ---- 2. part_pool_member 复合主键 + 维护字段 ----
    op.add_column("part_pool_member", sa.Column("added_by", sa.String(64), nullable=True))
    op.add_column("part_pool_member", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("part_pool_member", sa.Column("updated_at", _TZ, nullable=True))
    op.execute("UPDATE part_pool_member SET updated_at = created_at WHERE updated_at IS NULL")
    op.alter_column("part_pool_member", "updated_at", nullable=False, server_default=sa.text("now()"))

    op.drop_constraint("part_pool_member_pkey", "part_pool_member", type_="primary")
    op.create_primary_key("part_pool_member_pkey", "part_pool_member", ["group_id", "part_id"])
    op.create_index("ix_pool_member_part", "part_pool_member", ["part_id"])

    # ---- 3. 约束价历史表 ----
    op.create_table(
        "part_pool_price_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.Integer(),
                  sa.ForeignKey("part_pool.group_id", ondelete="CASCADE"), nullable=False),
        sa.Column("purchase_ceiling_ex_tax", _MONEY, nullable=True),
        sa.Column("sales_floor_ex_tax", _MONEY, nullable=True),
        sa.Column("purchase_input_value", _MONEY, nullable=True),
        sa.Column("purchase_input_basis", sa.String(8), nullable=True),
        sa.Column("sales_input_value", _MONEY, nullable=True),
        sa.Column("sales_input_basis", sa.String(8), nullable=True),
        sa.Column("valid_from", _TZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("valid_to", _TZ, nullable=True),
        sa.Column("changed_by", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", _TZ, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "purchase_input_basis IS NULL OR purchase_input_basis IN ('ex_tax','inc_tax')",
            name="ck_pool_policy_purchase_basis"),
        sa.CheckConstraint(
            "sales_input_basis IS NULL OR sales_input_basis IN ('ex_tax','inc_tax')",
            name="ck_pool_policy_sales_basis"),
    )
    op.create_index("ix_pool_policy_group", "part_pool_price_policy", ["group_id"])
    op.create_index(
        "uq_pool_policy_current", "part_pool_price_policy", ["group_id"],
        unique=True, postgresql_where=sa.text("valid_to IS NULL"))
    op.create_index(
        "ix_pool_policy_group_from", "part_pool_price_policy",
        ["group_id", sa.text("valid_from DESC")])

    # ---- 4. 序列高水位复核（幂等，同 b9e1f4a7c2d8）----
    op.execute("CREATE SEQUENCE IF NOT EXISTS part_pool_group_id_seq START 1")
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
    op.drop_index("ix_pool_policy_group_from", table_name="part_pool_price_policy")
    op.drop_index("uq_pool_policy_current", table_name="part_pool_price_policy")
    op.drop_index("ix_pool_policy_group", table_name="part_pool_price_policy")
    op.drop_table("part_pool_price_policy")

    op.drop_index("ix_pool_member_part", table_name="part_pool_member")
    op.drop_constraint("part_pool_member_pkey", "part_pool_member", type_="primary")
    # 若归档池数据已让同一 part 属于多个池，恢复单列主键会失败——属预期，
    # 带数据的生产回滚以数据库备份恢复为准（部署 runbook 铁律）。
    op.create_primary_key("part_pool_member_pkey", "part_pool_member", ["part_id"])
    op.drop_column("part_pool_member", "updated_at")
    op.drop_column("part_pool_member", "note")
    op.drop_column("part_pool_member", "added_by")

    op.drop_index("ix_part_pool_status_updated", table_name="part_pool")
    op.drop_constraint("ck_part_pool_status", "part_pool", type_="check")
    op.drop_column("part_pool", "created_at")
    op.drop_column("part_pool", "updated_by")
    op.drop_column("part_pool", "created_by")
    op.drop_column("part_pool", "version")
    op.drop_column("part_pool", "source")
    op.drop_column("part_pool", "status")
    op.drop_column("part_pool", "description")
    op.drop_column("part_pool", "name")
