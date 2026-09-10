"""Migration contract for explicit maintenance salesperson overrides."""

from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db import engine


_ROOT = Path(__file__).resolve().parents[1]
_REVISION = "f6b1d3e8a2c4"
_PREVIOUS = "e4a8c2f6b1d9"
# 2026-09-02 基座（fix/contract-total-inc-tax）：a8e4 期限回填迁移线性追加；
# 2026-09-07 #288 收款单台账 b7d3 线性追加（D-16）。全链仍单 head。
# 2026-09-09 现场领用需求单引用 c9e5 线性追加。
_HEAD = "c9e5a1b7d3f8"


def test_salesperson_override_revision_is_linear_single_head():
    config = AlembicConfig(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    revision = script.get_revision(_REVISION)
    assert revision.down_revision == _PREVIOUS
    assert list(script.get_heads()) == [_HEAD]


def test_salesperson_override_column_is_nonnull_false_by_default(db):
    column = next(
        item
        for item in inspect(engine).get_columns("maintenance_project")
        if item["name"] == "salesperson_override_active"
    )

    assert column["nullable"] is False
    assert str(column["default"]).lower() == "false"
