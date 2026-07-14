"""迁移生命周期测试（复审三轮 P0-2）：证明"已执行过 d3 的环境"升级到 head 后，
后续迁移 b9e1f4a7c2d8 **真正被 Alembic 执行**并把序列对齐到现存 MAX(group_id)。

不是复制迁移 SQL 手动执行，而是走 alembic_command.downgrade/upgrade 真跑 revision——
正是这一点上一轮漏测：改旧 revision d3 在已应用 d3 的库上不会重跑。
"""
import os

import pytest

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from app.db import engine


def _cfg():
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


@pytest.fixture()
def preserve_pool_seq(migrated):
    """保存并恢复 part_pool_group_id_seq 状态（复审五轮 Standards：Postgres 序列不随
    事务回滚，凡是直接操纵序列（ALTER/setval）或依赖其初始值的测试都必须挂本 fixture，
    否则测试结束把序列留在任意水位 → 跨用例顺序依赖）。"""
    with engine.begin() as conn:
        prev = conn.execute(text(
            "SELECT last_value, is_called FROM part_pool_group_id_seq")).one()
    yield
    with engine.begin() as conn:
        conn.execute(text("SELECT setval('part_pool_group_id_seq', :lv, :ic)"),
                     {"lv": prev.last_value, "ic": prev.is_called})


@pytest.mark.parametrize(
    ("group_id", "sequence_sql", "expected_next"),
    [
        (None, "ALTER SEQUENCE part_pool_group_id_seq RESTART WITH 1", 1),
        (40, "ALTER SEQUENCE part_pool_group_id_seq RESTART WITH 1", 41),
        (40, "SELECT setval('part_pool_group_id_seq', 100, true)", 101),
    ],
    ids=["empty-un-called", "sequence-lagging", "sequence-high-watermark"],
)
def test_followup_migration_preserves_sequence_high_watermark(
    preserve_pool_seq, group_id, sequence_sql, expected_next
):
    """已在 d3 的环境升级 b9 后，序列不得因存活池删除而回退。

    有池场景造两个真实成员，不用只改 member_count 的不可能状态绕过人工池
    迁移的全局不变量。"""
    cfg = _cfg()
    alembic_command.downgrade(cfg, "d3e8f1a6b2c4")   # 回到"后续对齐迁移未应用"的状态
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM part_pool_member"))
            conn.execute(text("DELETE FROM part_pool"))
            if group_id is not None:
                conn.execute(text(
                    "INSERT INTO part_pool (group_id, member_count, needs_calibration, oversized) "
                    "VALUES (:group_id, 2, false, false)"), {"group_id": group_id})
                for i in range(2):
                    part_id = conn.execute(text(
                        "INSERT INTO dim_part (pn_std) VALUES (:pn) RETURNING id"),
                        {"pn": f"MIGSEQ-{group_id}-{i}"}).scalar()
                    conn.execute(text(
                        "INSERT INTO part_pool_member (part_id, group_id) "
                        "VALUES (:part_id, :group_id)"),
                        {"part_id": part_id, "group_id": group_id})
            conn.execute(text(sequence_sql))
        alembic_command.upgrade(cfg, "head")          # Alembic 执行后续迁移 b9e1f4a7c2d8
        with engine.begin() as conn:
            nxt = conn.execute(text("SELECT nextval('part_pool_group_id_seq')")).scalar()
        assert nxt == expected_next
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM part_pool_member"))
            conn.execute(text("DELETE FROM part_pool"))
            conn.execute(text("DELETE FROM dim_part WHERE pn_std LIKE 'MIGSEQ-%'"))
        alembic_command.upgrade(cfg, "head")          # 回到 head；序列状态由 preserve_pool_seq 恢复


def test_single_alembic_head(migrated):
    """新增后续迁移后仍是单一 head（没修改旧 revision 造成分叉）。"""
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(_cfg()).get_heads()
    # 钉住当前 head：看板 v2 行表 order_id 索引迁移（a9c5e2f7d4b1 ← a3f8c1d9e5b2 权限中心 v2）
    assert heads == ["a9c5e2f7d4b1"], f"应只有一个 head=a9c5e2f7d4b1，实得 {heads}"
