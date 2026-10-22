"""D-36：通过真实 PostgreSQL 写入口验证捕获、版本和既有导入契约。"""

from datetime import date
from decimal import Decimal

import pytest
from alembic import command as alembic_command
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.etl import loader
from app.models.catalog_v2 import CatalogV2Change
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.models.purchase import FPurchaseLine
from app.models.system import SysImportBatch
from app.services import merge, part_rename
from tests import factories as f


def _revisions(db, part_id):
    return tuple(db.execute(select(DimPart.aggregate_revision, DimPart.identity_revision)
                            .where(DimPart.id == part_id)).one())


def _changes(db, **where):
    return list(db.scalars(select(CatalogV2Change).filter_by(**where).order_by(CatalogV2Change.id)))


def _clear_changes(db):
    db.execute(delete(CatalogV2Change))


def test_database_captures_orm_insert_in_the_same_transaction(db):
    part = DimPart(pn_std="CAPTURE-A")
    db.add(part)
    db.flush()
    record = db.execute(text(
        "SELECT txid, table_name, op, part_id FROM catalog_v2_change"
        " WHERE table_name = 'dim_part' AND part_id = :id"
    ), {"id": part.id}).one()
    assert tuple(record) == (
        db.scalar(text("SELECT txid_current()")), "dim_part", "I", part.id,
    )
    assert db.execute(text(
        "SELECT aggregate_revision, identity_revision FROM dim_part WHERE id = :id"
    ), {"id": part.id}).one() == (1, 1)


def test_core_bulk_and_upsert_are_captured_without_application_hooks(db):
    db.execute(insert(DimPart), [{"pn_std": "CORE-A"}, {"pn_std": "CORE-B"}])
    ids = list(db.scalars(select(DimPart.id).order_by(DimPart.id)))
    assert len(_changes(db, table_name="dim_part", op="I")) == 2
    db.execute(update(DimPart).where(DimPart.id.in_(ids)).values(description="bulk changed"))
    statement = pg_insert(DimPart).values(pn_std="CORE-A", brand="upsert brand")
    db.execute(statement.on_conflict_do_update(index_elements=[DimPart.pn_std], set_={"brand": statement.excluded.brand}))
    assert [_revisions(db, part_id) for part_id in ids] == [(3, 1), (2, 1)]
    assert len({row.txid for row in _changes(db)}) == 1


def test_real_noop_and_revision_spoof_do_not_capture_or_bump(db):
    part = DimPart(pn_std="NOOP", description="same", locked_fields=["brand", "description"])
    db.add(part)
    db.flush()
    _clear_changes(db)
    db.execute(update(DimPart).where(DimPart.id == part.id).values(
        description="same", updated_at=func.now(), exclude_reason="管理备注",
        locked_fields=["description", "brand", "brand"],
        aggregate_revision=999, identity_revision=0,
    ))
    assert _revisions(db, part.id) == (1, 1)
    assert _changes(db) == []
    statement = pg_insert(DimPart).values(pn_std="NOOP", description="same")
    db.execute(statement.on_conflict_do_update(index_elements=[DimPart.pn_std], set_={"description": statement.excluded.description}))
    assert _changes(db) == []


def test_specs_change_and_delete_bump_only_aggregate_and_keep_v1_timestamp(db):
    part = DimPart(pn_std="SPEC-A")
    db.add(part)
    db.flush()
    original_updated_at = part.updated_at
    db.execute(insert(ProductSpec).values(part_id=part.id, spec_key="legacy-key", spec_value="value"))
    assert _revisions(db, part.id) == (2, 1)
    _clear_changes(db)
    db.execute(update(ProductSpec).where(ProductSpec.part_id == part.id).values(spec_value="value", updated_at=func.now()))
    assert _changes(db) == []
    db.execute(delete(ProductSpec).where(ProductSpec.part_id == part.id))
    assert _revisions(db, part.id) == (3, 1)
    deleted = _changes(db, table_name="product_specs", op="D")
    assert len(deleted) == 1
    assert deleted[0].old_part_id == part.id and deleted[0].new_part_id is None
    assert db.scalar(select(DimPart.updated_at).where(DimPart.id == part.id)) == original_updated_at


