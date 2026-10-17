"""f_sales_reconciliation_snapshot 建表/约束/幂等 与 f_sales_order 含税三列契约（D-40/D-41 PR-1）。

覆盖：schema 与约束、(raw_order_id, content_hash) 幂等去重、NULL 与 0 的区分、
content_hash 规范化规则、取每单最新快照的排序语义、迁移 upgrade→downgrade→upgrade 往返、
ORM 与迁移零漂移。
"""

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import engine
from app.models.sales import FSalesReconciliationSnapshot
from app.services.sales_reconciliation import (
    latest_snapshots_by_raw_order_id,
    snapshot_content_hash,
)

_PREV = "7f73424a7f65"
_TABLE = "f_sales_reconciliation_snapshot"
_CST = "+08:00"
_TZ_CN = timezone(timedelta(hours=8))


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _seed_batch(db, *, name: str) -> int:
    return db.execute(
        text(
            "INSERT INTO sys_import_batch (filename, file_type, file_hash, status) "
            "VALUES (:filename, 'sales', :file_hash, 'success') RETURNING id"
        ),
        {"filename": f"{name}.xlsx", "file_hash": f"sales-recon-{name}"},
    ).scalar_one()


def _add_snapshot(
    db,
    batch_id: int,
    raw_order_id: str,
    *,
    source_modified_at: datetime | None = None,
    order_amount: Decimal | None = None,
    reconciled_amount: Decimal | None = None,
    pending_amount: Decimal | None = None,
    return_reconciled_amount: Decimal | None = None,
    source: str = "import",
    order_no: str | None = None,
    content_hash: str | None = None,
) -> FSalesReconciliationSnapshot:
    """按 PR-2 将来的写入路径组快照：哈希一律由 snapshot_content_hash 生成。"""
    snap = FSalesReconciliationSnapshot(
        raw_order_id=raw_order_id,
        order_no=order_no,
        sales_order_id=None,
        import_batch_id=batch_id,
        source=source,
        source_modified_at=source_modified_at,
        order_amount=order_amount,
        reconciled_amount=reconciled_amount,
        pending_amount=pending_amount,
        return_reconciled_amount=return_reconciled_amount,
        content_hash=content_hash
        or snapshot_content_hash(
            raw_order_id=raw_order_id,
            source_modified_at=source_modified_at,
            order_amount=order_amount,
            reconciled_amount=reconciled_amount,
            pending_amount=pending_amount,
            return_reconciled_amount=return_reconciled_amount,
        ),
    )
    db.add(snap)
    db.flush()
    return snap


# ---- schema 契约 ----


def test_snapshot_table_columns_and_nullability(db):
    columns = {c["name"]: c for c in inspect(db.get_bind()).get_columns(_TABLE)}
    assert set(columns) == {
        "id", "raw_order_id", "order_no", "sales_order_id", "import_batch_id",
        "source", "source_modified_at", "order_amount", "reconciled_amount",
        "pending_amount", "return_reconciled_amount", "content_hash", "created_at",
    }
    # 空单元格=NULL 的列必须可空；身份/去重/溯源列必须非空
    for name in ("order_no", "sales_order_id", "source_modified_at", "order_amount",
                 "reconciled_amount", "pending_amount", "return_reconciled_amount"):
        assert columns[name]["nullable"] is True, name
    for name in ("raw_order_id", "import_batch_id", "source", "content_hash",
                 "created_at"):
        assert columns[name]["nullable"] is False, name


