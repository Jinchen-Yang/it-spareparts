"""D-34：全局幂等回执索引的迁移链、查询计划及无损往返。"""
import hashlib
import json

from alembic.script import ScriptDirectory
from sqlalchemy import text

from alembic import command
from app.db import engine
from tests.conftest import _alembic_cfg

_REVISION = "d8f6a2c4b9e7"
_PREVIOUS = "c9d4f2a6b8e1"
_INDEX = "ix_batch_source_cancel_receipts"
_RESULTS_KEY = "_source_cancel_confirmations"


def _index_state(connection):
    return connection.execute(text("""
        SELECT ix.indisvalid, ix.indisready, am.amname,
               pg_get_expr(ix.indexprs, ix.indrelid) AS expression,
               pg_get_expr(ix.indpred, ix.indrelid) AS predicate
        FROM pg_index ix
        JOIN pg_class idx ON idx.oid = ix.indexrelid
        JOIN pg_am am ON am.oid = idx.relam
        WHERE idx.relname = :index_name
    """), {"index_name": _INDEX}).mappings().one_or_none()


def _plan_indexes(node):
    indexes = {node["Index Name"]} if "Index Name" in node else set()
    for child in node.get("Plans", []):
        indexes.update(_plan_indexes(child))
    return indexes


def test_receipt_index_is_additive_child_on_single_migration_chain():
    script = ScriptDirectory.from_config(_alembic_cfg())
    revision = script.get_revision(_REVISION)
    assert revision.down_revision == _PREVIOUS
    heads = script.get_heads()
    assert len(heads) == 1
    assert _REVISION in {
        item.revision for item in script.iterate_revisions(heads[0], "base")
    }


def test_receipt_index_is_valid_gin_without_history_scope_filter(db):
    row = _index_state(db)
    assert row is not None, "全局取消确认回执缺少专用索引"
    assert row["indisvalid"] and row["indisready"]
    assert row["amname"] == "gin"
    assert _RESULTS_KEY in row["expression"]
    assert "->" in row["expression"]
    # 回执无有效期，不能因批次后续 superseded 或类型变化失去同键冲突保护。
    assert row["predicate"] is None


def test_receipt_lookup_plan_uses_index_and_retains_old_cross_batch_keys(db):
    from app.services.maintenance_source_cancel_cascade import receipt_batch_query

    # 合成 20,000 个无关批次；不禁用 seqscan，不用时间阈值冒充索引计划证据。
    db.execute(text("""
        INSERT INTO sys_import_batch
            (filename, file_type, file_hash, status, report_json)
        SELECT 'receipt-plan-' || n || '.xlsx',
               CASE WHEN n % 2 = 0 THEN 'maintenance' ELSE 'sales' END,
               md5(n::text), 'superseded', jsonb_build_object('summary', n)
        FROM generate_series(1, 20000) AS n
    """))
    keys = [
        hashlib.sha256(json.dumps(
            [actor, "same-confirmation"], separators=(",", ":")
        ).encode()).hexdigest()
        for actor in ("first-operator", "other-operator")
    ]
    batch_ids = []
    for ordinal, (file_type, status) in enumerate(
        [("maintenance", "superseded"), ("sales", "failed")]
    ):
        report = {_RESULTS_KEY: {
            keys[ordinal]: {
                "request_digest": f"request-{ordinal}",
                "result": {"source_order_id": f"old-source-{ordinal}"},
            }
        }}
        batch_ids.append(db.execute(text("""
            INSERT INTO sys_import_batch
                (filename, file_type, file_hash, status, uploaded_at, report_json)
            VALUES (:filename, :file_type, :file_hash, :status,
                    '2020-01-01T00:00:00Z', CAST(:report AS jsonb))
            RETURNING id
        """), {
            "filename": f"old-receipt-{ordinal}.xlsx",
            "file_type": file_type,
            "file_hash": f"old-receipt-{ordinal}",
            "status": status,
            "report": json.dumps(report),
        }).scalar_one())
    db.execute(text("ANALYZE sys_import_batch"))
    for key, batch_id in zip(keys, batch_ids, strict=True):
        assert [row.id for row in db.scalars(receipt_batch_query(key))] == [batch_id]
    assert not db.scalars(receipt_batch_query("unknown-key")).all()
    # 使用确认命令的真实全实体 SELECT；状态查询已知 batch_id，走主键。
    lookup_sql = str(receipt_batch_query(keys[0]).compile(
        bind=db.get_bind(), compile_kwargs={"literal_binds": True},
    ))
    explain = text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + lookup_sql)
    print("receipt lookup SQL:", lookup_sql)
    # 同一数据、投影与 SQL 对拍；仅在 SAVEPOINT 内暂时移除索引。
    # rollback 恢复索引，不提交或清除外层合成数据。
    savepoint = db.begin_nested()
    try:
        db.execute(text(f"DROP INDEX {_INDEX}"))
        assert _index_state(db) is None
        before = db.scalar(explain)
        print("receipt lookup EXPLAIN before:", json.dumps(before, ensure_ascii=False))
        assert before[0]["Plan"]["Node Type"] == "Seq Scan"
        assert _INDEX not in _plan_indexes(before[0]["Plan"])
    finally:
        savepoint.rollback()
    assert _index_state(db)["indisvalid"]
    after = db.scalar(explain)
    print("receipt lookup EXPLAIN after:", json.dumps(after, ensure_ascii=False))
    assert _INDEX in _plan_indexes(after[0]["Plan"])
    assert before[0]["Plan"]["Actual Rows"] == after[0]["Plan"]["Actual Rows"] == 1


def test_receipt_index_migration_roundtrip_preserves_reports_and_orm(db):
    report = {_RESULTS_KEY: {"historic-key": {
        "request_digest": "unchanged-digest",
        "result": {"source_order_id": "old-source", "voided_lines": 2},
    }}, "unrelated": {"keep": True}}
    db.execute(text("""
        INSERT INTO sys_import_batch
            (filename, file_type, file_hash, status, report_json)
        VALUES ('preserved.xlsx', 'maintenance', 'preserved-hash',
                'superseded', CAST(:report AS jsonb))
    """), {"report": json.dumps(report)})
    db.commit()
    db.close()
    cfg = _alembic_cfg()
    try:
        with engine.connect() as connection:
            assert _index_state(connection) is not None
        command.downgrade(cfg, _PREVIOUS)
        with engine.connect() as connection:
            assert _index_state(connection) is None
            assert connection.scalar(text(
                "SELECT report_json FROM sys_import_batch "
                "WHERE file_hash = 'preserved-hash'"
            )) == report
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert _index_state(connection)["indisvalid"]
            assert connection.scalar(text(
                "SELECT report_json FROM sys_import_batch "
                "WHERE file_hash = 'preserved-hash'"
            )) == report
        command.check(cfg)
    finally:
        command.upgrade(cfg, "head")