def test_alias_move_and_delete_retain_both_parents_and_old_row(db):
    a, b = DimPart(pn_std="ALIAS-A"), DimPart(pn_std="ALIAS-B")
    db.add_all([a, b])
    db.flush()
    alias = PartAlias(pn_raw="OLD-PN", pn_std=a.pn_std, part_id=a.id, status="pending")
    db.add(alias)
    db.flush()
    assert _revisions(db, a.id) == (2, 1)
    alias.status = "active"
    db.flush()
    assert _revisions(db, a.id) == (3, 2)
    _clear_changes(db)
    db.execute(update(PartAlias).where(PartAlias.id == alias.id).values(part_id=b.id, pn_std=b.pn_std))
    moved = _changes(db, table_name="part_alias", op="U")[0]
    assert (moved.old_part_id, moved.new_part_id, moved.alias_id) == (a.id, b.id, alias.id)
    assert _revisions(db, a.id) == (4, 3) and _revisions(db, b.id) == (2, 2)
    db.execute(delete(PartAlias).where(PartAlias.id == alias.id))
    deleted = _changes(db, table_name="part_alias", op="D")[0]
    assert deleted.old_row == {"id": alias.id, "pn_raw": "OLD-PN", "pn_std": "ALIAS-B", "part_id": b.id, "status": "active"}
    assert deleted.new_row is None
    assert _revisions(db, b.id) == (3, 3)


def test_dim_delete_and_transaction_rollback_keep_atomic_capture(db):
    part = DimPart(pn_std="DELETE-MARKER")
    db.add(part)
    db.commit()
    part_id = part.id
    _clear_changes(db)
    db.commit()
    db.execute(delete(DimPart).where(DimPart.id == part_id))
    assert _changes(db, table_name="dim_part", op="D")[0].part_id == part_id
    db.rollback()
    assert db.get(DimPart, part_id) is not None
    assert _changes(db) == []
    db.execute(delete(DimPart).where(DimPart.id == part_id))
    db.commit()
    assert _changes(db, table_name="dim_part", op="D")[0].old_row["pn_std"] == "DELETE-MARKER"


def test_merge_unmerge_path_compression_and_rename_all_capture(db):
    a, b, c = [DimPart(pn_std=f"MERGE-{name}") for name in "ABC"]
    db.add_all([a, b, c])
    db.flush()
    db.add_all([
        PartAlias(pn_raw="ORIGINAL", pn_std=a.pn_std, part_id=a.id, status="active"),
        ProductSpec(part_id=a.id, spec_key="capacity", spec_value="960 GB", source="manual"),
    ])
    db.commit()
    merge.merge_parts(db, a.pn_std, b.pn_std, "test", "tester")
    _clear_changes(db)
    db.commit()
    result = merge.merge_parts(db, b.pn_std, c.pn_std, "test", "tester")
    captures = _changes(db)
    assert {a.id, b.id, c.id} <= {row.part_id for row in captures}
    assert len({row.txid for row in captures}) == 1
    assert any(row.table_name == "dim_part" and row.part_id == a.id and row.new_row["merged_into_id"] == c.id for row in captures)
    assert any(row.table_name == "product_specs" and row.old_part_id == b.id and row.new_part_id == c.id for row in captures)
    _clear_changes(db)
    db.commit()
    merge.unmerge(db, result["merge_log_id"], "tester")
    captures = _changes(db)
    assert any(row.table_name == "dim_part" and row.part_id == a.id and row.new_row["merged_into_id"] == b.id for row in captures)
    assert any(row.table_name == "part_alias" and row.op == "D" for row in captures)
    _clear_changes(db)
    db.commit()
    before = _revisions(db, b.id)
    part_rename.rename_part(db, b.id, "RENAMED-B", "test", "tester")
    assert _revisions(db, b.id)[1] > before[1]
    assert {"dim_part", "part_alias"} <= {row.table_name for row in _changes(db)}


def test_loader_preserves_manual_null_and_reimport_does_not_bump(db):
    part = DimPart(pn_std="IMPORT-LOCKED", description=None, brand="Manual", unit="个",
                   locked_fields=["description", "brand"])
    batch = SysImportBatch(filename="capture.xlsx", file_type="purchase", file_hash="capture-test")
    db.add_all([part, batch])
    db.flush()
    result = f.purchase_result({"ORDER": f.purchase_head("ORDER")}, [
        f.purchase_line("ORDER", "LINE", part.pn_std, qty="2", price="50", description="不得覆盖人工空值", brand="Imported")
    ])
    loader.load(db, result, batch.id, date(2026, 6, 1))
    db.commit()
    db.refresh(part)
    assert part.description is None and part.brand == "Manual"
    line = db.scalar(select(FPurchaseLine))
    assert line.part_id == part.id and line.qty == Decimal("2")
    before_revision = _revisions(db, part.id)
    before_updated_at = part.updated_at
    _clear_changes(db)
    db.commit()
    loader.load(db, result, batch.id, date(2026, 6, 1))
    db.commit()
    db.refresh(part)
    assert part.updated_at == before_updated_at
    assert _revisions(db, part.id) == before_revision
    assert _changes(db) == []
    assert db.scalar(select(func.count()).select_from(FPurchaseLine)) == 1