def test_snapshot_table_constraints_and_indexes(db):
    bind = db.get_bind()
    inspector = inspect(bind)
    checks = {
        c["name"]: c for c in inspector.get_check_constraints(_TABLE)
    }
    assert "ck_sales_recon_snapshot_source" in checks
    assert "source" in checks["ck_sales_recon_snapshot_source"]["sqltext"]
    assert {
        c["name"] for c in inspector.get_unique_constraints(_TABLE)
    } == {"uq_sales_recon_snapshot_content"}
    indexes = {i["name"]: i for i in inspector.get_indexes(_TABLE)}
    assert indexes["ix_sales_recon_snapshot_batch"]["column_names"] == [
        "import_batch_id"
    ]
    # DESC + NULLS LAST 排序键必须真的落进索引定义：PG 裸 DESC 默认 NULLS FIRST，
    # 与查询排序键不匹配会导致索引永远无法消排序（复审已实测，别退回裸 DESC）
    latest_def = db.execute(
        text("SELECT pg_get_indexdef('ix_sales_recon_snapshot_latest'::regclass)")
    ).scalar_one()
    assert re.search(r"source_modified_at DESC NULLS LAST", latest_def)
    assert re.search(r"id DESC", latest_def)
    assert latest_def.startswith("CREATE INDEX ix_sales_recon_snapshot_latest")
    fks = {
        (fk["referred_table"], fk["constrained_columns"][0])
        for fk in inspector.get_foreign_keys(_TABLE)
    }
    assert ("f_sales_order", "sales_order_id") in fks
    assert ("sys_import_batch", "import_batch_id") in fks


def test_sales_order_amount_columns_exist_and_nullable(db):
    columns = {
        c["name"]: c for c in inspect(db.get_bind()).get_columns("f_sales_order")
    }
    for name in ("amount_inc_tax", "is_tax_inclusive", "tax_amount"):
        assert name in columns, name
        assert columns[name]["nullable"] is True, name


# ---- 幂等与约束行为 ----


def test_on_conflict_do_nothing_swallows_duplicate_content(db):
    """重复上传/回填重跑：同 (raw_order_id, content_hash) 被 ON CONFLICT 吞掉。"""
    batch_a = _seed_batch(db, name="dup-a")
    batch_b = _seed_batch(db, name="dup-b")
    snap = _add_snapshot(
        db, batch_a, "DUP-1",
        source_modified_at=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        order_amount=Decimal("100.00"), reconciled_amount=Decimal("60.00"),
    )
    db.commit()
    # PR-2 的写入形态：不同批次、同一内容，INSERT ... ON CONFLICT DO NOTHING
    result = db.execute(
        text(
            "INSERT INTO f_sales_reconciliation_snapshot "
            "(raw_order_id, order_no, sales_order_id, import_batch_id, source, "
            " source_modified_at, order_amount, reconciled_amount, pending_amount, "
            " return_reconciled_amount, content_hash) "
            "VALUES ('DUP-1', NULL, NULL, :batch, 'backfill', "
            " :modified, :order_amount, :reconciled, NULL, NULL, :ch) "
            "ON CONFLICT (raw_order_id, content_hash) DO NOTHING"
        ),
        {
            "batch": batch_b,
            "modified": datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
            "order_amount": Decimal("100.00"),
            "reconciled": Decimal("60.00"),
            "ch": snap.content_hash,
        },
    )
    db.commit()
    assert result.rowcount == 0
    assert db.execute(
        text(f"SELECT count(*) FROM {_TABLE} WHERE raw_order_id = 'DUP-1'")
    ).scalar_one() == 1


def test_duplicate_without_on_conflict_rejected(db):
    batch_id = _seed_batch(db, name="dup-raw")
    snap = _add_snapshot(db, batch_id, "DUP-2", pending_amount=Decimal("1.00"))
    db.flush()
    with pytest.raises(IntegrityError, match="uq_sales_recon_snapshot_content"):
        db.execute(
            text(
                "INSERT INTO f_sales_reconciliation_snapshot "
                "(raw_order_id, import_batch_id, source, content_hash) "
                "VALUES ('DUP-2', :batch, 'import', :ch)"
            ),
            {"batch": batch_id, "ch": snap.content_hash},
        )
    db.rollback()


def test_distinct_content_appends_new_row(db):
    """快照只追加、不覆盖：对账金额变化（内容哈希不同）→ 新增一行。"""
    batch_id = _seed_batch(db, name="append")
    _add_snapshot(
        db, batch_id, "ORD-APPEND",
        source_modified_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        reconciled_amount=Decimal("60.00"),
    )
    _add_snapshot(
        db, batch_id, "ORD-APPEND",
        source_modified_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        reconciled_amount=Decimal("80.00"),
    )
    db.commit()
    rows = db.execute(
        text(
            "SELECT reconciled_amount FROM f_sales_reconciliation_snapshot "
            "WHERE raw_order_id = 'ORD-APPEND' ORDER BY id"
        )
    ).scalars().all()
    assert rows == [Decimal("60.00"), Decimal("80.00")]


