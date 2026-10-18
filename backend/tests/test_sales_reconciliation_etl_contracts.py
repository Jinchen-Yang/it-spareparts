"""快照行构造共用与税列批量回填的契约测试（#396 S5/S6，2026-10-18）。

- S5：loader 导入与 scripts/backfill 历史回填的快照行构造收敛到
  ``sales_reconciliation.snapshot_rows_from_orders`` 单一事实源——这里证明
  共用构造的行内容与 content_hash 独立重算一致，且 loader 写库行与共用构造
  逐字段相同（skip/upsert 语义由既有 test_sales_reconciliation_import 看守）。
- S6：回填脚本税三件套改批量 UPDATE ... FROM (VALUES ...) 后，与逐条 UPDATE
  同一守卫语义——只补本批次订单的 NULL 列、已有值不覆盖、他批次同号单不误写、
  未登记列名直接拒绝。
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.etl.loader import _write_sales_reconciliation_snapshots
from app.etl.transform import TransformResult
from app.models.sales import FSalesReconciliationSnapshot
from app.services.sales_reconciliation import (
    SOURCE_BACKFILL,
    SOURCE_IMPORT,
    snapshot_content_hash,
    snapshot_row_from_head,
    snapshot_rows_from_orders,
)

MODIFIED = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


def _head(**over):
    head = {
        "order_no": "XS-C-1",
        "amount_inc_tax": Decimal("1130.00"),
        "reconciled_amount": Decimal("100.00"),
        "pending_amount": None,
        "return_reconciled_amount": Decimal("10.00"),
        "source_modified_at": MODIFIED,
    }
    head.update(over)
    return head


# ---------------------------------------------------------------- S5 行构造

def test_snapshot_row_builder_content_matches_direct_hash_recompute():
    """共用构造的行 = 字面期望字段 + 独立重算的 content_hash（不复用构造器算哈希）。"""
    head = _head()
    row = snapshot_row_from_head(
        raw_order_id="R-C-1", head=head, batch_id=7,
        source=SOURCE_BACKFILL, sales_order_id=42)
    assert row == {
        "raw_order_id": "R-C-1",
        "order_no": "XS-C-1",
        "sales_order_id": 42,
        "import_batch_id": 7,
        "source": "backfill",
        "source_modified_at": MODIFIED,
        "order_amount": Decimal("1130.00"),
        "reconciled_amount": Decimal("100.00"),
        "pending_amount": None,
        "return_reconciled_amount": Decimal("10.00"),
        "content_hash": snapshot_content_hash(
            raw_order_id="R-C-1", source_modified_at=MODIFIED,
            order_amount=Decimal("1130.00"), reconciled_amount=Decimal("100.00"),
            pending_amount=None, return_reconciled_amount=Decimal("10.00")),
    }


def test_snapshot_rows_batch_form_matches_single():
    heads = {"R-A": _head(), "R-B": _head(order_no="XS-C-2", reconciled_amount=None)}
    oid_map = {"R-A": 1, "R-B": None}   # R-B：订单缺席（回填路径允许）
    batch = snapshot_rows_from_orders(
        heads, batch_id=9, source=SOURCE_IMPORT, oid_map=oid_map)
    assert batch == [
        snapshot_row_from_head(raw_order_id="R-A", head=heads["R-A"],
                               batch_id=9, source=SOURCE_IMPORT, sales_order_id=1),
        snapshot_row_from_head(raw_order_id="R-B", head=heads["R-B"],
                               batch_id=9, source=SOURCE_IMPORT, sales_order_id=None),
    ]


def test_loader_written_rows_equal_shared_builder(db):
    """loader 写库的快照行与共用构造逐字段一致（S5 收敛后 loader 不再自构行）。"""
    batch_id = db.execute(text(
        "INSERT INTO sys_import_batch (filename, file_type, file_hash, status) "
        "VALUES ('c5.xlsx', 'sales', 'c5-h1', 'success') RETURNING id")).scalar_one()
    result = TransformResult(file_type="sales", orders={"R-L": _head()})
    inserted, skipped = _write_sales_reconciliation_snapshots(
        db, result, batch_id, oid_map={})
    assert (inserted, skipped) == (1, 0)
    db.commit()
    row = db.execute(text(
        "SELECT raw_order_id, order_no, sales_order_id, import_batch_id, source,"
        " source_modified_at, order_amount, reconciled_amount, pending_amount,"
        " return_reconciled_amount, content_hash"
        " FROM f_sales_reconciliation_snapshot")).mappings().one()
    expected = snapshot_rows_from_orders(
        result.orders, batch_id=batch_id, source=SOURCE_IMPORT)[0]
    assert dict(row) == {k: expected[k] for k in row.keys()}


def test_loader_same_batch_replay_still_idempotent(db):
    """（契约回归）共用构造后同批次重放仍不产生重复行——ON CONFLICT 语义不变。"""
    batch_id = db.execute(text(
        "INSERT INTO sys_import_batch (filename, file_type, file_hash, status) "
        "VALUES ('c5r.xlsx', 'sales', 'c5r-h1', 'success') RETURNING id")).scalar_one()
    result = TransformResult(file_type="sales", orders={"R-L": _head()})
    assert _write_sales_reconciliation_snapshots(db, result, batch_id, {}) == (1, 0)
    db.commit()
    assert _write_sales_reconciliation_snapshots(db, result, batch_id, {}) == (0, 1)
    db.commit()
    assert db.query(FSalesReconciliationSnapshot).count() == 1


# ---------------------------------------------------------------- S6 批量回填

def _seed_order(db, *, raw_id: str, batch_id: int, inc=None, is_inc=None, tax=None) -> int:
    return db.execute(text(
        "INSERT INTO f_sales_order (raw_order_id, order_no, order_date, salesperson,"
        " business_type, data_status, import_batch_id, amount_inc_tax,"
        " is_tax_inclusive, tax_amount)"
        " VALUES (:r, :no, DATE '2026-01-01', '张三', '备件销售', '已生效', :b,"
        " :inc, :is_inc, :tax) RETURNING id"),
        {"r": raw_id, "no": f"XS-{raw_id}", "b": batch_id,
         "inc": str(inc) if inc is not None else None, "is_inc": is_inc,
         "tax": str(tax) if tax is not None else None}).scalar_one()


def _batch_id(db, name: str) -> int:
    return db.execute(text(
        "INSERT INTO sys_import_batch (filename, file_type, file_hash, status) "
        "VALUES (:f, 'sales', :h, 'success') RETURNING id"),
        {"f": f"{name}.xlsx", "h": f"c6-{name}"}).scalar_one()


@pytest.fixture()
def fills_db(db):
    from scripts import backfill_sales_reconciliation as bs
    b1 = _batch_id(db, "fills1")
    b2 = _batch_id(db, "fills2")
    o1 = _seed_order(db, raw_id="R-F1", batch_id=b1)                    # 全 NULL，待补
    o2 = _seed_order(db, raw_id="R-F2", batch_id=b1, inc="999.00")      # 已有值，不得覆盖
    o3 = _seed_order(db, raw_id="R-F3", batch_id=b2)                    # 他批次同号，不得误写
    db.commit()
    return SimpleNamespace(db=db, b1=b1, b2=b2, o1=o1, o2=o2, o3=o3,
                           apply=bs._apply_tax_fills)


def test_tax_fills_batch_update_null_only_in_batch(fills_db):
    """批量 UPDATE 只补本批次 NULL 列：b1 的 R-F1 补齐、R-F2 已有值不动。"""
    f = fills_db
    fills = [
        {"raw_order_id": "R-F1", "column": "amount_inc_tax", "value": Decimal("1130.00")},
        {"raw_order_id": "R-F2", "column": "amount_inc_tax", "value": Decimal("1130.00")},
        {"raw_order_id": "R-F1", "column": "is_tax_inclusive", "value": True},
    ]
    assert f.apply(f.db, SimpleNamespace(id=f.b1), fills) == 2   # R-F1 两列
    f.db.commit()
    r1 = f.db.execute(text(
        "SELECT amount_inc_tax, is_tax_inclusive FROM f_sales_order WHERE id=:i"),
        {"i": f.o1}).one()
    assert r1.amount_inc_tax == Decimal("1130.00") and r1.is_tax_inclusive is True
    r2 = f.db.execute(text(
        "SELECT amount_inc_tax FROM f_sales_order WHERE id=:i"), {"i": f.o2}).one()
    assert r2.amount_inc_tax == Decimal("999.00")   # WHERE col IS NULL 守卫住了


def test_tax_fills_other_batch_row_untouched(fills_db):
    """本批次谓词守住：他批次同 raw_order_id 的 NULL 列不被本批次原件误写。"""
    f = fills_db
    fills = [{"raw_order_id": "R-F3", "column": "tax_amount", "value": Decimal("13.00")}]
    assert f.apply(f.db, SimpleNamespace(id=f.b1), fills) == 0
    f.db.commit()
    r3 = f.db.execute(text(
        "SELECT tax_amount FROM f_sales_order WHERE id=:i"), {"i": f.o3}).one()
    assert r3.tax_amount is None


def test_tax_fills_unknown_column_rejected(fills_db):
    """列名白名单外直接拒绝（VALUES/SET 拼接列名，防未来计划侧漂移注入）。"""
    f = fills_db
    fills = [{"raw_order_id": "R-F1", "column": "order_no; DROP TABLE x", "value": "1"}]
    with pytest.raises(ValueError, match="未登记的税列回填目标"):
        f.apply(f.db, SimpleNamespace(id=f.b1), fills)
    f.db.rollback()


def test_tax_fills_chunked_under_bind_parameter_limit(fills_db, monkeypatch):
    """分块执行（对抗复审 2026-10-18）：单语句绑定参数须远离 PG 65535 上限，
    超大批次按 _TAX_FILL_CHUNK 切成多条 UPDATE——把块压到 2 验证 3 行回填
    确实走 2 条语句、跨块行各自拿到**自己的**值（rid 关联不因分块错位）。"""
    from sqlalchemy import event
    from scripts import backfill_sales_reconciliation as bs
    f = fills_db
    monkeypatch.setattr(bs, "_TAX_FILL_CHUNK", 2)
    fills = [{"raw_order_id": rid, "column": "tax_amount", "value": Decimal(v)}
             for rid, v in (("R-F1", "1.00"), ("R-F2", "2.00"), ("R-F3", "9.00"))]
    stmts: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        stmts.append(statement)

    event.listen(f.db.get_bind(), "before_cursor_execute", _capture)
    try:
        assert f.apply(f.db, SimpleNamespace(id=f.b1), fills) == 2   # R-F3 他批次
    finally:
        event.remove(f.db.get_bind(), "before_cursor_execute", _capture)
    f.db.commit()
    updates = [s for s in stmts if s.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 2                        # 3 行 / 块 2 → 两条语句
    values = dict(f.db.execute(text(
        "SELECT raw_order_id, tax_amount FROM f_sales_order"
        " WHERE import_batch_id=:b ORDER BY raw_order_id"),
        {"b": f.b1}).all())
    assert values == {"R-F1": Decimal("1.00"), "R-F2": Decimal("2.00")}