@pytest.mark.parametrize("status", [None, "pending", "rejected", "active"])
def test_alias_status_preserved_and_only_active_relation_bumps_identity(db, status):
    part = DimPart(pn_std="STATUS")
    db.add(part)
    db.flush()
    # 历史 part_id=NULL 仍按唯一 pn_std 捕获所属节点，不修改该历史源行。
    db.execute(insert(PartAlias).values(pn_raw="LEGACY", pn_std=part.pn_std, status=status))
    capture = _changes(db, table_name="part_alias")[0]
    assert capture.new_part_id == part.id and capture.new_row["status"] == status
    assert _revisions(db, part.id) == (2, 2 if status == "active" else 1)
    assert db.scalar(select(PartAlias.part_id)) is None


def test_migration_backfills_existing_rows_without_rewriting_source(db):
    from app.db import engine
    from tests.conftest import _alembic_cfg

    db.close()
    cfg = _alembic_cfg()
    alembic_command.downgrade(cfg, "c9d4f2a6b8e1")
    try:
        with engine.begin() as connection:
            before = connection.execute(text(
                "INSERT INTO dim_part(pn_std, description, locked_fields)"
                " VALUES ('PRE-MIGRATION', '保留原值', ARRAY['description'])"
                " RETURNING id, pn_std, description, locked_fields, updated_at"
            )).one()
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            after = connection.execute(text(
                "SELECT id, pn_std, description, locked_fields, updated_at,"
                " aggregate_revision, identity_revision FROM dim_part"
            )).one()
            assert tuple(after[:5]) == tuple(before)
            assert tuple(after[5:]) == (1, 1)
            assert connection.scalar(text("SELECT count(*) FROM catalog_v2_change")) == 0
            connection.execute(text("UPDATE dim_part SET brand = 'Captured'"))
            assert connection.scalar(text("SELECT count(*) FROM catalog_v2_change")) == 1
            assert connection.scalar(text("SELECT aggregate_revision FROM dim_part")) == 2
    finally:
        alembic_command.upgrade(cfg, "head")


def test_alias_review_and_parent_first_rename_do_not_deadlock(db):
    """人工审核与既有改名共用父先子后的锁顺序，捕获不可引入反向等待。"""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue

    from app.db import SessionLocal, engine
    from app.services.master_data import review_alias

    part = DimPart(pn_std="LOCK-PARENT")
    db.add(part)
    db.flush()
    alias = PartAlias(pn_raw="LOCK-OLD", pn_std=part.pn_std, part_id=part.id, status="pending")
    db.add(alias)
    db.commit()
    part_id, alias_id = part.id, alias.id
    db.rollback()
    worker_pid = Queue()

    def approve():
        with SessionLocal() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            worker_pid.put(session.scalar(text("SELECT pg_backend_pid()")))
            return review_alias(session, alias_id, "approve", None, "test")

    with ThreadPoolExecutor(max_workers=1) as executor:
        db.execute(select(DimPart).where(DimPart.id == part_id).with_for_update())
        pending = executor.submit(approve)
        pid = worker_pid.get(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            with engine.connect() as probe:
                waiting = probe.scalar(text(
                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                ), {"pid": pid})
            if waiting:
                break
            assert time.monotonic() < deadline, "别名审核未进入预期父锁等待"
            time.sleep(.01)
        try:
            part_rename.rename_part(db, part_id, "LOCK-RENAMED", "test", "test")
        finally:
            db.rollback()
        assert pending.result(timeout=6)["action"] == "approve"


def test_core_redirect_bumps_descendant_identity_without_rewriting_graph(db):
    a, b, c = [DimPart(pn_std=f"CHAIN-{name}") for name in "ABC"]
    db.add_all([a, b, c])
    db.flush()
    db.execute(update(DimPart).where(DimPart.id == a.id).values(status="merged", merged_into_id=b.id))
    before = _revisions(db, a.id)
    _clear_changes(db)
    db.execute(update(DimPart).where(DimPart.id == b.id).values(status="merged", merged_into_id=c.id))
    assert _revisions(db, a.id) == (before[0] + 1, before[1] + 1)
    assert db.scalar(select(DimPart.merged_into_id).where(DimPart.id == a.id)) == b.id
    assert {a.id, b.id} <= {row.part_id for row in _changes(db, table_name="dim_part")}
    # 非法环留给发布者停批，数据库捕获本身必须有限完成，不能递归至栈溢出。
    db.execute(text("SET LOCAL statement_timeout = '2s'"))
    db.execute(update(DimPart).where(DimPart.id == c.id).values(status="merged", merged_into_id=a.id))
    assert _revisions(db, a.id)[1] > before[1]