def test_null_amount_stored_as_null_and_hashed_distinctly_from_zero(db):
    batch_id = _seed_batch(db, name="null-vs-zero")
    _add_snapshot(db, batch_id, "ORD-NZ", pending_amount=None,
                  source_modified_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    _add_snapshot(db, batch_id, "ORD-NZ", pending_amount=Decimal("0.00"),
                  source_modified_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    db.commit()
    stored = db.execute(
        text(
            "SELECT pending_amount FROM f_sales_reconciliation_snapshot "
            "WHERE raw_order_id = 'ORD-NZ' ORDER BY id"
        )
    ).scalars().all()
    assert stored == [None, Decimal("0.00")]
    # 哈希层同样可区分：空单元格 ≠ 0（两行都已插入成功即隐含哈希不同）
    assert snapshot_content_hash(
        raw_order_id="X", source_modified_at=None, order_amount=None,
        reconciled_amount=None, pending_amount=None,
        return_reconciled_amount=None,
    ) != snapshot_content_hash(
        raw_order_id="X", source_modified_at=None, order_amount=None,
        reconciled_amount=None, pending_amount=Decimal("0.00"),
        return_reconciled_amount=None,
    )


def test_source_check_rejects_unknown_value(db):
    batch_id = _seed_batch(db, name="check-source")
    with pytest.raises(IntegrityError, match="ck_sales_recon_snapshot_source"):
        _add_snapshot(db, batch_id, "ORD-SRC", source="manual")
    db.rollback()


def test_sales_order_fk_optional_and_import_batch_fk_enforced(db):
    batch_id = _seed_batch(db, name="fk")
    # sales_order_id 可空：快照先于订单入库（D-40 订单可能尚未入库）
    snap = _add_snapshot(db, batch_id, "ORD-FK")
    assert snap.sales_order_id is None
    db.commit()
    with pytest.raises(IntegrityError, match="foreign key constraint"):
        _add_snapshot(db, 10**9, raw_order_id="ORD-FK-BAD")
    db.rollback()


# ---- content_hash 规范化 ----


def _hash(**overrides):
    params = dict(
        raw_order_id="H-1",
        source_modified_at=None,
        order_amount=None,
        reconciled_amount=None,
        pending_amount=None,
        return_reconciled_amount=None,
    )
    params.update(overrides)
    return snapshot_content_hash(**params)


def test_hash_shape_and_determinism():
    h1, h2 = _hash(order_amount=Decimal("12.34")), _hash(order_amount=Decimal("12.34"))
    assert h1 == h2
    assert re.fullmatch(r"[0-9a-f]{64}", h1)


def test_hash_decimal_scale_insensitive():
    """1 / 1.0 / Decimal("1") / Decimal("1.00") 同一内容同一哈希。"""
    assert _hash(order_amount=1) == _hash(order_amount=1.0)
    assert _hash(order_amount=1) == _hash(order_amount=Decimal("1"))
    assert _hash(order_amount=1) == _hash(order_amount=Decimal("1.00"))


def test_hash_quantizes_half_up_to_money_precision():
    """量化口径与 PG numeric(14,2) 一致：half away from zero。"""
    assert _hash(order_amount=Decimal("1.005")) == _hash(order_amount=Decimal("1.01"))
    assert _hash(order_amount=Decimal("-1.005")) == _hash(order_amount=Decimal("-1.01"))


def test_hash_normalizes_negative_zero_like_pg_storage():
    """PG numeric 把 -0.00 存成 0.00；哈希必须同样归一，否则符号零绕过去重。"""
    assert _hash(order_amount=Decimal("-0.00")) == _hash(order_amount=Decimal("0.00"))
    assert _hash(order_amount=Decimal("-0")) == _hash(order_amount=0)
    for field in ("reconciled_amount", "pending_amount", "return_reconciled_amount"):
        assert _hash(**{field: Decimal("-0.00")}) == _hash(**{field: 0}), field


def test_hash_null_distinct_from_zero_for_every_amount():
    for field in ("order_amount", "reconciled_amount", "pending_amount",
                  "return_reconciled_amount"):
        assert _hash(**{field: None}) != _hash(**{field: 0}), field
        assert _hash(**{field: None}) != _hash(**{field: Decimal("0.00")}), field


def test_hash_normalizes_timezone_to_utc():
    """同一时刻的不同时区表示（含 naive 视作 UTC）哈希相同。"""
    assert _hash(source_modified_at=datetime(2026, 6, 30, 20, 0, tzinfo=_TZ_CN)) == \
        _hash(source_modified_at=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc))
    assert _hash(source_modified_at=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)) == \
        _hash(source_modified_at=datetime(2026, 6, 30, 12, 0))


