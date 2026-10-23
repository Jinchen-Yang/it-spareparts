"""Isolated frozen-source PN conversion preserves origin and live editing boundaries."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.pn_catalog import api
from app.pn_catalog.attribute_models import AttributeValue
from app.pn_catalog.dto import PatchProfile
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.migration import stage_manifest
from app.pn_catalog.migration_apply import apply_source_records
from app.pn_catalog.migration_dto import LegacyRecord, fingerprint
from app.pn_catalog.migration_models import LegacySourceRecord
from app.pn_catalog.migration_state import register_snapshot
from app.pn_catalog.migration_state_dto import ApplySourceRecords
from app.pn_catalog.migration_state_models import MigrationRecordApplication
from app.pn_catalog.models import AuditEvent, PartState
from app.pn_catalog.references import bind_migration_reference
from tests.test_pn_catalog_migration import allow, manifest, proof
from tests.test_pn_catalog_migration_state import command as registration, context
from tests.test_pn_catalog_attributes import context as human_context


def prepared(db, *, target=True, payload=None, extra_records=()):
    part = DimPart(pn_std=f"MIGRATED-PROFILE-{uuid4()}")
    db.add(part)
    db.commit()
    value = manifest(target=part.id if target else None)
    origin = value.mappings[0].source_pn_id
    body = {
        "id": str(origin),
        "brand": "来源品牌",
        "summary": "历史 AI 资料",
        "fill_status": "ai_filled",
        "manual_catalog": {
            "specs": {"deleted": None, "custom": {"types": [None, True, 2, "文本"]}}
        },
        "field_sources": {"summary": {"source": "ai", "model": "old-model"}},
        "specs": {"interface": "SAS", "capacity": 16, "nullable": None},
        **(payload or {}),
    }
    record = LegacyRecord(
        kind="pn",
        source_id=str(origin),
        source_pn_id=origin,
        payload=body,
        payload_sha256=fingerprint(body),
    )
    value = value.model_copy(update={"records": [record, *extra_records]})
    stage_manifest(
        db, value, verified_source=proof(value), context=context(), authorize=allow
    )
    register_snapshot(db, registration(db, value), context=context(), authorize=allow)
    db.commit()
    ids = list(
        db.scalars(
            select(LegacySourceRecord.id).where(
                LegacySourceRecord.batch_id == value.batch_id
            )
        )
    )
    return (
        part.id,
        value,
        ApplySourceRecords(expected_snapshot_revision=1, record_ids=ids),
    )


def apply(db, value, command, ctx=None):
    return apply_source_records(
        db,
        value.namespace,
        value.snapshot_id,
        command,
        context=ctx or context(),
        authorize=allow,
    )


def test_profile_effective_ai_origin_custom_json_manual_tombstone_and_status(db):
    pid, value, cmd = prepared(db)
    result = apply(db, value, cmd)
    assert (
        result["states"] == {"applied": 1}
        and result["live_complete"]
        and not result["cutover_ready"]
    )
    db.commit()
    state = db.get(PartState, pid)
    assert state.fill_status == "ai_filled"
    assert state.field_provenance["description"]["kind"] == "ai"
    assert state.field_provenance["description"]["confirmed_by"] is None
    assert (
        state.field_provenance["description"]["historical_evidence"]["model"]
        == "old-model"
    )
    assert db.get(DimPart, pid).description == "历史 AI 资料"
    assert "description" not in db.get(DimPart, pid).locked_fields
    attrs = {r.key: r for r in db.scalars(select(AttributeValue))}
    assert attrs["capacity"].value == 16 and isinstance(attrs["capacity"].value, int)
    assert attrs["nullable"].value is None and not attrs["nullable"].deleted
    assert attrs["deleted"].deleted and attrs["deleted"].protected
    assert attrs["custom"].value == {"types": [None, True, 2, "文本"]}
    assert (
        db.scalar(
            select(ProductSpec).where(ProductSpec.spec_key == "interface")
        ).spec_value
        == "SAS"
    )
    # A later human edit may confirm the profile; importing AI never did so.
    api.patch_profile(
        db,
        pid,
        PatchProfile(
            expected_identity_revision=0,
            expected_profile_revision=state.profile_revision,
            changes={"brand": "人工确认"},
        ),
        context=human_context(),
        authorize=allow,
    )
    assert db.get(PartState, pid).fill_status == "confirmed"


def test_unresolved_reference_can_be_explicitly_bound_without_rewriting_archive(db):
    pid, value, cmd = prepared(db, target=False)
    result = apply(db, value, cmd)
    assert result["states"] == {"dependency_blocked": 1} and not result["live_complete"]
    db.commit()
    row = db.get(LegacySourceRecord, cmd.record_ids[0])
    original_mapping = dict(row.mapping)
    bind_migration_reference(
        db,
        namespace=value.namespace,
        source_id=row.source_pn_id,
        part_id=pid,
        expected_identity_revision=0,
        decision_ref="reviewed:resolve",
        context=context(),
        authorize=allow,
    )
    db.commit()
    assert apply(db, value, cmd)["states"] == {"applied": 1}
    assert row.target_part_id is None and row.mapping == original_mapping
    assert db.get(MigrationRecordApplication, row.id).revision == 2


def test_target_manual_data_is_not_overwritten_and_partial_effective_changes_rollback(
    db,
):
    pid, value, cmd = prepared(db)
    part = db.get(DimPart, pid)
    part.brand, part.locked_fields = "人工维护", ["brand"]
    db.commit()
    result = apply(db, value, cmd)
    assert result["states"] == {"conflict": 1}
    assert (
        db.get(DimPart, pid).brand == "人工维护"
        and db.get(DimPart, pid).description is None
    )
    assert not list(db.scalars(select(AttributeValue)))
    state = db.get(PartState, pid)
    assert state is None or state.fill_status is None


def test_replay_is_idempotent_rollback_is_atomic_and_snapshot_identity_checked(db):
    pid, value, cmd = prepared(db)
    ctx = context()
    apply(db, value, cmd, ctx)
    db.rollback()
    assert db.get(MigrationRecordApplication, cmd.record_ids[0]) is None
    assert db.get(DimPart, pid).description is None
    apply(db, value, cmd, ctx)
    db.commit()
    before = db.scalar(select(func.count()).select_from(AuditEvent))
    assert apply(db, value, cmd, ctx)["replayed"]
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == before
    assert apply(db, value, cmd)["states"] == {"applied": 1}
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == before
    with pytest.raises(CatalogError) as exc:
        apply_source_records(
            db, value.namespace, uuid4(), cmd, context=context(), authorize=allow
        )
    assert exc.value.code == "migration.snapshot_not_found"


def test_two_requests_apply_same_frozen_record_once(db):
    pid, value, cmd = prepared(db)
    barrier = Barrier(2)

    def run(_):
        with Session(engine) as worker:
            barrier.wait(timeout=5)
            result = apply(worker, value, cmd)
            worker.commit()
            return result["states"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, range(2))) == [{"applied": 1}, {"applied": 1}]
    assert db.get(MigrationRecordApplication, cmd.record_ids[0]).revision == 1
    assert db.get(PartState, pid).profile_revision == 1


def test_unimplemented_live_state_is_explicitly_blocked(db):
    payload = {"id": str(uuid4()), "status": "running", "lease_token": str(uuid4())}
    extra = LegacyRecord(
        kind="agent_job",
        source_id=payload["id"],
        payload=payload,
        payload_sha256=fingerprint(payload),
    )
    _, value, cmd = prepared(db, extra_records=[extra])
    result = apply(db, value, cmd)
    assert result["states"] == {"applied": 1, "dependency_blocked": 1}
    assert not result["live_complete"] and not result["cutover_ready"]


def test_migrated_manual_clear_protects_empty_target_and_does_not_confirm_migration_actor(
    db,
):
    pid, value, cmd = prepared(
        db,
        payload={
            "brand": "",
            "summary": "",
            "fill_status": "confirmed",
            "manual_catalog": {"summary": None, "specs": {"capacity": None}},
            "specs": {},
        },
    )
    assert apply(db, value, cmd)["states"] == {"applied": 1}
    state = db.get(PartState, pid)
    assert "description" in db.get(DimPart, pid).locked_fields
    assert state.field_provenance["description"]["kind"] == "manual"
    assert state.field_provenance["description"]["confirmed_by"] is None
    assert "capacity" in state.deleted_specs
    assert state.field_provenance["specs.capacity"]["confirmed_by"] is None
    assert state.fill_status == "confirmed"


def test_existing_b2_unresolved_image_binds_only_via_explicit_reference(db):
    from app.pn_catalog.media_models import GalleryEntry
    from app.pn_catalog.migration_materialize import materialize_batch

    part = DimPart(pn_std=f"MIGRATED-IMAGE-{uuid4()}")
    db.add(part)
    db.commit()
    value = manifest(status="gallery")
    payload = {
        **value.records[0].payload,
        "width": 100,
        "height": 100,
        "bytes": 1000,
        "content_type": "image/jpeg",
        "sort_order": 0,
        "is_defect": False,
        "thumbnail_status": "ready",
    }
    value = value.model_copy(
        update={
            "records": [
                value.records[0].model_copy(
                    update={"payload": payload, "payload_sha256": fingerprint(payload)}
                )
            ]
        }
    )
    stage_manifest(
        db, value, verified_source=proof(value), context=context(), authorize=allow
    )
    materialize_batch(db, value.batch_id, context=context(), authorize=allow)
    register_snapshot(db, registration(db, value), context=context(), authorize=allow)
    row = db.scalar(
        select(LegacySourceRecord).where(LegacySourceRecord.batch_id == value.batch_id)
    )
    bind_migration_reference(
        db,
        namespace=value.namespace,
        source_id=row.source_pn_id,
        part_id=part.id,
        expected_identity_revision=0,
        decision_ref="reviewed:image",
        context=context(),
        authorize=allow,
    )
    db.commit()
    cmd = ApplySourceRecords(expected_snapshot_revision=1, record_ids=[row.id])
    assert apply(db, value, cmd)["states"] == {"applied": 1}
    entry = db.scalar(select(GalleryEntry))
    assert entry.origin_part_id == part.id and row.target_part_id is None
    assert row.mapping["disposition"] == "unresolved"


def test_revoked_migration_permission_blocks_replay(db):
    _, value, cmd = prepared(db)
    ctx = context()
    apply(db, value, cmd, ctx)
    db.commit()

    def denied(*_):
        raise CatalogError("auth.forbidden", "revoked", status_code=403)

    with pytest.raises(CatalogError) as exc:
        apply_source_records(
            db, value.namespace, value.snapshot_id, cmd, context=ctx, authorize=denied
        )
    assert exc.value.code == "auth.forbidden"
