"""Offline conservation prevents partial archive pages from looking like live cutover."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.pn_catalog.errors import CatalogError
from app.pn_catalog.migration import stage_manifest
from app.pn_catalog.migration_dto import LegacyRecord, fingerprint
from app.pn_catalog.migration_models import LegacySourceRecord
from app.pn_catalog.migration_state import (
    archive_inventory,
    register_snapshot,
    snapshot_report,
)
from app.pn_catalog.migration_state_dto import KindInventory, SnapshotRegistration
from app.pn_catalog.migration_state_models import MigrationSnapshotState
from app.pn_catalog.models import AuditEvent
from tests.test_pn_catalog_migration import (
    allow,
    context as legacy_context,
    manifest,
    proof,
)


def context():
    value = legacy_context()
    return value.model_copy(
        update={"actor": value.actor.model_copy(update={"client_id": "itsp"})}
    )


def staged(db):
    value = manifest()
    stage_manifest(
        db, value, verified_source=proof(value), context=context(), authorize=allow
    )
    db.commit()
    return value


def command(db, value):
    return SnapshotRegistration(
        namespace=value.namespace,
        snapshot_id=value.snapshot_id,
        source_schema=value.source_schema,
        inventory=[
            KindInventory(kind=kind, **counts)
            for kind, counts in archive_inventory(
                db, value.namespace, value.snapshot_id
            ).items()
        ],
        verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        verifier_ref="isolated-copy:second-read",
        writer_fence_ref=proof(value).writer_fence_ref,
        lease_policy="revoked",
        lease_evidence_ref="offline-workers:fenced",
    )


def test_inventory_requires_every_kind_including_empty_sources(db):
    value = staged(db)
    expected = command(db, value)
    assert len(expected.inventory) == 17
    with pytest.raises(ValidationError):
        SnapshotRegistration.model_validate(
            expected.model_dump() | {"inventory": expected.inventory[:-1]}
        )
    with pytest.raises(ValidationError):
        SnapshotRegistration.model_validate(
            expected.model_dump() | {"verification_scope": "production"}
        )


def test_registered_pending_records_never_imply_live_or_cutover_ready(db):
    value = staged(db)
    result = register_snapshot(
        db, command(db, value), context=context(), authorize=allow
    )
    assert result["record_count"] == result["pending"] == 1
    assert (
        not result["live_complete"]
        and not result["materialization_complete"]
        and not result["cutover_ready"]
    )
    db.commit()
    assert (
        snapshot_report(
            db, value.namespace, value.snapshot_id, context=context(), authorize=allow
        )["states"]
        == {}
    )


@pytest.mark.parametrize("drift", ["count", "hash", "schema", "fence"])
def test_inventory_and_source_headers_must_match_before_any_registration(db, drift):
    value = staged(db)
    expected = command(db, value)
    if drift in {"count", "hash"}:
        values = [
            item.model_copy(
                update={"count": item.count + 1}
                if drift == "count"
                else {"records_sha256": "f" * 64}
            )
            if item.kind == "image"
            else item
            for item in expected.inventory
        ]
        expected = expected.model_copy(update={"inventory": values})
    else:
        expected = expected.model_copy(
            update={
                "source_schema"
                if drift == "schema"
                else "writer_fence_ref": "different"
            }
        )
    with pytest.raises(CatalogError) as caught:
        register_snapshot(db, expected, context=context(), authorize=allow)
    assert caught.value.code == "migration.inventory_mismatch"
    assert db.scalar(select(func.count()).select_from(MigrationSnapshotState)) == 0


def test_registration_replay_and_rollback_leave_one_event_and_no_new_pages(db):
    value = staged(db)
    expected = command(db, value)
    register_snapshot(db, expected, context=context(), authorize=allow)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(MigrationSnapshotState)) == 0
    register_snapshot(db, expected, context=context(), authorize=allow)
    db.commit()
    replay = register_snapshot(db, expected, context=context(), authorize=allow)
    assert replay["replayed"]
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "migration.snapshot_registered")
        )
        == 1
    )
    db.commit()
    payload = {"id": str(uuid4()), "event": "must not append"}
    new = value.model_copy(
        update={
            "batch_id": uuid4(),
            "records": [
                LegacyRecord(
                    kind="audit",
                    source_id=payload["id"],
                    payload=payload,
                    payload_sha256=fingerprint(payload),
                )
            ],
        }
    )
    with pytest.raises(CatalogError) as caught:
        stage_manifest(
            db, new, verified_source=proof(new), context=context(), authorize=allow
        )
    assert caught.value.code == "migration.snapshot_frozen"
    assert db.scalar(select(func.count()).select_from(LegacySourceRecord)) == 1


def test_snapshot_evidence_immutable_and_current_permissions_before_replay(db):
    value = staged(db)
    expected = command(db, value)
    ctx = context()
    register_snapshot(db, expected, context=ctx, authorize=allow)
    db.commit()
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE pn_catalog_migration_snapshot SET fence_evidence='{}'"))
    db.rollback()

    def deny(*args):
        raise CatalogError("auth.forbidden", "revoked", status_code=403)

    with pytest.raises(CatalogError) as caught:
        register_snapshot(db, expected, context=ctx, authorize=deny)
    assert caught.value.code == "auth.forbidden"
