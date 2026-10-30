"""D-0049: real PostgreSQL updates give each PN a strictly later version."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from queue import Queue
from threading import Event
from time import monotonic, sleep

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DataError, IntegrityError

from app.api.resale_catalog_export import _identity_items
from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.services import master_edit
from tests.conftest import _alembic_cfg

REVISION = "a3c5e7f9b2d4"
PARENT = "f1a3c5e7b9d2"
OLD = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _part(db, *, stamp=OLD, pn="VERSION-CONTRACT"):
    part = DimPart(pn_std=pn, description="before", updated_at=stamp)
    db.add(part)
    db.commit()
    assert part.updated_at == stamp  # INSERT preserves historical source times.
    return part


def test_row_change_uses_wall_clock_and_noop_preserves_version(db):
    part = _part(db)
    pid = part.id
    transaction_start = db.scalar(text("SELECT now()"))
    before = db.scalar(text("SELECT clock_timestamp()"))
    changed = db.execute(text("""
        UPDATE dim_part SET description='after' WHERE id=:id
        RETURNING updated_at, pn_compact, search_doc
    """), {"id": pid}).mappings().one()
    after = db.scalar(text("SELECT clock_timestamp()"))
    assert transaction_start <= before <= changed["updated_at"] <= after
    assert changed["updated_at"] > OLD
    assert changed["pn_compact"] == "VERSIONCONTRACT" and "after" in changed["search_doc"]
    # Generated columns unavailable in NEW must not turn a no-op into a change.
    same = db.scalar(text("""
        UPDATE dim_part SET description=description, updated_at=updated_at,
                            brand=brand, locked_fields=locked_fields
        WHERE id=:id RETURNING updated_at
    """), {"id": pid})
    assert same == changed["updated_at"]
    cleared = db.scalar(text("""
        UPDATE dim_part SET description=NULL WHERE id=:id RETURNING updated_at
    """), {"id": pid})
    assert cleared > same


def test_microsecond_floor_and_explicit_later_value_are_preserved(db):
    future = datetime(2100, 1, 1, 0, 0, 0, 999999, tzinfo=timezone.utc)
    part = _part(db, stamp=future)
    changed = db.scalar(text("""
        UPDATE dim_part SET brand='future' WHERE id=:id RETURNING updated_at
    """), {"id": part.id})
    assert changed == future + timedelta(microseconds=1)
    requested = future + timedelta(days=1)
    advanced = db.scalar(text("""
        UPDATE dim_part SET updated_at=:stamp WHERE id=:id RETURNING updated_at
    """), {"id": part.id, "stamp": requested})
    assert advanced == requested


def test_orm_noop_and_rolled_back_change_keep_committed_version(db):
    part = _part(db)
    pid = part.id
    part.description = "before"
    db.flush()
    assert db.scalar(select(DimPart.updated_at).where(DimPart.id == pid)) == OLD
    result = master_edit.edit_part_in_transaction(db, part_id=pid, pn_std=part.pn_std,
        updates={"description": "must roll back"}, operated_by="synthetic-version-test")
    changed = db.scalar(select(DimPart.updated_at).where(DimPart.id == pid))
    assert changed > OLD and result["edit_state"]["version"] == changed.isoformat()
    db.rollback()
    db.expire_all()
    assert db.get(DimPart, pid).description == "before"
    assert db.get(DimPart, pid).updated_at == OLD


def test_null_and_nonfinite_updates_fail_atomically(db):
    part = _part(db)
    for value, error in (("NULL", IntegrityError), ("'infinity'::timestamptz", DataError)):
        with pytest.raises(error), db.begin_nested():
            db.execute(text(f"""
                UPDATE dim_part SET updated_at={value}, description='invalid'
                WHERE id=:id
            """), {"id": part.id})
        db.refresh(part)
        assert part.updated_at == OLD and part.description == "before"


def test_postgresql_timestamp_overflow_does_not_partially_apply(db):
    # Exercise the SQL type's finite upper bound without decoding it into
    # Python's narrower datetime range. INSERT retains its original semantics.
    pid = db.scalar(text("""
        INSERT INTO dim_part (pn_std, description, updated_at)
        VALUES ('VERSION-MAX', 'before', '294276-12-31 23:59:59.999999+00')
        RETURNING id
    """))
    before = db.scalar(text("SELECT updated_at::text FROM dim_part WHERE id=:id"), {"id": pid})
    with pytest.raises(DataError), db.begin_nested():
        db.execute(text("UPDATE dim_part SET description='invalid' WHERE id=:id"), {"id": pid})
    row = db.execute(text("SELECT updated_at::text, description FROM dim_part WHERE id=:id"),
                     {"id": pid}).one()
    assert tuple(row) == (before, "before")


def test_waiting_earlier_transaction_exports_a_later_version(db):
    created = master_edit.create_part(db, pn_std="VERSION-WAITING", description="initial",
                                      force=True, operated_by="synthetic-version-test")
    baseline = master_edit.get_part_edit_state(db, created["id"])
    db.commit()
    started, allow_write = Queue(), Event()

    def early_writer():
        with SessionLocal() as early:
            started.put(early.execute(text("SELECT pg_backend_pid(), now()")).one())
            assert allow_write.wait(10)
            result = master_edit.edit_part_in_transaction(early, part_id=created["id"],
                pn_std=created["pn_std"], updates={"brand": "last committed"},
                base_snapshot=baseline, operated_by="synthetic-early")
            early.commit()
            return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(early_writer)
        early_pid, early_start = started.get(timeout=10)
        with SessionLocal() as later:
            later_pid, later_start = later.execute(text("SELECT pg_backend_pid(), now()")).one()
            assert early_start < later_start
            master_edit.edit_part_in_transaction(later, part_id=created["id"],
                pn_std=created["pn_std"], updates={"description": "first committed"},
                base_snapshot=baseline, operated_by="synthetic-later")
            previous = _identity_items(later, [(later.get(DimPart, created["id"]), None)])[0]
            allow_write.set()
            try:
                deadline = monotonic() + 10
                with SessionLocal() as monitor:
                    while True:
                        monitor.execute(text("SELECT pg_stat_clear_snapshot()"))
                        row = monitor.execute(text("""
                            SELECT wait_event_type, pg_blocking_pids(pid) AS blockers
                            FROM pg_stat_activity WHERE pid=:pid
                        """), {"pid": early_pid}).mappings().one()
                        if row["wait_event_type"] == "Lock" and later_pid in row["blockers"]:
                            break
                        assert monotonic() < deadline, "writer never reached the real master lock"
                        sleep(0.01)
                later.commit()
            finally:
                later.rollback()
            edited = future.result(timeout=15)
    with SessionLocal() as reader:
        part = reader.get(DimPart, created["id"])
        current = _identity_items(reader, [(part, None)])[0]
        assert part.description == "first committed" and part.brand == "last committed"
    assert edited["edit_state"]["version"] == current["updatedAt"]
    assert datetime.fromisoformat(current["updatedAt"]) > datetime.fromisoformat(previous["updatedAt"])


def test_trigger_covers_every_ordinary_column_and_preserves_schema(db):
    scripts = ScriptDirectory.from_config(_alembic_cfg())
    assert scripts.get_heads() == [REVISION]
    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == PARENT
    columns = inspect(db.get_bind()).get_columns("dim_part")
    assert set(revision.module.COMPARED_COLUMNS) == {
        column["name"] for column in columns if not column.get("computed")}
    assert {column["name"] for column in columns if column.get("computed")} == {
        "pn_compact", "search_doc"}
    stamp = next(column for column in columns if column["name"] == "updated_at")
    assert not stamp["nullable"] and stamp["type"].timezone


def test_migration_roundtrip_has_no_data_backfill_or_orm_drift(db):
    part = _part(db, pn="VERSION-MIGRATION")
    pid, engine = part.id, db.get_bind()
    db.commit()
    db.close()

    def stored():
        with engine.connect() as connection:
            return connection.scalar(text("SELECT to_jsonb(p)::text FROM dim_part p WHERE id=:id"),
                                     {"id": pid})

    cfg = _alembic_cfg()
    before = stored()
    try:
        command.downgrade(cfg, PARENT)
        assert stored() == before
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT to_regprocedure('itsp_dim_part_monotonic_updated_at()')")) is None
        command.upgrade(cfg, "head")
        assert stored() == before
        command.check(cfg)
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname='trg_dim_part_monotonic_updated_at'")) == 1
    finally:
        command.upgrade(cfg, "head")