def test_hash_none_time_differs_from_any_time():
    none_time = _hash(source_modified_at=None)
    assert none_time != _hash(source_modified_at=datetime(1970, 1, 1, tzinfo=timezone.utc))
    assert none_time != _hash(source_modified_at=datetime(2026, 6, 30, 12, 0))


def test_hash_covers_every_component():
    base = _hash(
        raw_order_id="H-1",
        source_modified_at=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        order_amount=Decimal("1.00"),
        reconciled_amount=Decimal("2.00"),
        pending_amount=Decimal("3.00"),
        return_reconciled_amount=Decimal("4.00"),
    )
    variants = [
        _hash(raw_order_id="H-2"),
        _hash(source_modified_at=datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc)),
        _hash(order_amount=Decimal("1.01")),
        _hash(reconciled_amount=Decimal("2.01")),
        _hash(pending_amount=Decimal("3.01")),
        _hash(return_reconciled_amount=Decimal("4.01")),
    ]
    assert all(v != base for v in variants)
    assert len(set(variants)) == len(variants)


def test_hash_roundtrip_matches_stored_value(db):
    """哈希输入与落库精度一致：DB 读回的 Decimal 重新计算哈希不变。

    特意覆盖跨层归一边界：半分钱（Python ROUND_HALF_UP 量化 vs PG numeric(14,2)
    强制转换）与负零（PG 存储归一成 0.00）——两侧规则任何一边被改动都会在此爆。
    """
    batch_id = _seed_batch(db, name="roundtrip")
    snap = _add_snapshot(
        db, batch_id, "ORD-RT",
        source_modified_at=datetime(2026, 6, 30, 20, 0, tzinfo=_TZ_CN),
        order_amount=Decimal("255011389.07"),
        reconciled_amount=Decimal("10000000.5"),   # 入库即量化为 10000000.50
        pending_amount=Decimal("0"),
        return_reconciled_amount=None,
    )
    snap_edge = _add_snapshot(
        db, batch_id, "ORD-RT-EDGE",
        source_modified_at=datetime(2026, 6, 30, 20, 0, tzinfo=_TZ_CN),
        order_amount=Decimal("-0.00"),             # PG 归一存 0.00
        reconciled_amount=Decimal("1.005"),        # half away from zero → 1.01
        pending_amount=Decimal("-1.005"),          # → -1.01
        return_reconciled_amount=None,
    )
    db.expire_all()
    stored = db.get(FSalesReconciliationSnapshot, snap.id)
    assert stored.order_amount == Decimal("255011389.07")
    assert stored.reconciled_amount == Decimal("10000000.50")
    assert stored.pending_amount == Decimal("0.00")
    assert stored.return_reconciled_amount is None
    assert snapshot_content_hash(
        raw_order_id=stored.raw_order_id,
        source_modified_at=stored.source_modified_at,
        order_amount=stored.order_amount,
        reconciled_amount=stored.reconciled_amount,
        pending_amount=stored.pending_amount,
        return_reconciled_amount=stored.return_reconciled_amount,
    ) == stored.content_hash
    # 写入时用的 10000000.5 与落库 10000000.50 是同一哈希（量化规则生效）
    assert snap.content_hash == snapshot_content_hash(
        raw_order_id="ORD-RT",
        source_modified_at=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        order_amount=Decimal("255011389.07"),
        reconciled_amount=Decimal("10000000.5"),
        pending_amount=Decimal("0"),
        return_reconciled_amount=None,
    )

    # 半分钱/负零：落库值按 PG 归一，且原始输入与读回值算出的哈希一致
    edge = db.get(FSalesReconciliationSnapshot, snap_edge.id)
    assert edge.order_amount == Decimal("0.00")
    assert edge.reconciled_amount == Decimal("1.01")
    assert edge.pending_amount == Decimal("-1.01")
    hash_from_readback = snapshot_content_hash(
        raw_order_id=edge.raw_order_id,
        source_modified_at=edge.source_modified_at,
        order_amount=edge.order_amount,
        reconciled_amount=edge.reconciled_amount,
        pending_amount=edge.pending_amount,
        return_reconciled_amount=edge.return_reconciled_amount,
    )
    assert hash_from_readback == edge.content_hash == snap_edge.content_hash


