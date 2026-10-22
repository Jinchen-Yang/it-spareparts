"""D-36：通过真实 PG 和发布者公共入口验证事务、闭包及恢复。"""
from sqlalchemy import select, text

from app.db import engine
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.services.catalog_v2.publisher import publish_once, seed, status


def test_seed_materializes_all_states_and_starts_at_zero(db):
    a = DimPart(pn_std="SEED-A", needs_review=True, is_excluded=True,
                locked_fields=["brand"], brand="Demo")
    db.add(a)
    db.flush()
    db.add(ProductSpec(part_id=a.id, spec_key="historic_key", spec_value="opaque",
                       source="manual"))
    db.add(PartAlias(part_id=a.id, pn_std=a.pn_std, pn_raw="SEED-OLD", status=None))
    db.commit()
    epoch = seed(engine)
    assert status(engine)["publication_epoch"] == epoch
    assert status(engine)["status"] == "ready"
    assert status(engine)["watermark"] == 0
    from app.models.catalog_v2 import CatalogV2Projection
    rows = db.scalars(select(CatalogV2Projection).where(
        CatalogV2Projection.epoch_id == epoch)).all()
    node = next(row.data for row in rows if row.kind == "node")
    assert node["needs_review"] is True and node["is_excluded"] is True
    assert node["locked_fields"] == ["brand"]
    assert node["specs"] == [{"key": "historic_key", "value": "opaque", "unit": None,
                              "numeric_value": None, "source": "manual"}]
    assert next(row.data for row in rows if row.kind == "alias")["status"] is None
    db.rollback()
    assert publish_once(engine) is None


def _parts(db, *names):
    rows = [DimPart(pn_std=name) for name in names]
    db.add_all(rows)
    db.commit()
    return [row.id for row in rows]


def _nodes(batch):
    return {i["node"]["part_id"]: i["node"] for i in batch["items"] if i["kind"] == "node"}


