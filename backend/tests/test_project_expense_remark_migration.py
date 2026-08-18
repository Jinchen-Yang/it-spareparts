"""P2：`f_project_expense.remark` 迁移契约（REQUIREMENTS #47）。

业务 2026-08-16 批准这 1 条迁移，要求**纯加法 nullable、追加链尾、保持线性**
（M0-E 整链发布口径：只增不改）。
"""
import importlib.util
import os
from pathlib import Path

import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db import engine

_ROOT = Path(__file__).resolve().parents[1]
_REVISION = "d6e1f4a8c3b5"
_PREVIOUS = "c5d9e3f7a2b4"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


def _load_migration():
    path = _ROOT / "alembic" / "versions" / f"{_REVISION}_project_expense_remark.py"
    spec = importlib.util.spec_from_file_location("mig_expense_remark", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_is_appended_at_the_tail_and_chain_stays_linear():
    script = ScriptDirectory.from_config(_cfg())
    rev = script.get_revision(_REVISION)
    assert rev.down_revision == _PREVIOUS, "必须追加在链尾（只增不改）"
    # 链必须单 head（后续迁移可继续追加，但不许开分叉）
    assert len(script.get_heads()) == 1, "链必须保持单 head，不开分叉"


def test_migration_is_strictly_additive():
    """纯加法：只有一次 add_column，没有回填/索引/约束改动。"""
    source = (_ROOT / "alembic" / "versions"
              / f"{_REVISION}_project_expense_remark.py").read_text()
    upgrade = source.split("def upgrade()")[1].split("def downgrade()")[0]
    assert upgrade.count("op.add_column") == 1
    for banned in ("op.execute", "op.create_index", "op.drop_", "op.alter_column",
                   "server_default"):
        assert banned not in upgrade, f"upgrade 不得出现 {banned}"


def test_column_exists_nullable_and_is_text(db):
    columns = {c["name"]: c for c in inspect(engine).get_columns("f_project_expense")}
    assert "remark" in columns
    assert columns["remark"]["nullable"] is True
    assert isinstance(columns["remark"]["type"], sa.Text)


def test_no_new_index_on_the_fact_table(db):
    """备注是展示列，不该为它加索引（纯加法的一部分）。"""
    # 排除唯一约束自动带的 *_key 索引，只看显式命名的业务索引
    names = {i["name"] for i in inspect(engine).get_indexes("f_project_expense")
             if not i["name"].endswith("_key")}
    assert names == {"ix_pe_bxd", "ix_pe_linked", "ix_pe_status_date"}


def test_downgrade_upgrade_roundtrip(db):
    """upgrade↔downgrade 往返可跑（迁移测试家族口径；生产回滚仍是关 flag）。

    注意：downgrade 掉的列在 Postgres 里仍占列槽位，conftest 的槽位守卫负责在
    逼近 1600 前重建 schema——这条用例本身不需要额外处理。
    """
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREVIOUS)
    try:
        with engine.connect() as conn:
            present = conn.execute(text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'f_project_expense' AND column_name = 'remark'"
            )).scalar_one()
        assert present == 0, "downgrade 后该列应当不存在"
    finally:
        alembic_command.upgrade(cfg, "head")
    with engine.connect() as conn:
        restored = conn.execute(text(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_name = 'f_project_expense' AND column_name = 'remark'"
        )).scalar_one()
    assert restored == 1


def test_existing_rows_keep_null_remark(db):
    """纯加法无回填：既有行的备注保持 NULL，不被塞入空串或占位符。"""
    mod = _load_migration()
    assert mod.revision == _REVISION and mod.down_revision == _PREVIOUS
    with engine.connect() as conn:
        nulls = conn.execute(text(
            "SELECT count(*) FROM f_project_expense WHERE remark IS NOT NULL"
        )).scalar_one()
    assert nulls == 0
