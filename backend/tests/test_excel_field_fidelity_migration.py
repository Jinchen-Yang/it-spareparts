"""D-0050：c7e1a3b5d9f2（维保展示文本扩 Text + adjust_warehouse_raw）回滚契约。

- 空表/仅短数据：downgrade→upgrade 可往返，短值原样保留。
- 任一会收窄的列已有超长值：downgrade 必须拒绝，事务回滚后 revision、schema、
  值都保持 upgrade 后状态（不允许静默截断或留下半降级 schema）。
- 新增 adjust_warehouse_raw 已承载非空原文：downgrade 拒绝删除该列。

隔离与 conftest 口径：使用测试库（db fixture 建 schema/清表），不连接生产库。
"""
import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.system import SysImportBatch

_REVISION = "c7e1a3b5d9f2"
_PREVIOUS = "a3c5e7f9b2d4"
_BATCH_HASH = "c" * 64


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


def _column(table: str, name: str):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT data_type, character_maximum_length"
            " FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ), {"t": table, "c": name}).first()


def _revision() -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _seed_batch(db) -> int:
    batch = SysImportBatch(
        filename="migration-contract.xlsx", file_type="maintenance",
        file_hash=_BATCH_HASH, status="processing",
    )
    db.add(batch)
    db.flush()
    return batch.id


def _seed_order(db, *, receiver="短收货人") -> int:
    order = FMaintenanceOrder(
        raw_order_id="MIG-ORDER-1",
        order_no="WBDD-MIG-1",
        import_batch_id=_seed_batch(db),
        receiver=receiver,
        maintainer_raw="张三",
        whole_machine_check="是",
    )
    db.add(order)
    db.flush()
    return order.id


def _seed_line(db, *, adjust_warehouse="短仓", adjust_warehouse_raw=None) -> int:
    order_id = _seed_order(db)
    part = DimPart(pn_std="MIG-LINE-PN")
    db.add(part)
    db.flush()
    line = FMaintenanceLine(
        raw_line_id="MIG-LINE-1",
        order_id=order_id,
        part_id=part.id,
        adjust_warehouse=adjust_warehouse,
        adjust_warehouse_raw=adjust_warehouse_raw,
        return_old_part="旧件",
    )
    db.add(line)
    db.flush()
    return line.id


def _scalar(sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_empty_tables_roundtrip_downgrade_upgrade(db):
    alembic_command.downgrade(_cfg(), _PREVIOUS)
    try:
        assert _revision() == _PREVIOUS
        assert _column("f_maintenance_line", "adjust_warehouse_raw") is None
        assert tuple(_column("f_maintenance_order", "receiver")) == (
            "character varying", 64)
    finally:
        alembic_command.upgrade(_cfg(), "head")

    assert _revision() == _REVISION
    assert tuple(_column("f_maintenance_order", "receiver")) == ("text", None)
    assert tuple(_column("f_maintenance_line", "adjust_warehouse_raw")) == (
        "text", None)


def test_short_data_survives_downgrade_upgrade(db):
    _seed_line(db)
    db.commit()

    alembic_command.downgrade(_cfg(), _PREVIOUS)
    try:
        assert _revision() == _PREVIOUS
        assert _scalar(
            "SELECT receiver FROM f_maintenance_order WHERE raw_order_id = 'MIG-ORDER-1'"
        ) == "短收货人"
        assert _scalar(
            "SELECT adjust_warehouse FROM f_maintenance_line"
            " WHERE raw_line_id = 'MIG-LINE-1'"
        ) == "短仓"
    finally:
        alembic_command.upgrade(_cfg(), "head")

    assert _revision() == _REVISION
    assert _scalar(
        "SELECT receiver FROM f_maintenance_order WHERE raw_order_id = 'MIG-ORDER-1'"
    ) == "短收货人"
    assert _scalar(
        "SELECT return_old_part FROM f_maintenance_line WHERE raw_line_id = 'MIG-LINE-1'"
    ) == "旧件"
    assert _scalar(
        "SELECT adjust_warehouse FROM f_maintenance_line"
        " WHERE raw_line_id = 'MIG-LINE-1'"
    ) == "短仓"
    # 往返新增列只能为空，不得凭空回填源文本。
    assert _scalar(
        "SELECT count(*) FROM f_maintenance_line"
        " WHERE adjust_warehouse_raw IS NOT NULL"
    ) == 0


def _assert_head_rolled_back(table: str, column: str, value_sql: str, expected: str):
    assert _revision() == _REVISION
    assert tuple(_column(table, column)) == ("text", None)
    assert _scalar(value_sql) == expected


def _expect_downgrade_rejected(db, pattern: str) -> None:
    """断言 downgrade 被迁移守卫拒绝；若实现回归导致真的降级，先恢复 head 再失败，
    避免把后续用例连坐成半降级 schema。"""
    try:
        with pytest.raises(DBAPIError, match=pattern):
            alembic_command.downgrade(_cfg(), _PREVIOUS)
    finally:
        db.rollback()
        if _revision() != _REVISION:
            alembic_command.upgrade(_cfg(), "head")


def test_overlong_head_value_rejects_downgrade(db):
    _seed_order(db, receiver="R" * 65)
    db.commit()

    _expect_downgrade_rejected(
        db, "downgrade blocked.*f_maintenance_order.receiver")

    _assert_head_rolled_back(
        "f_maintenance_order", "receiver",
        "SELECT receiver FROM f_maintenance_order WHERE raw_order_id = 'MIG-ORDER-1'",
        "R" * 65)
    # downgrade 整段回滚：新增列仍在，未被“删一半”。
    assert tuple(_column("f_maintenance_line", "adjust_warehouse_raw")) == (
        "text", None)


def test_overlong_line_value_rejects_downgrade(db):
    _seed_line(db, adjust_warehouse="A" * 65)
    db.commit()

    _expect_downgrade_rejected(
        db, "downgrade blocked.*f_maintenance_line.adjust_warehouse")

    _assert_head_rolled_back(
        "f_maintenance_line", "adjust_warehouse",
        "SELECT adjust_warehouse FROM f_maintenance_line"
        " WHERE raw_line_id = 'MIG-LINE-1'",
        "A" * 65)
    assert tuple(_column("f_maintenance_line", "adjust_warehouse_raw")) == (
        "text", None)


def test_present_adjust_warehouse_raw_rejects_downgrade(db):
    _seed_line(db, adjust_warehouse_raw="原文仓库")
    db.commit()

    _expect_downgrade_rejected(
        db,
        "downgrade blocked.*f_maintenance_line.adjust_warehouse_raw have data",
    )

    _assert_head_rolled_back(
        "f_maintenance_line", "adjust_warehouse_raw",
        "SELECT adjust_warehouse_raw FROM f_maintenance_line"
        " WHERE raw_line_id = 'MIG-LINE-1'",
        "原文仓库")
