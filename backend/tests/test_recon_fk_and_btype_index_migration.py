"""c9d4f2a6b8e1 迁移契约：快照→订单 FK 索引 + ON DELETE SET NULL、business_type 筛选索引。

覆盖（#395 检察官 / #397 (7)）：
- 迁移后：FK 显式命名且 ondelete=SET NULL、两处新索引在位；
- downgrade 往返：回到 b8f2d6c4a1e9 原状（PG 默认 FK 名、无策略、无索引）再升回来；
- 计划：禁用顺序/位图扫描后，business_type 查询可走 ix_so_status_btype 的 Index Scan；
- 行为：删除订单 → 快照存活、sales_order_id 置空（观测不级联删、不阻塞删除）。
"""
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

from app.db import engine
from app.models.sales import FSalesReconciliationSnapshot
from app.services.sales_reconciliation import snapshot_content_hash

_PREV = "a5e8c3f7b2d9"
_SNAP = "f_sales_reconciliation_snapshot"
_FK = "fk_sales_recon_snapshot_sales_order"
_LEGACY_FK = "f_sales_reconciliation_snapshot_sales_order_id_fkey"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _snapshot_fks(insp) -> dict:
    return {
        fk["name"]: fk for fk in insp.get_foreign_keys(_SNAP)
        if fk["constrained_columns"] == ["sales_order_id"]
    }


def _seed_batch(db) -> int:
    return db.execute(
        text(
            "INSERT INTO sys_import_batch (filename, file_type, file_hash, status) "
            "VALUES ('fk-mig.xlsx', 'sales', 'fk-mig-1', 'success') RETURNING id"
        )
    ).scalar_one()


def test_fk_index_and_ondelete_after_upgrade(db):
    insp = inspect(db.get_bind())
    fks = _snapshot_fks(insp)
    assert set(fks) == {_FK}
    assert fks[_FK]["options"].get("ondelete") == "SET NULL"
    assert fks[_FK]["referred_table"] == "f_sales_order"
    snap_idx = {i["name"]: i["column_names"] for i in insp.get_indexes(_SNAP)}
    assert snap_idx["ix_sales_recon_snapshot_sales_order"] == ["sales_order_id"]
    so_idx = {i["name"]: i["column_names"] for i in insp.get_indexes("f_sales_order")}
    assert so_idx["ix_so_status_btype"] == ["data_status", "business_type"]


def test_business_type_index_explain(db):
    """验证迁移索引可供筛选使用；强制计划不代表生产成本模型会自然选它。

    SET LOCAL 仅影响当前测试事务，夹具 rollback 后恢复；关闭位图扫描以固定
    Index Scan 形态，避免小型合成表上的默认 Seq Scan 或 Bitmap Heap Scan。
    """
    db.execute(text("SET LOCAL enable_seqscan = off"))
    db.execute(text("SET LOCAL enable_bitmapscan = off"))
    plan = db.execute(text(
        "EXPLAIN (FORMAT JSON) SELECT id FROM f_sales_order "
        "WHERE data_status = '已生效' AND business_type = '备件销售'"
    )).scalar_one()[0]["Plan"]
    assert plan["Node Type"] == "Index Scan", plan
    assert plan["Index Name"] == "ix_so_status_btype", plan
    assert "business_type" in plan["Index Cond"], plan


def test_migration_roundtrip_restores_legacy_fk(db):
    """downgrade 回 a5e8c3f7b2d9：默认 FK 名、无 ondelete、无新索引；再升回 head 恢复。"""
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            fks = _snapshot_fks(insp)
            assert set(fks) == {_LEGACY_FK}
            assert not fks[_LEGACY_FK]["options"].get("ondelete")
            snap_idx = {i["name"] for i in insp.get_indexes(_SNAP)}
            assert "ix_sales_recon_snapshot_sales_order" not in snap_idx
            so_idx = {i["name"] for i in insp.get_indexes("f_sales_order")}
            assert "ix_so_status_btype" not in so_idx
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as conn:
            insp = inspect(conn)
            fks = _snapshot_fks(insp)
            assert set(fks) == {_FK}
            assert fks[_FK]["options"].get("ondelete") == "SET NULL"
    finally:
        alembic_command.upgrade(cfg, "head")


def test_order_delete_nulls_snapshot_link_but_keeps_observation(db):
    """ON DELETE SET NULL 行为：订单删除不级联删快照、不阻塞，链接置空、观测保留。"""
    batch_id = _seed_batch(db)
    order_id = db.execute(text(
        "INSERT INTO f_sales_order (raw_order_id, order_no, order_date, salesperson,"
        " business_type, data_status, import_batch_id)"
        " VALUES ('R-DEL', 'XS-DEL', DATE '2026-01-01', '张三', '备件销售',"
        " '已生效', :b) RETURNING id"), {"b": batch_id}).scalar_one()
    modified = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    db.add(FSalesReconciliationSnapshot(
        raw_order_id="R-DEL", order_no="XS-DEL", sales_order_id=order_id,
        import_batch_id=batch_id, source="import", source_modified_at=modified,
        reconciled_amount=Decimal("100"),
        content_hash=snapshot_content_hash(
            raw_order_id="R-DEL", source_modified_at=modified,
            order_amount=None, reconciled_amount=Decimal("100"),
            pending_amount=None, return_reconciled_amount=None),
    ))
    db.commit()
    db.execute(text("DELETE FROM f_sales_order WHERE id = :i"), {"i": order_id})
    db.commit()
    row = db.execute(text(
        "SELECT sales_order_id, reconciled_amount FROM f_sales_reconciliation_snapshot"
        " WHERE raw_order_id = 'R-DEL'")).one()
    assert row.sales_order_id is None            # 链接置空
    assert row.reconciled_amount == Decimal("100")   # 观测保留，未被级联删
