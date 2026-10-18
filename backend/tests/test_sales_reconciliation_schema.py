"""f_sales_reconciliation_snapshot 建表/约束/幂等 与 f_sales_order 含税三列契约（D-40/D-41 PR-1）。

覆盖：schema 与约束、(raw_order_id, import_batch_id) 每批次每单一条观测的幂等
（同批次重放不新增、不同批次各一条——A→B→A 不被内容去重吞掉）、NULL 与 0 的区分、
content_hash 规范化规则、取每单最新快照的排序语义（修改时间 → 批次号 → 写入顺序）、
迁移 upgrade→downgrade→upgrade 往返、ORM 与迁移零漂移。
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
from app.etl.cleaner import parse_money
from app.models.sales import FSalesReconciliationSnapshot
from app.services.sales_reconciliation import (
    _canonical_amount,
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
    uniques = {c["name"]: c for c in inspector.get_unique_constraints(_TABLE)}
    assert set(uniques) == {"uq_sales_recon_snapshot_order_batch"}
    assert uniques["uq_sales_recon_snapshot_order_batch"]["column_names"] == [
        "raw_order_id", "import_batch_id"
    ]
    indexes = {i["name"]: i for i in inspector.get_indexes(_TABLE)}
    assert indexes["ix_sales_recon_snapshot_batch"]["column_names"] == [
        "import_batch_id"
    ]
    # DESC + NULLS LAST 排序键必须真的落进索引定义：PG 裸 DESC 默认 NULLS FIRST，
    # 与查询排序键不匹配会导致索引永远无法消排序（复审已实测，别退回裸 DESC）。
    # 键序必须与 latest_snapshots_by_raw_order_id 的 ORDER BY 完全一致
    latest_def = db.execute(
        text("SELECT pg_get_indexdef('ix_sales_recon_snapshot_latest'::regclass)")
    ).scalar_one()
    assert latest_def.startswith("CREATE INDEX ix_sales_recon_snapshot_latest")
    index_keys = latest_def[latest_def.rindex("(") + 1:latest_def.rindex(")")].split(", ")
    assert index_keys == [
        "raw_order_id", "source_modified_at DESC NULLS LAST",
        "import_batch_id DESC", "id DESC",
    ]
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


def test_on_conflict_do_nothing_same_batch_replay_is_idempotent(db):
    """同批次重放/回填重跑：同 (raw_order_id, import_batch_id) 被 ON CONFLICT 吞掉。"""
    batch_id = _seed_batch(db, name="replay")
    snap = _add_snapshot(
        db, batch_id, "DUP-1",
        source_modified_at=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        order_amount=Decimal("100.00"), reconciled_amount=Decimal("60.00"),
    )
    db.commit()
    # PR-2 的写入形态：同批次重放，INSERT ... ON CONFLICT DO NOTHING
    result = db.execute(
        text(
            "INSERT INTO f_sales_reconciliation_snapshot "
            "(raw_order_id, order_no, sales_order_id, import_batch_id, source, "
            " source_modified_at, order_amount, reconciled_amount, pending_amount, "
            " return_reconciled_amount, content_hash) "
            "VALUES ('DUP-1', NULL, NULL, :batch, 'backfill', "
            " :modified, :order_amount, :reconciled, NULL, NULL, :ch) "
            "ON CONFLICT (raw_order_id, import_batch_id) DO NOTHING"
        ),
        {
            "batch": batch_id,
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
    _add_snapshot(db, batch_id, "DUP-2", pending_amount=Decimal("1.00"))
    db.flush()
    with pytest.raises(IntegrityError, match="uq_sales_recon_snapshot_order_batch"):
        db.execute(
            text(
                "INSERT INTO f_sales_reconciliation_snapshot "
                "(raw_order_id, import_batch_id, source, content_hash) "
                "VALUES ('DUP-2', :batch, 'import', :ch)"
            ),
            # content_hash 故意不同：证明被拒是因为 (raw_order_id, import_batch_id)
            {"batch": batch_id, "ch": "0" * 64},
        )
    db.rollback()


def test_distinct_content_appends_new_row(db):
    """快照只追加、不覆盖：不同批次对账金额变化（内容哈希不同）→ 各记一条。"""
    batch1 = _seed_batch(db, name="append-1")
    batch2 = _seed_batch(db, name="append-2")
    _add_snapshot(
        db, batch1, "ORD-APPEND",
        source_modified_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        reconciled_amount=Decimal("60.00"),
    )
    _add_snapshot(
        db, batch2, "ORD-APPEND",
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


def test_second_observation_for_same_order_and_batch_rejected(db):
    """批次粒度：每批次每单至多一条观测——同批次第二次写入（即使内容不同）被拒。

    这是「同一批次同一订单在文件里出现多次仍只写一条」的数据库层保证：
    loader 侧先按订单聚合（取首个非空），本约束兜底。
    """
    batch_id = _seed_batch(db, name="append-same-batch")
    _add_snapshot(db, batch_id, "ORD-APPEND", reconciled_amount=Decimal("60.00"))
    with pytest.raises(IntegrityError, match="uq_sales_recon_snapshot_order_batch"):
        _add_snapshot(db, batch_id, "ORD-APPEND", reconciled_amount=Decimal("80.00"))
    db.rollback()


def test_null_amount_stored_as_null_and_hashed_distinctly_from_zero(db):
    batch1 = _seed_batch(db, name="null-vs-zero-1")
    batch2 = _seed_batch(db, name="null-vs-zero-2")
    _add_snapshot(db, batch1, "ORD-NZ", pending_amount=None,
                  source_modified_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    _add_snapshot(db, batch2, "ORD-NZ", pending_amount=Decimal("0.00"),
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


def test_canonical_amount_idempotent_after_parse_money():
    """两级量化幂等（#395 质询的证明，2026-10-18）。

    真实链路金额先经 cleaner.parse_money（HALF_EVEN，decimal 默认上下文）
    量化到 0.01 落库；_canonical_amount 的 ROUND_HALF_UP 再量化对两位小数
    输入是空操作——哈希输入与落库值同形，content_hash 不因两级舍入口径
    不同而漂移。半分边界上两种取向确实分歧（1.005 → HALF_EVEN 1.00 /
    HALF_UP 1.01），但链路上不可达：进入 _canonical_amount 的值永远是
    parse_money 的产物（符号零归一由
    test_hash_normalizes_negative_zero_like_pg_storage 单独看守）。
    """
    # 半分边界：裸值上两级取向分歧，parse_money 产物上收敛
    assert parse_money("1.005") == Decimal("1.00")            # HALF_EVEN → 偶
    assert _canonical_amount(Decimal("1.005")) == "1.01"      # HALF_UP（旁路直呼才可达）
    assert _canonical_amount(parse_money("1.005")) == "1.00"  # 真实链路：幂等空操作

    for raw in ["1.005", "2.005", "1.015", "2.015", "0.005", "-1.005",
                "1130", "99.999", "13.5", "0.001"]:
        pm = parse_money(raw)
        assert pm.as_tuple().exponent == -2                   # 已是两位小数
        assert _canonical_amount(pm) == str(pm)               # 再量化不改形（哈希↔落库同形）


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


def test_aba_with_null_times_keeps_all_three_latest_is_a(db):
    """缺陷回归（Codex 审查）：修改时间全空时 A→B→A 不被内容去重吞掉。

    旧口径 (raw_order_id, content_hash) 唯一会把第三个批次与第一个批次内容
    相同的 A 当重复丢弃（ON CONFLICT DO NOTHING），"最新"错误地停在 B；
    新口径每批次各记一条，最新 = 批次最大的 A。
    """
    batch_a1 = _seed_batch(db, name="aba-1")
    batch_b = _seed_batch(db, name="aba-2")
    batch_a2 = _seed_batch(db, name="aba-3")
    _add_snapshot(db, batch_a1, "ORD-ABA", reconciled_amount=Decimal("100.00"))
    _add_snapshot(db, batch_b, "ORD-ABA", reconciled_amount=Decimal("60.00"))
    _add_snapshot(db, batch_a2, "ORD-ABA", reconciled_amount=Decimal("100.00"))
    db.commit()
    assert db.execute(
        text(f"SELECT count(*) FROM {_TABLE} WHERE raw_order_id = 'ORD-ABA'")
    ).scalar_one() == 3
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-ABA"])
    assert latest["ORD-ABA"].reconciled_amount == Decimal("100.00")
    assert latest["ORD-ABA"].import_batch_id == batch_a2


def test_same_content_different_batches_each_kept_latest_prefers_larger_batch(db):
    """内容相同的两个不同批次各一行（不去重），"最新"取批次较大者。"""
    batch1 = _seed_batch(db, name="same-content-1")
    batch2 = _seed_batch(db, name="same-content-2")
    first = _add_snapshot(db, batch1, "ORD-SAME", reconciled_amount=Decimal("7.00"))
    second = _add_snapshot(db, batch2, "ORD-SAME", reconciled_amount=Decimal("7.00"))
    db.commit()
    assert first.content_hash == second.content_hash          # 内容确实相同
    assert db.execute(
        text(f"SELECT count(*) FROM {_TABLE} WHERE raw_order_id = 'ORD-SAME'")
    ).scalar_one() == 2
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-SAME"])
    assert latest["ORD-SAME"].import_batch_id == batch2


def test_latest_prefers_newest_modified_time_despite_insert_order(db):
    batch1 = _seed_batch(db, name="latest-1")
    batch2 = _seed_batch(db, name="latest-2")
    batch3 = _seed_batch(db, name="latest-3")
    t1 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 5, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 9, tzinfo=timezone.utc)
    _add_snapshot(db, batch1, "ORD-L1", source_modified_at=t2,
                  reconciled_amount=Decimal("20.00"))
    _add_snapshot(db, batch2, "ORD-L1", source_modified_at=t3,
                  reconciled_amount=Decimal("30.00"))
    _add_snapshot(db, batch3, "ORD-L1", source_modified_at=t1,
                  reconciled_amount=Decimal("10.00"))
    db.commit()
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-L1"])
    assert latest["ORD-L1"].reconciled_amount == Decimal("30.00")


def test_latest_modified_time_beats_larger_batch(db):
    """修改时间优先于批次号：批次更大但时间更旧 → 不算最新。"""
    batch1 = _seed_batch(db, name="time-first-1")
    batch2 = _seed_batch(db, name="time-first-2")
    _add_snapshot(db, batch2, "ORD-TF",
                  source_modified_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                  reconciled_amount=Decimal("1.00"))
    _add_snapshot(db, batch1, "ORD-TF",
                  source_modified_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                  reconciled_amount=Decimal("2.00"))
    db.commit()
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-TF"])
    assert latest["ORD-TF"].import_batch_id == batch1
    assert latest["ORD-TF"].reconciled_amount == Decimal("2.00")


def test_latest_same_time_tie_breaks_by_larger_batch_despite_larger_row_id(db):
    """乱序插入：批次较小的行 id 反而更大时，同时间并列的最新仍取批次较大者。"""
    batch_small = _seed_batch(db, name="tie-small")
    batch_large = _seed_batch(db, name="tie-large")
    assert batch_small < batch_large
    tie = datetime(2026, 7, 3, tzinfo=timezone.utc)
    large_batch_row = _add_snapshot(db, batch_large, "ORD-L2", source_modified_at=tie,
                                    reconciled_amount=Decimal("2.00"))
    small_batch_row = _add_snapshot(db, batch_small, "ORD-L2", source_modified_at=tie,
                                    reconciled_amount=Decimal("1.00"))
    db.commit()
    assert small_batch_row.id > large_batch_row.id       # 模拟并发/回填乱序写入
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-L2"])
    assert latest["ORD-L2"].import_batch_id == batch_large


def test_latest_null_modified_time_loses_to_any_time_then_falls_back_to_batch(db):
    batch1 = _seed_batch(db, name="latest-null-1")
    batch2 = _seed_batch(db, name="latest-null-2")
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    _add_snapshot(db, batch2, "ORD-L3", source_modified_at=None,
                  reconciled_amount=Decimal("1.00"))        # 旧格式：无修改时间
    _add_snapshot(db, batch1, "ORD-L3", source_modified_at=old,
                  reconciled_amount=Decimal("2.00"))
    db.commit()
    latest = latest_snapshots_by_raw_order_id(db, ["ORD-L3"])
    assert latest["ORD-L3"].reconciled_amount == Decimal("2.00")

    # 全部无修改时间：批次较大者胜（行 id 乱序也不影响）
    null_large_batch = _add_snapshot(db, batch2, "ORD-L4", source_modified_at=None,
                                     reconciled_amount=Decimal("7.00"))
    null_small_batch = _add_snapshot(db, batch1, "ORD-L4", source_modified_at=None,
                                     reconciled_amount=Decimal("8.00"))
    db.commit()
    assert null_small_batch.id > null_large_batch.id        # 乱序：批次小的行 id 大
    latest4 = latest_snapshots_by_raw_order_id(db, ["ORD-L4"])
    assert latest4["ORD-L4"].import_batch_id == batch2


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
