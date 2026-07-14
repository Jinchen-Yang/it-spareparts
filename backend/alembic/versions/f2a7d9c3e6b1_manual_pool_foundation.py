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

索引口径（复审阻塞 5，与 ORM 元数据严格对齐，alembic check 必须零漂移）：
- part_pool_member 复合主键前导列即 group_id → 删除旧单列索引 ix_pool_member_group；
- part_pool_price_policy 不建 group_id 单列索引（ix_pool_policy_group_from 前导列覆盖）。

downgrade **不是无条件可逆**：只有 upgrade 后从未产生人工建池、池/成员维护、
一 PN 多池或约束价历史时才无损；否则守卫失败即停、不删任何数据。生产回滚
一律恢复迁移前数据库备份。
应用过本迁移旧版本（2026-07-13 未合并版）的开发库：直接重建，或 downgrade 到
b9e1f4a7c2d8 再 upgrade（索引操作带 IF [NOT] EXISTS，可安全重放）。
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
    # ---- 0. 全局不变量：有效池至少 2 个实际成员 ----
    # 在任何 DDL 之前检查旧 schema，不信任可能漂移的 member_count 冗余值。
    # 若存在单成员/空池，必须先治理再升级；PostgreSQL 事务性 DDL 保证失败后
    # schema 和数据都仍停在上一版。
    bind = op.get_bind()
    undersized = bind.execute(sa.text(
        "SELECT p.group_id, COUNT(m.part_id) AS actual_member_count "
        "FROM part_pool p LEFT JOIN part_pool_member m ON m.group_id=p.group_id "
        "GROUP BY p.group_id HAVING COUNT(m.part_id) < 2 ORDER BY p.group_id"
    )).all()
    if undersized:
        detail = ", ".join(
            f"group_id={row.group_id}(实际成员={row.actual_member_count})"
            for row in undersized
        )
        raise RuntimeError(
            "upgrade f2a7d9c3e6b1 中止（未执行任何 DDL）：有效池必须至少"
            f"包含 2 个 PN，请先治理以下历史池：{detail}"
        )

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
    # 旧单列索引（c1a7f0d5e2b9 创建）随主键改造变纯冗余：新复合主键前导列即 group_id。
    # IF EXISTS：容忍应用过本迁移旧版本（未删此索引）的开发库重放（复审阻塞 5）
    op.execute("DROP INDEX IF EXISTS ix_pool_member_group")

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
    # 不建 group_id 单列索引：ix_pool_policy_group_from 前导列已覆盖（复审阻塞 5，
    # 与 ORM 元数据对齐，alembic check 必须零漂移）
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
    # ---- 数据丢失守卫：本 downgrade 只在"未产生本迁移之后的新数据"时才是无损的 ----
    # 1) 同一 part_id 已属多个池（归档 A → A 的成员加入新有效池 B 的正常使用轨迹）：
    #    恢复单列主键 (part_id) 必然 UniqueViolation；
    # 2) part_pool_price_policy 已有约束价历史：drop_table 会把它整个删掉；
    # 3) 人工建池/改名/说明/归档/版本或维护人变更：旧 schema 没有这些字段；
    # 4) 成员的 added_by/note/updated_at 已变：旧 schema 同样无法表达。
    # 任一情况都**失败即停**（事务回滚，什么都不删），生产回滚一律恢复
    # 迁移前数据库备份，绝不静默丢历史。
    bind = op.get_bind()
    multi_pool_parts = bind.execute(sa.text(
        "SELECT COUNT(*) FROM (SELECT part_id FROM part_pool_member "
        "GROUP BY part_id HAVING COUNT(*) > 1) t")).scalar()
    policy_rows = bind.execute(sa.text(
        "SELECT COUNT(*) FROM part_pool_price_policy")).scalar()
    changed_pools = bind.execute(sa.text(
        "SELECT group_id FROM part_pool WHERE "
        "source IS DISTINCT FROM 'legacy_generated' "
        "OR name IS DISTINCT FROM ('互通池-' || group_id::text) "
        "OR description IS NOT NULL OR status IS DISTINCT FROM 'active' "
        "OR version IS DISTINCT FROM 1 OR created_by IS NOT NULL OR updated_by IS NOT NULL "
        "OR created_at IS DISTINCT FROM updated_at ORDER BY group_id"
    )).scalars().all()
    changed_member_pools = bind.execute(sa.text(
        "SELECT DISTINCT group_id FROM part_pool_member WHERE "
        "added_by IS NOT NULL OR note IS NOT NULL OR updated_at IS DISTINCT FROM created_at "
        "ORDER BY group_id"
    )).scalars().all()
    if multi_pool_parts or policy_rows or changed_pools or changed_member_pools:
        raise RuntimeError(
            "downgrade f2a7d9c3e6b1 中止（未做任何改动）：检测到本迁移之后产生的业务数据——"
            f"{multi_pool_parts} 个 PN 属于多个池（无法恢复 part_id 单列主键）、"
            f"{policy_rows} 条约束价历史（drop 表会永久丢失）、"
            f"人工池/池元数据已变更 group_id={list(changed_pools)}、"
            f"成员维护元数据已变更 group_id={list(changed_member_pools)}。"
            "此状态下 schema 降级不是无损回滚：生产环境请恢复迁移前的数据库备份；"
            "开发环境确认可丢弃后，先恢复或清理所有迁移后业务数据再重试。")

    op.drop_index("ix_pool_policy_group_from", table_name="part_pool_price_policy")
    op.drop_index("uq_pool_policy_current", table_name="part_pool_price_policy")
    op.drop_table("part_pool_price_policy")

    op.drop_index("ix_pool_member_part", table_name="part_pool_member")
    op.drop_constraint("part_pool_member_pkey", "part_pool_member", type_="primary")
    op.create_primary_key("part_pool_member_pkey", "part_pool_member", ["part_id"])
    # 恢复 c1a7f0d5e2b9 时代的单列索引（IF NOT EXISTS 容忍旧版本迁移遗留）
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pool_member_group ON part_pool_member (group_id)")
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
