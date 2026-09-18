"""M1-4：WBDD 展示补全列迁移契约（plan v1.3 §3；模仿既有 *_migration 测试家族）。"""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.script import ScriptDirectory
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect

from app.db import engine

_ROOT = Path(__file__).resolve().parents[1]
_REVISION = "b4c8d2e6f1a3"
_PREVIOUS = "f1b3d5e7a9c2"


def _load_migration():
    path = _ROOT / "alembic" / "versions" / f"{_REVISION}_wbdd_display_columns.py"
    spec = importlib.util.spec_from_file_location("mig_wbdd_cols", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_new_revision_is_additive_child_and_single_head():
    cfg = AlembicConfig(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision(_REVISION)
    assert rev.down_revision == _PREVIOUS
    # 全链单 head（本计划两个迁移线性追加，不开分叉）。
    # head 随链前进更新：b1d4f6a8c2e7 = 维保负责人角色+行键（客户反馈 2026-08-21）。
    assert list(script.get_heads()) == ["b1d4f6a8c2e7"]


def test_migration_declares_exact_34_plus_28_columns():
    mod = _load_migration()
    assert len(mod._ORDER_COLUMNS) == 34
    assert len(mod._LINE_COLUMNS) == 28
    # 与模型 / plan §3 清单逐名核对（顺序无关）
    order_names = {n for n, _ in mod._ORDER_COLUMNS}
    line_names = {n for n, _ in mod._LINE_COLUMNS}
    from app.etl import mapping
    assert order_names == set(mapping.MAINTENANCE_HEAD_DISPLAY_FIELDS)
    assert line_names == set(mapping.MAINTENANCE_LINE_DISPLAY_FIELDS)


def test_columns_exist_nullable_and_no_new_indexes(db):
    insp = inspect(engine)
    order_cols = {c["name"]: c for c in insp.get_columns("f_maintenance_order")}
    line_cols = {c["name"]: c for c in insp.get_columns("f_maintenance_line")}
    from app.etl import mapping
    for name in mapping.MAINTENANCE_HEAD_DISPLAY_FIELDS:
        assert name in order_cols, name
        assert order_cols[name]["nullable"] is True, name
    for name in mapping.MAINTENANCE_LINE_DISPLAY_FIELDS:
        assert name in line_cols, name
        assert line_cols[name]["nullable"] is True, name
    # 纯加法：事实表命名索引集不因本迁移缩小/改名（唯一约束自动索引 *_key 除外）。
    # 不钉死全集：后续迁移（如 c5d7e9f1a3b5 行级作废的 ix_ml_active/ix_ml_order_active）
    # 允许继续加索引，这里只守护本迁移没有移除或更名任何既有索引。
    order_idx = {i["name"] for i in insp.get_indexes("f_maintenance_order")
                 if not i["name"].endswith("_key")}
    assert {"ix_mo_order_no", "ix_mo_linked", "ix_mo_project",
            "ix_mo_status_date"} <= order_idx
    line_idx = {i["name"] for i in insp.get_indexes("f_maintenance_line")
                if not i["name"].endswith("_key")}
    assert {"ix_ml_order", "ix_ml_part"} <= line_idx


def test_receipt_table_shape(db):
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("maintenance_wbdd_import_receipt")}
    assert set(cols) == {"id", "batch_id", "idempotency_key", "uploaded_by",
                         "file_hash", "layout", "report_json", "created_at"}
    for required in ("batch_id", "idempotency_key", "uploaded_by", "file_hash",
                     "report_json", "created_at"):
        assert cols[required]["nullable"] is False, required
    uniques = {u["name"]: u for u in
               insp.get_unique_constraints("maintenance_wbdd_import_receipt")}
    assert "uq_maintenance_wbdd_import_idempotency" in uniques
    assert uniques["uq_maintenance_wbdd_import_idempotency"]["column_names"] == [
        "uploaded_by", "idempotency_key"]


def test_selected_column_types(db):
    """类型抽查：Qty=Numeric(14,3)、Boolean、Date、String 定长、Text。"""
    insp = inspect(engine)
    order_cols = {c["name"]: c["type"] for c in insp.get_columns("f_maintenance_order")}
    line_cols = {c["name"]: c["type"] for c in insp.get_columns("f_maintenance_line")}
    assert isinstance(order_cols["head_demand_qty"], sa.Numeric)
    assert order_cols["head_demand_qty"].precision == 14
    assert order_cols["head_demand_qty"].scale == 3
    assert isinstance(order_cols["change_warehouse_flag"], sa.Boolean)
    assert isinstance(order_cols["supply_deadline"], sa.Date)
    assert isinstance(order_cols["receiver_phone"], sa.String)
    assert order_cols["receiver_phone"].length == 32
    assert isinstance(order_cols["receiver_address"], sa.Text)
    assert isinstance(line_cols["consumed_qty"], sa.Numeric)
    assert isinstance(line_cols["adjust_warehouse_flag"], sa.Boolean)
    assert isinstance(line_cols["line_note"], sa.Text)
    assert isinstance(line_cols["whole_or_part"], sa.String)
    assert line_cols["whole_or_part"].length == 8
