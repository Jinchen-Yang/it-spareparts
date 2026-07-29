"""系统业务设置迁移：真实 downgrade/re-upgrade 验证建表、种值和回滚。"""

import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

_PREV = "c9d4e7f2a6b1"
_HEAD = "d4e8f1a2b3c4"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_business_setting_upgrade_seed_and_downgrade(db):
    engine = db.get_bind()
    db.close()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as connection:
            assert "sys_business_setting" not in inspect(connection).get_table_names()

        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT id, maintenance_project_profit_default_basis, version,"
                " updated_by, updated_at"
                " FROM sys_business_setting",
            )).mappings().one()
            assert dict(row) == {
                "id": 1,
                "maintenance_project_profit_default_basis": "both",
                "version": 1,
                "updated_by": None,
                "updated_at": None,
            }
            assert connection.execute(
                text("SELECT version_num FROM alembic_version"),
            ).scalar_one() == _HEAD

        alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as connection:
            assert "sys_business_setting" not in inspect(connection).get_table_names()
    finally:
        alembic_command.upgrade(cfg, "head")