# ---- 取每单最新 ----


def test_latest_prefers_newest_modified_time_despite_insert_order(db):
    batch_id = _seed_batch(db, name="latest")
    t1 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 5, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 9, tzinfo=timezone.utc)
    _add_snapshot(db, batch_id, "ORD-L1", source_modified_at=t2,
                  reconciled_amount=Decimal("20.00"))
    _add_snapshot(db, batch_id, "ORD-L1", source_modified_at=t3,
                  reconciled_amount=Decimal("30.00"))
    _add_snapshot(db, batch_id, "ORD-L1", source_modified_at=t1,
                  reconciled_amount=Decimal("10.00"))
    db.commit()
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-L1"])
    assert latest["ORD-L1"].reconciled_amount == Decimal("30.00")


def test_latest_tie_breaks_by_larger_id(db):
    batch_id = _seed_batch(db, name="latest-tie")
    tie = datetime(2026, 7, 3, tzinfo=timezone.utc)
    first = _add_snapshot(db, batch_id, "ORD-L2", source_modified_at=tie,
                          reconciled_amount=Decimal("1.00"))
    second = _add_snapshot(db, batch_id, "ORD-L2", source_modified_at=tie,
                           reconciled_amount=Decimal("2.00"))
    db.commit()
    assert second.id > first.id
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-L2"])
    assert latest["ORD-L2"].id == second.id


def test_latest_null_modified_time_loses_to_any_time_then_falls_back_to_id(db):
    batch_id = _seed_batch(db, name="latest-null")
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    _add_snapshot(db, batch_id, "ORD-L3", source_modified_at=None,
                  reconciled_amount=Decimal("1.00"))        # 旧格式：无修改时间
    _add_snapshot(db, batch_id, "ORD-L3", source_modified_at=old,
                  reconciled_amount=Decimal("2.00"))
    db.commit()
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-L3"])
    assert latest["ORD-L3"].reconciled_amount == Decimal("2.00")

    # 全部无修改时间：退化为 id DESC（后写入者胜）
    _add_snapshot(db, batch_id, "ORD-L4", source_modified_at=None,
                  reconciled_amount=Decimal("7.00"))
    newest = _add_snapshot(db, batch_id, "ORD-L4", source_modified_at=None,
                           reconciled_amount=Decimal("8.00"))
    db.commit()
    latest4 = latest_snapshots_by_raw_order_id(db, ["ORD-L4"])
    assert latest4["ORD-L4"].id == newest.id


def test_latest_ignores_unknown_orders_and_empty_input(db):
    batch_id = _seed_batch(db, name="latest-miss")
    _add_snapshot(db, batch_id, "ORD-L5", reconciled_amount=Decimal("5.00"))
    db.commit()
    assert latest_snapshots_by_raw_order_id(db, []) == {}
    assert latest_snapshots_by_raw_order_id(db, ["NO-SUCH-ORDER"]) == {}


# ---- 迁移往返与零漂移 ----


def test_migration_downgrade_upgrade_roundtrip(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert _TABLE not in inspector.get_table_names()
            sales_columns = {
                c["name"] for c in inspector.get_columns("f_sales_order")
            }
            assert not {"amount_inc_tax", "is_tax_inclusive", "tax_amount"} & sales_columns
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert _TABLE in inspector.get_table_names()
            sales_columns = {
                c["name"] for c in inspector.get_columns("f_sales_order")
            }
            assert {"amount_inc_tax", "is_tax_inclusive", "tax_amount"} <= sales_columns
    finally:
        alembic_command.upgrade(cfg, "head")


def test_alembic_check_no_drift(migrated):
    """ORM 元数据与迁移链零漂移（CI 另有独立步骤，这里本地双保险）。"""
    alembic_command.check(_cfg())
