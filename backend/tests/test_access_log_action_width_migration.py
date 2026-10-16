"""baff4b809f12 迁移验收：action 扩宽 varchar(64)、降级遇超长行明确拒绝。

降级策略（不静默截断）：存在 >32 字符的 action 行时 downgrade 报错并给出
行数，由运维决定导出/清理后再降；空表/短名可直接往返。
"""
import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine

_PREV = "a40c9e1d7b52"
_THIS = "baff4b809f12"


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location",
                        os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


def _action_width() -> int:
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT character_maximum_length FROM information_schema.columns"
            " WHERE table_name = 'sys_access_log' AND column_name = 'action'"
        )).scalar_one()


def _seed_long_action_rows() -> None:
    # 两条超 32：一条 43（现存最长动作名），一条 33（边界 +1）。
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO sys_access_log (username, role, action)"
            " VALUES ('acl-width-u', 'readonly', 'maintenance_warehouse_integration_reconcile'),"
            "        ('acl-width-u', 'readonly', 'a' || repeat('x', 32))"))


def test_upgrade_widens_action_and_downgrade_roundtrip(db):
    db.rollback()  # 让出夹具会话快照，迁移走独立连接
    cfg = _alembic_cfg()

    # head 状态：列宽 64（模型 String(64) 与迁移一致，alembic check 零漂移）。
    assert _action_width() == 64

    # 降级守卫：存在超 32 字符的行时拒绝，报错带行数与处理指引，不静默截断。
    _seed_long_action_rows()
    with pytest.raises(DBAPIError, match="downgrade blocked.*2 rows"):
        alembic_command.downgrade(cfg, _PREV)
    assert _action_width() == 64  # 被拒后列宽不变

    # 清掉超长行后可正常降级回 32，再升回 head 恢复 64。
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM sys_access_log WHERE username = 'acl-width-u'"))
    alembic_command.downgrade(cfg, _PREV)
    assert _action_width() == 32
    alembic_command.upgrade(cfg, "head")
    assert _action_width() == 64