def test_t1_a_t2_a_and_b_publishes_entire_fixed_point(db):
    a, b = _parts(db, "FIXED-A", "FIXED-B")
    seed(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE dim_part SET description='T1' WHERE id=:id"), {"id": a})
        t1 = conn.scalar(text("SELECT txid_current()"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE dim_part SET description='T2' WHERE id IN (:a,:b)"),
                     {"a": a, "b": b})
        t2 = conn.scalar(text("SELECT txid_current()"))
    batch = publish_once(engine)
    assert batch["transaction_groups"] == sorted([str(t1), str(t2)])
    assert set(_nodes(batch)) == {a, b}
    assert {n["description"] for n in _nodes(batch).values()} == {"T2"}
    assert batch["previous_sequence"] == 0 and batch["published_sequence"] == 1
    assert publish_once(engine) is None


def test_visible_but_unsafe_t2_defers_entire_component(db):
    a, b = _parts(db, "UNSAFE-A", "UNSAFE-B")
    seed(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE dim_part SET description='T1' WHERE id=:id"), {"id": a})
    with engine.connect() as blocker:
        blocker.execute(text("SELECT txid_current()"))
        with engine.begin() as conn:
            conn.execute(text("UPDATE dim_part SET description='T2' WHERE id IN (:a,:b)"),
                         {"a": a, "b": b})
        assert publish_once(engine) is None
        assert status(engine)["watermark"] == 0
        blocker.rollback()
    batch = publish_once(engine)
    assert set(_nodes(batch)) == {a, b}
    assert {n["description"] for n in _nodes(batch).values()} == {"T2"}


def test_outbox_100_late_commit_and_101_early_commit_are_both_published(db):
    seed(engine)
    with engine.begin() as conn:
        conn.execute(text("SELECT setval('catalog_v2_change_id_seq', 99, true)"))
    with engine.connect() as late, engine.connect() as early:
        a = late.scalar(text("INSERT INTO dim_part(pn_std) VALUES ('LATE-100') RETURNING id"))
        assert late.scalar(text("SELECT id FROM catalog_v2_change WHERE part_id=:id"), {"id": a}) == 100
        b = early.scalar(text("INSERT INTO dim_part(pn_std) VALUES ('EARLY-101') RETURNING id"))
        assert early.scalar(text("SELECT id FROM catalog_v2_change WHERE part_id=:id"), {"id": b}) == 101
        early.commit()
        assert publish_once(engine) is None  # xid horizon includes the still active earlier writer
        late.commit()
    batch = publish_once(engine)
    assert set(_nodes(batch)) == {a, b}
    assert status(engine)["pending_changes"] == 0


def test_seed_does_not_swallow_preexisting_uncommitted_writer(db):
    with engine.connect() as late:
        part_id = late.scalar(text("INSERT INTO dim_part(pn_std) VALUES ('SEED-LATE') RETURNING id"))
        seed(engine)
        assert status(engine)["watermark"] == 0
        late.commit()
    assert part_id in _nodes(publish_once(engine))


def test_noop_and_spec_only_delete(db):
    (a,) = _parts(db, "SPEC-DELETE")
    db.add(ProductSpec(part_id=a, spec_key="capacity", spec_value="1 GB", source="auto"))
    db.commit()
    seed(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE dim_part SET description=description, updated_at=now() WHERE id=:id"), {"id": a})
        conn.execute(text("UPDATE product_specs SET spec_value=spec_value WHERE part_id=:id"), {"id": a})
    assert publish_once(engine) is None
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM product_specs WHERE part_id=:id"), {"id": a})
    batch = publish_once(engine)
    assert _nodes(batch)[a]["specs"] == []
    assert batch["published_sequence"] == 1


def test_dim_part_delete_stops_without_advancing_and_reseed_changes_epoch(db):
    from app.models.catalog_v2 import CatalogV2Alarm, CatalogV2Batch
    (a,) = _parts(db, "DELETE-ALARM")
    old_epoch = seed(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dim_part WHERE id=:id"), {"id": a})
    assert publish_once(engine) is None
    assert status(engine)["status"] == "stopped"
    assert status(engine)["watermark"] == 0
    assert status(engine)["pending_changes"] > 0
    assert db.scalar(select(CatalogV2Alarm.code)) == "node_deleted"
    assert db.scalar(select(CatalogV2Batch)) is None
    assert publish_once(engine) is None
    db.rollback()
    new_epoch = seed(engine)
    assert new_epoch != old_epoch
    assert status(engine)["status"] == "ready"
    assert status(engine)["watermark"] == 0


def test_alias_move_delete_merge_unmerge_and_path_compression(db):
    from app.services.merge import merge_parts, unmerge
    a, b, c = _parts(db, "MERGE-A", "MERGE-B", "MERGE-C")
    alias = PartAlias(part_id=a, pn_std="MERGE-A", pn_raw="MERGE-OLD", status="active")
    gone = PartAlias(part_id=a, pn_std="MERGE-A", pn_raw="MERGE-DELETED", status="pending")
    db.add_all([alias, gone])
    db.commit()
    alias_id, gone_id = alias.id, gone.id
    seed(engine)
    db.delete(gone)
    db.commit()
    result = merge_parts(db, "MERGE-A", "MERGE-B", "synthetic", "test")
    log_id = result["merge_log_id"]
    batch = publish_once(engine)
    assert _nodes(batch)[a]["canonical_part_id"] == b
    assert {a, b} <= set(_nodes(batch))
    assert {"alias_id": alias_id, "old_part_id": a, "new_part_id": b} in batch["alias_moves"]
    assert any(i["kind"] == "alias" and i["alias"]["alias_id"] == gone_id
               and i["alias"]["deleted"] for i in batch["items"])
    result2 = merge_parts(db, "MERGE-B", "MERGE-C", "synthetic", "test")
    compressed = publish_once(engine)
    assert set(_nodes(compressed)) == {a, b, c}
    assert _nodes(compressed)[a]["merged_into_id"] == c
    unmerge(db, result2["merge_log_id"], "test")
    restored = publish_once(engine)
    assert _nodes(restored)[a]["merged_into_id"] == b
    unmerge(db, log_id, "test")
    final = publish_once(engine)
    assert _nodes(final)[a]["canonical_part_id"] == a
    assert {"alias_id": alias_id, "old_part_id": b, "new_part_id": a} in final["alias_moves"]
    assert not any(i["kind"] == "alias" and i["alias"]["alias_id"] == gone_id
                   and not i["alias"]["deleted"] for i in final["items"])


def test_publication_is_atomic_when_projection_write_fails(db):
    import pytest
    from sqlalchemy.exc import IntegrityError
    from app.models.catalog_v2 import CatalogV2Batch
    (a,) = _parts(db, "ATOMIC")
    seed(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE dim_part SET description='fail-publication' WHERE id=:id"), {"id": a})
        conn.execute(text("ALTER TABLE catalog_v2_projection ADD CONSTRAINT test_publication_failure "
                          "CHECK (data->>'description' IS DISTINCT FROM 'fail-publication')"))
    try:
        with pytest.raises(IntegrityError):
            publish_once(engine)
        assert status(engine)["watermark"] == 0
        assert status(engine)["pending_changes"] > 0
        assert db.scalar(select(CatalogV2Batch)) is None
        db.rollback()
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE catalog_v2_projection DROP CONSTRAINT test_publication_failure"))
    assert publish_once(engine)["published_sequence"] == 1


def test_lock_is_acquired_before_repeatable_read_snapshot(db):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.services.catalog_v2.publisher import publication_transaction
    (a,) = _parts(db, "LOCK-SNAPSHOT")
    seed(engine)
    started = Event()

    def waiting_publisher():
        started.set()
        return publish_once(engine)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with publication_transaction(engine):
            pending = executor.submit(waiting_publisher)
            assert started.wait(5)
            deadline = time.monotonic() + 5
            while True:
                with engine.connect() as probe:
                    waiting = probe.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"))
                if waiting:
                    break
                assert time.monotonic() < deadline, "publisher did not wait on the single publisher lock"
                time.sleep(.01)
            with engine.begin() as conn:
                conn.execute(text("UPDATE dim_part SET description='after-lock-wait' WHERE id=:id"), {"id": a})
        batch = pending.result(timeout=10)
    assert _nodes(batch)[a]["description"] == "after-lock-wait"


def test_cleanup_keeps_thirty_days_and_exposes_retained_boundary(db):
    import pytest
    from app.models.catalog_v2 import CatalogV2Batch, CatalogV2Epoch
    from app.services.catalog_v2.publisher import cleanup
    (a,) = _parts(db, "RETENTION")
    epoch = seed(engine)
    for value in ("first", "second"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE dim_part SET description=:value WHERE id=:id"), {"value": value, "id": a})
        publish_once(engine)
    with pytest.raises(ValueError):
        cleanup(engine, retain_days=29)
    assert cleanup(engine) == 0
    with engine.begin() as conn:
        conn.execute(text("UPDATE catalog_v2_batch SET created_at=now()-interval '31 days' WHERE published_sequence=1"))
    assert cleanup(engine) == 1
    assert db.get(CatalogV2Epoch, epoch).retained_from_sequence == 1
    assert db.get(CatalogV2Batch, (epoch, 2)) is not None


def test_batch_payload_fragments_persist_in_publication_transaction(db, monkeypatch):
    from app.config import get_settings
    from app.models.catalog_v2 import CatalogV2Batch, CatalogV2PayloadFragment
    from app.services.catalog_v2.canonical import bytes_hash, strict_loads
    (a,) = _parts(db, "PERSISTENT-FRAGMENTS")
    seed(engine)
    monkeypatch.setattr(get_settings(), "catalog_v2_response_max_bytes", 4096)
    monkeypatch.setattr(get_settings(), "catalog_v2_fragment_max_bytes", 1537)
    with engine.begin() as conn:
        conn.execute(text("UPDATE dim_part SET description=:value WHERE id=:id"),
                     {"value": "中文" * 2500, "id": a})
    batch = publish_once(engine)
    stored = db.get(CatalogV2Batch, (batch["publication_epoch"], 1))
    fragments = db.scalars(select(CatalogV2PayloadFragment).order_by(
        CatalogV2PayloadFragment.part_no)).all()
    assert len(fragments) > 1
    payload = b"".join(f.data for f in fragments)
    assert payload == stored.payload
    assert {f.payload_hash for f in fragments} == {bytes_hash(payload)}
    assert all(f.part_hash == bytes_hash(f.data) for f in fragments)
    assert strict_loads(payload)["batch_hash"] == batch["batch_hash"]


def test_unresolvable_legacy_alias_stops_instead_of_marking_published(db):
    from app.models.catalog_v2 import CatalogV2Alarm
    seed(engine)
    # The legacy schema allows NULL part_id; an exact missing pn_std is not an identity.
    db.add(PartAlias(part_id=None, pn_std="NO-SUCH-NODE", pn_raw="ORPHAN-ALIAS", status=None))
    db.commit()
    assert publish_once(engine) is None
    assert status(engine)["status"] == "stopped"
    assert status(engine)["pending_changes"] > 0
    assert db.scalar(select(CatalogV2Alarm.code)) == "invalid_source"


def test_invalid_spec_source_stops_atomically(db):
    from app.models.catalog_v2 import CatalogV2Alarm
    (a,) = _parts(db, "INVALID-SPEC")
    seed(engine)
    db.add(ProductSpec(part_id=a, spec_key="historic_key", spec_value="v", source="unknown"))
    db.commit()
    assert publish_once(engine) is None
    assert status(engine)["watermark"] == 0
    assert status(engine)["status"] == "stopped"
    assert db.scalar(select(CatalogV2Alarm.code)) == "invalid_source"


def test_legacy_alias_lost_by_rename_stops_without_guessing_a_deletion(db):
    from app.services.part_rename import rename_part
    (a,) = _parts(db, "LEGACY-OWNER")
    db.add(PartAlias(part_id=None, pn_std="LEGACY-OWNER", pn_raw="LEGACY-SPELLING", status=None))
    db.commit()
    seed(engine)
    rename_part(db, a, "LEGACY-RENAMED", "synthetic", "test")
    assert publish_once(engine) is None
    assert status(engine)["status"] == "stopped"
    assert status(engine)["watermark"] == 0


def test_alias_move_before_first_publication_has_both_parents_and_move(db):
    a, b = _parts(db, "NEW-ALIAS-A", "NEW-ALIAS-B")
    seed(engine)
    alias = PartAlias(part_id=a, pn_std="NEW-ALIAS-A", pn_raw="NEW-MOVING-ALIAS", status="active")
    db.add(alias)
    db.commit()
    aid = alias.id
    with engine.begin() as conn:
        conn.execute(text("UPDATE part_alias SET part_id=:b,pn_std='NEW-ALIAS-B' WHERE id=:id"),
                     {"b": b, "id": aid})
    batch = publish_once(engine)
    assert set(_nodes(batch)) == {a, b}
    assert batch["alias_moves"] == [{"alias_id": aid, "old_part_id": a, "new_part_id": b}]
