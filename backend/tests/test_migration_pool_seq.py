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
    migrated, group_id, sequence_sql, expected_next
):
    """已在 d3 的环境升级 b9 后，序列不得因存活池删除而回退。"""
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
            conn.execute(text(sequence_sql))
        alembic_command.upgrade(cfg, "head")          # Alembic 执行后续迁移 b9e1f4a7c2d8
        with engine.begin() as conn:
            nxt = conn.execute(text("SELECT nextval('part_pool_group_id_seq')")).scalar()
        assert nxt == expected_next
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM part_pool_member"))
            conn.execute(text("DELETE FROM part_pool"))
        alembic_command.upgrade(cfg, "head")          # 确保回到 head，不污染后续用例


def test_single_alembic_head(migrated):
    """新增后续迁移后仍是单一 head（没修改旧 revision 造成分叉）。"""
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(_cfg()).get_heads()
    assert heads == ["b9e1f4a7c2d8"], f"应只有一个 head=b9e1f4a7c2d8，实得 {heads}"
