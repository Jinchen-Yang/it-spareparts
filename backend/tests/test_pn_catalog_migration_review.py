"""Independent migration review invariants against the real database."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_models import Asset, GalleryEntry
from app.pn_catalog.migration import stage_manifest
from app.pn_catalog.migration_materialize import materialize_batch
from app.pn_catalog.migration_models import LegacySourceRecord
from tests.test_pn_catalog_migration import (
    allow,
    complete_image_manifest,
    context,
    proof,
)


def test_materialization_cannot_consume_payload_drift_after_manifest_was_frozen(db):
    value = complete_image_manifest(status="deleted")
    stage_manifest(db, value, verified_source=proof(value), context=context(), authorize=allow)
    db.commit()
    row = db.scalar(select(LegacySourceRecord))
    # Simulate an accidental maintenance rewrite between stage and materialization.
    row.payload = {**row.payload, "oss_key": "original/not-in-reviewed-manifest.jpg"}
    try:
        db.commit()
    except DBAPIError as error:
        assert error.orig.sqlstate == "55000"
        db.rollback()  # A DB evidence-immutability guard is also a valid implementation.
        return
    try:
        materialize_batch(db, value.batch_id, context=context(), authorize=allow)
    except CatalogError as error:
        assert error.code in {"migration.source_changed", "migration.record_conflict", "migration.invalid_record"}
        db.rollback()
        return
    entry = db.get(GalleryEntry, UUID(value.records[0].source_id))
    materialized_key = db.get(Asset, entry.asset_id).object_key
    assert materialized_key == value.records[0].payload["oss_key"], "materialized bytes were not bound to the frozen payload hash"


def _unique_manifest(pid, index):
    from app.pn_catalog.migration_dto import MigrationManifest, fingerprint
    value = complete_image_manifest(target=pid, order=index)
    raw = value.model_dump(mode="python")
    record = raw["records"][0]
    record["payload"].update({"oss_key": f"original/part-{index}.jpg", "thumb_key": f"original/part-{index}-thumb.jpg",
                             "sha256": str(index) * 64})
    record["payload_sha256"] = fingerprint(record["payload"])
    return MigrationManifest.model_validate(raw)


def test_two_batches_for_one_target_preserve_both_images_and_gallery_revision(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy.orm import Session

    from app.db import engine
    from app.models.dimensions import DimPart
    from app.pn_catalog.media_models import GalleryGroup
    part = DimPart(pn_std="MIGRATION-CONCURRENT-TARGET")
    db.add(part)
    db.commit()
    values = [_unique_manifest(part.id, index) for index in (1, 2)]
    for value in values:
        stage_manifest(db, value, verified_source=proof(value), context=context(), authorize=allow)
    db.commit()
    barrier = Barrier(2)
    def run(value):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            result = materialize_batch(session, value.batch_id, context=context(), authorize=allow)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, values))
    assert all(result["batch_complete"] for result in results)
    group = db.scalar(select(GalleryGroup).where(GalleryGroup.canonical_part_id == part.id))
    assert group.revision == 2
    assert set(group.ordered_entry_ids) == {value.records[0].source_id for value in values}


def test_manual_gallery_order_and_new_batch_race_cannot_overwrite_each_other(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy.orm import Session

    from app.db import engine
    from app.models.dimensions import DimPart
    from app.pn_catalog.media import arrange_gallery
    from app.pn_catalog.media_dto import ArrangeGallery
    from app.pn_catalog.media_models import GalleryGroup
    part = DimPart(pn_std="MIGRATION-MANUAL-RACE")
    db.add(part)
    db.commit()
    first, second = _unique_manifest(part.id, 1), _unique_manifest(part.id, 2)
    for value in (first, second):
        stage_manifest(db, value, verified_source=proof(value), context=context(), authorize=allow)
    materialize_batch(db, first.batch_id, context=context(), authorize=allow)
    db.commit()
    pid, eid, barrier = part.id, UUID(first.records[0].source_id), Barrier(2)
    def run(kind):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                if kind == "manual":
                    arrange_gallery(session, pid, ArrangeGallery(expected_gallery_revision=1,
                        primary_entry_id=eid, ordered_entry_ids=[eid]), context=context(), authorize=allow)
                else:
                    materialize_batch(session, second.batch_id, context=context(), authorize=allow)
                session.commit()
                return "ok"
            except CatalogError as error:
                session.rollback()
                return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["manual", "migration"]))
    assert results.count("ok") == 1
    assert set(results) <= {"ok", "gallery.version_conflict", "migration.gallery_conflict"}
    group = db.scalar(select(GalleryGroup).where(GalleryGroup.canonical_part_id == pid))
    assert group.revision == 2
    assert str(eid) in group.ordered_entry_ids
