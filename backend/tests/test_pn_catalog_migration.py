"""Frozen-source checks, replay, rollback, and lossless unresolved migration evidence."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models.dimensions import DimPart
from app.pn_catalog.dto import (
    ActorContext,
    AuthorizationDecision,
    CommandContext,
    Provenance,
)
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_models import Asset, Derivative, GalleryEntry, GalleryGroup
from app.pn_catalog.migration import list_legacy_records, stage_manifest
from app.pn_catalog.migration_dto import (
    LegacyRecord,
    MigrationManifest,
    PartMapping,
    VerifiedSourceSnapshot,
    fingerprint,
    source_key,
)
from app.pn_catalog.migration_materialize import materialize_batch
from app.pn_catalog.migration_models import LegacySourceRecord, MigrationBatch
from app.pn_catalog.models import AuditEvent


def context():
    return CommandContext(request_id=uuid4(), actor=ActorContext(principal_id="account:42",
        client_id="migration", executor_id="offline-migration", entrypoint="migration",
        provenance=Provenance(kind="migration")))


def allow(*_args):
    return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"scope_kind": "all"})


def manifest(*, target=None, status="deleted", image_id=None):
    pn_id, image_id = uuid4(), image_id or uuid4()
    payload = {"id": str(image_id), "pn_id": str(pn_id), "status": status,
               "oss_key": "original/retained.jpg", "thumb_key": "original/retained-thumb.jpg",
               "sha256": "a" * 64, "uploaded_by": None, "source": "legacy"}
    record = LegacyRecord(kind="image", source_id=str(image_id), source_pn_id=pn_id,
                          payload=payload, payload_sha256=fingerprint(payload))
    mapping = PartMapping(source_pn_id=pn_id, target_part_id=target,
        expected_identity_revision=0 if target else None,
        disposition="mapped" if target else "unresolved", evidence="exact link" if target else "no source link",
        decision_ref="mapping-review:1" if target else None)
    return MigrationManifest(namespace="shop", snapshot_id=uuid4(), batch_id=uuid4(),
        source_schema="0023_catalog_import", exported_at=datetime.now(UTC), records=[record], mappings=[mapping])


def proof(value):
    return VerifiedSourceSnapshot(namespace=value.namespace, snapshot_id=value.snapshot_id,
        manifest_sha256=value.sha256, record_hashes={source_key(row): row.payload_sha256 for row in value.records},
        writer_fence_ref="isolated-copy:frozen:1")


def test_schema_rejects_guessed_mapping_tampered_payload_and_duplicate_records():
    value = manifest()
    with pytest.raises(ValidationError):
        PartMapping(source_pn_id=uuid4(), disposition="unresolved", target_part_id=1, evidence="guess")
    raw = value.model_dump(mode="python")
    raw["records"][0]["payload"]["status"] = "gallery"
    with pytest.raises(ValidationError, match="fingerprint mismatch"):
        MigrationManifest.model_validate(raw)
    with pytest.raises(ValidationError, match="duplicate legacy"):
        MigrationManifest.model_validate({**value.model_dump(), "records": value.records * 2})


def test_unresolved_deleted_images_are_preserved_discoverable_and_not_materialized(db):
    value, ctx = manifest(), context()
    result = stage_manifest(db, value, verified_source=proof(value), context=ctx, authorize=allow)
    db.commit()
    assert result["status"] == "archived"
    row = db.scalar(select(LegacySourceRecord))
    assert row.target_part_id is None and row.state == "unresolved"
    assert row.payload == value.records[0].payload
    page = list_legacy_records(db, namespace="shop", context=context(), authorize=allow)
    assert page["items"][0]["payload"]["status"] == "deleted"
    assert page["items"][0]["source_id"] == value.records[0].source_id
    assert page["items"][0]["read_only"] is True
    replay = stage_manifest(db, value, verified_source=proof(value), context=ctx, authorize=allow)
    assert replay["replayed"] is True
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_drift_rejects_entire_batch_and_does_not_hide_missing_records(db):
    value = manifest()
    stale = proof(value).model_copy(update={"record_hashes": {}})
    with pytest.raises(CatalogError) as caught:
        stage_manifest(db, value, verified_source=stale, context=context(), authorize=allow)
    assert caught.value.code == "migration.source_changed"
    assert db.scalar(select(func.count()).select_from(MigrationBatch)) == 0


def test_target_identity_checked_and_rollback_removes_batch_and_audit(db):
    part = DimPart(pn_std="MIGRATION-PN")
    db.add(part)
    db.commit()
    value = manifest(target=part.id)
    stage_manifest(db, value, verified_source=proof(value), context=context(), authorize=allow)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(MigrationBatch)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
    target = DimPart(pn_std="MIGRATION-TARGET")
    db.add(target)
    db.flush()
    part.status = "merged"
    part.merged_into_id = target.id
    db.commit()
    with pytest.raises(CatalogError) as caught:
        stage_manifest(db, value, verified_source=proof(value), context=context(), authorize=allow)
    assert caught.value.code == "catalog.identity_changed"


def test_other_batch_cannot_overwrite_source_record_or_mapping(db):
    value = manifest()
    stage_manifest(db, value, verified_source=proof(value), context=context(), authorize=allow)
    db.commit()
    raw = value.model_dump(mode="python")
    raw["batch_id"] = uuid4()
    raw["records"][0]["payload"]["status"] = "gallery"
    raw["records"][0]["payload_sha256"] = fingerprint(raw["records"][0]["payload"])
    changed = MigrationManifest.model_validate(raw)
    with pytest.raises(CatalogError) as caught:
        stage_manifest(db, changed, verified_source=proof(changed), context=context(), authorize=allow)
    assert caught.value.code == "migration.record_conflict"
    db.rollback()
    assert db.scalar(select(LegacySourceRecord)).payload["status"] == "deleted"


def test_current_permission_precedes_migration_replay_and_private_archive_reads(db):
    value, ctx = manifest(), context()
    stage_manifest(db, value, verified_source=proof(value), context=ctx, authorize=allow)
    db.commit()
    def restricted(*_args):
        return AuthorizationDecision(frozenset({"brand"}), frozenset({"brand"}), {"scope_kind": "all"})
    with pytest.raises(CatalogError) as denied:
        stage_manifest(db, value, verified_source=proof(value), context=ctx, authorize=restricted)
    assert denied.value.code == "auth.forbidden"
    with pytest.raises(CatalogError):
        list_legacy_records(db, namespace="shop", context=context(), authorize=restricted)


def complete_image_manifest(*, target=None, order=0, status="gallery"):
    value = manifest(target=target, status=status)
    raw = value.model_dump(mode="python")
    row = raw["records"][0]
    row["payload"].update({"width": 100, "height": 200, "bytes": 1234, "content_type": "image/jpeg",
                           "sort_order": order, "is_defect": False, "thumbnail_status": "ready"})
    row["payload_sha256"] = fingerprint(row["payload"])
    return MigrationManifest.model_validate(raw)


def test_image_materialization_preserves_ids_objects_and_unknown_verification(db):
    value = complete_image_manifest(status="deleted")
    stage_manifest(db, value, verified_source=proof(value), context=context(), authorize=allow)
    db.commit()
    ctx = context()
    result = materialize_batch(db, value.batch_id, context=ctx, authorize=allow)
    db.commit()
    assert result["cutover_ready"] is False and result["batch_complete"] is False
    entry = db.get(GalleryEntry, UUID(value.records[0].source_id))
    assert entry.status == "deleted" and entry.origin_part_id is None
    assert entry.origin_id == str(value.records[0].source_pn_id)
    asset = db.get(Asset, entry.asset_id)
    assert asset.object_key == "original/retained.jpg"
    assert asset.etag is None and asset.verification_status == "legacy_metadata"
    derivative = db.scalar(select(Derivative).where(Derivative.asset_id == asset.id))
    assert derivative.object_key == "original/retained-thumb.jpg" and derivative.status == "ready"
    assert materialize_batch(db, value.batch_id, context=ctx, authorize=allow)["replayed"] is True


def test_backfill_rejects_manual_gallery_order_change_and_rolls_back(db):
    part = DimPart(pn_std="GALLERY-MIGRATION")
    db.add(part)
    db.commit()
    first = complete_image_manifest(target=part.id, order=1)
    stage_manifest(db, first, verified_source=proof(first), context=context(), authorize=allow)
    materialize_batch(db, first.batch_id, context=context(), authorize=allow)
    db.commit()
    group = db.scalar(select(GalleryGroup))
    group.revision += 1
    db.commit()
    second = complete_image_manifest(target=part.id, order=0)
    raw = second.model_dump(mode="python")
    raw["records"][0]["payload"]["oss_key"] = "original/new.jpg"
    raw["records"][0]["payload"]["thumb_key"] = "original/new-thumb.jpg"
    raw["records"][0]["payload_sha256"] = fingerprint(raw["records"][0]["payload"])
    second = MigrationManifest.model_validate(raw)
    stage_manifest(db, second, verified_source=proof(second), context=context(), authorize=allow)
    db.commit()
    with pytest.raises(CatalogError) as caught:
        materialize_batch(db, second.batch_id, context=context(), authorize=allow)
    assert caught.value.code == "migration.gallery_conflict"
    db.rollback()
    assert db.scalar(select(func.count()).select_from(GalleryEntry)) == 1


def test_good_bad_effective_content_preserves_ai_and_unknown_confirmed_actor(db):
    from app.pn_catalog.content_models import ConditionContent
    part = DimPart(pn_std="CONTENT-MIGRATION")
    db.add(part)
    db.commit()
    origin = uuid4()
    records = []
    for grade, status in (("good", "ai_filled"), ("bad", "confirmed")):
        source_id = uuid4()
        payload = {"id": str(source_id), "pn_id": str(origin), "grade": grade, "title": grade,
                   "description": f"original {grade}", "desc_status": status}
        records.append(LegacyRecord(kind="content", source_id=str(source_id), source_pn_id=origin,
                                    payload=payload, payload_sha256=fingerprint(payload)))
    value = MigrationManifest(namespace="shop", snapshot_id=uuid4(), batch_id=uuid4(), source_schema="0023",
        exported_at=datetime.now(UTC), mappings=[PartMapping(source_pn_id=origin, target_part_id=part.id,
            expected_identity_revision=0, disposition="mapped", evidence="exact", decision_ref="review:1")], records=records)
    stage_manifest(db, value, verified_source=proof(value), context=context(), authorize=allow)
    result = materialize_batch(db, value.batch_id, context=context(), authorize=allow)
    db.commit()
    assert result["batch_complete"] is True and result["cutover_ready"] is False
    rows = {row.grade: row for row in db.scalars(select(ConditionContent))}
    assert rows["good"].status == "ai_filled" and rows["bad"].status == "confirmed"
    assert rows["bad"].field_provenance["description"]["confirmed_by"] is None
