"""Legacy uploads/inbox remain usable with canonical owners and freshly fenced work."""

from datetime import datetime, timezone, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select

from app.authz.pn_models import CatalogSubjectBinding
from app.models.system import SysUser
from app.pn_catalog import media
from app.pn_catalog.dto import ActorContext, CommandContext
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.media_dto import CompleteMultipart, MultipartPart
from app.pn_catalog.media_models import InboxItem, MultipartUpload, UploadTicket
from app.pn_catalog.migration import stage_manifest
from app.pn_catalog.migration_apply import apply_source_records
from app.pn_catalog.migration_dto import LegacyRecord, fingerprint
from app.pn_catalog.migration_models import LegacySourceRecord
from app.pn_catalog.migration_state import register_snapshot
from app.pn_catalog.migration_state_dto import ApplySourceRecords
from tests.test_pn_catalog_migration import allow, manifest, proof
from tests.test_pn_catalog_migration_state import command as registration, context
from tests.test_pn_media_execution import FakeCloud, FakeStorage, run


def owner(db):
    user = SysUser(
        username=f"migration-owner-{uuid4()}",
        password_hash="unused",
        role="readonly",
        token_version=3,
    )
    db.add(user)
    db.flush()
    subject = uuid4()
    db.add(
        CatalogSubjectBinding(
            issuer="shop",
            subject=subject,
            user_id=user.id,
            version=1,
            active=True,
            updated_by=user.id,
        )
    )
    db.commit()
    return user.id, subject


def upload(subject, identifier=None):
    identifier = identifier or uuid4()
    instant = datetime.now(timezone.utc)
    return {
        "id": str(identifier),
        "user_id": str(subject),
        "target_pn_id": None,
        "staging_key": f"legacy/staging/{identifier}",
        "filename": "photo.jpg",
        "bytes": 100,
        "content_type": "image/jpeg",
        "created_at": instant.isoformat(),
        "expires_at": (instant + timedelta(hours=1)).isoformat(),
        "consumed_image_id": None,
    }


def record(kind, payload, identifier=None):
    return LegacyRecord(
        kind=kind,
        source_id=str(identifier or payload.get("id") or payload["upload_id"]),
        payload=payload,
        payload_sha256=fingerprint(payload),
    )


def prepare(db, records, namespace="shop"):
    value = manifest().model_copy(
        update={"namespace": namespace, "mappings": [], "records": records}
    )
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
    db.commit()
    return value, ApplySourceRecords(expected_snapshot_revision=1, record_ids=ids)


def apply(db, value, cmd):
    return apply_source_records(
        db, value.namespace, value.snapshot_id, cmd, context=context(), authorize=allow
    )


def actor(uid):
    return CommandContext(
        request_id=uuid4(),
        actor=ActorContext(
            principal_id=f"account:{uid}",
            client_id="shop",
            executor_id="itsp-api",
            entrypoint="shop_ui",
        ),
    )


def multipart(source, state="uploading", leased=False):
    return {
        "upload_id": source["id"],
        "user_id": source["user_id"],
        "client_request_id": str(uuid4()),
        "storage_upload_id": "legacy-remote-upload",
        "status": state,
        "part_size": 1048576,
        "parts": [],
        "operation_token": str(uuid4()) if leased else None,
        "lease_until": None,
        "cleaned_at": None,
    }


def test_resume_legacy_multipart_uses_original_remote_fingerprint_and_new_fenced_job(
    db,
):
    uid, subject = owner(db)
    source = upload(subject)
    value, cmd = prepare(
        db, [record("upload", source), record("multipart", multipart(source))]
    )
    assert apply(db, value, cmd)["states"] == {"applied": 2}
    db.commit()
    identifier = next(db.scalars(select(UploadTicket.id)))
    ticket = db.get(UploadTicket, identifier)
    remote = db.get(MultipartUpload, identifier)
    expected = sha256(f"multipart:{identifier}".encode()).hexdigest()
    assert remote.storage_fingerprint == expected and remote.state == "uploading"
    assert remote.operation_token is not None
    projection = media.get_upload(db, identifier, context=actor(uid), authorize=allow)
    assert "storage_fingerprint" not in str(projection) and source[
        "staging_key"
    ] not in str(projection)
    part = MultipartPart(part_number=1, etag="old-part-etag", size_bytes=100)
    media.complete_multipart(
        db,
        identifier,
        CompleteMultipart(expected_revision=ticket.revision, parts=[part]),
        server_parts=[part],
        context=actor(uid),
        authorize=allow,
    )
    queued = db.scalar(select(CatalogJob))
    assert queued.payload["expected_fingerprint"] == expected
    db.commit()
    storage = FakeStorage()
    storage.metadata[source["staging_key"]] = SimpleNamespace(
        size_bytes=100, request_fingerprint=expected
    )
    # A previous remote completion is recognized without reissuing the cloud call.
    assert (
        run(storage, FakeCloud(), kind="media.complete_multipart").status == "succeeded"
    )
    assert not storage.complete_calls
    assert (
        db.get(MultipartUpload, identifier, populate_existing=True).state == "completed"
    )


def test_active_remote_lease_is_not_transferred_or_queued_and_retry_preserves_recovery(
    db,
):
    _, subject = owner(db)
    source = upload(subject)
    old = multipart(source, "completing", leased=True)
    value, cmd = prepare(db, [record("upload", source), record("multipart", old)])
    result = apply(db, value, cmd)
    assert (
        result["states"] == {"applied": 1, "recovery_required": 1}
        and not result["live_complete"]
    )
    db.commit()
    ticket = db.scalar(select(UploadTicket))
    remote = db.scalar(select(MultipartUpload))
    assert ticket.status == "failed" and remote.state == "cancelled"
    assert str(remote.operation_token) != old["operation_token"]
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0
    assert apply(db, value, cmd)["states"] == result["states"]


def test_missing_or_foreign_namespace_owner_binding_stays_blocked_without_resources(db):
    _, subject = owner(db)
    value, cmd = prepare(
        db, [record("upload", upload(subject))], namespace="foreign-shop"
    )
    result = apply(db, value, cmd)
    assert result["states"] == {"dependency_blocked": 1}
    assert db.scalar(select(func.count()).select_from(UploadTicket)) == 0


def test_ready_inbox_preserves_owner_group_capture_order_claim_and_asset(db):
    uid, subject = owner(db)
    group = uuid4()
    records = []
    for order in (5, 1):
        source = upload(subject)
        inbox_id = uuid4()
        records.append(record("upload", source))
        inbox = {
            "id": str(inbox_id),
            "upload_id": source["id"],
            "group_id": str(group),
            "client_order": order,
            "uploaded_by": str(subject),
            "source": "phone",
            "status": "ready",
            "revision": 7,
            "durable_key": f"legacy/inbox/{inbox_id}",
            "sha256": "a" * 64,
            "source_etag": "original",
            "bytes": 100,
            "content_type": "image/jpeg",
            "width": 10,
            "height": 10,
            "thumb_key": None,
            "received_at": source["created_at"],
            "claimed_by": str(subject),
            "claim_expires_at": source["expires_at"],
            "is_defect": True,
        }
        records.append(record("inbox", inbox))
    value, cmd = prepare(db, records)
    assert apply(db, value, cmd)["states"] == {"applied": 4}
    db.commit()
    items = list(db.scalars(select(InboxItem).order_by(InboxItem.position)))
    assert [item.client_order for item in items] == [1, 5]
    assert all(
        item.owner_principal_id == f"account:{uid}"
        and item.claimed_by == f"account:{uid}"
        for item in items
    )
    assert all(
        item.claim_revision == 1 and item.revision == 7 and item.asset_id
        for item in items
    )
    result = media.get_inbox(db, items[0].id, context=actor(uid), authorize=allow)
    assert result["data"]["status"] == "ready" and result["data"]["purpose"] == "defect"
    assert "legacy/inbox" not in str(result)


def test_application_rollback_removes_upload_group_asset_and_audit(db):
    _, subject = owner(db)
    source = upload(subject)
    value, cmd = prepare(db, [record("upload", source)])
    apply(db, value, cmd)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(UploadTicket)) == 0


def test_prepared_cloud_copy_intent_requires_reconciliation_and_never_restarts_job(db):
    _, subject = owner(db)
    source = upload(subject)
    intent = {
        "upload_id": source["id"],
        "image_id": str(uuid4()),
        "status": "prepared",
        "attempts": 2,
    }
    value, cmd = prepare(
        db, [record("upload", source), record("upload_intent", intent)]
    )
    result = apply(db, value, cmd)
    assert result["states"] == {"applied": 1, "recovery_required": 1}
    assert not result["live_complete"]
    assert (
        db.scalar(select(UploadTicket)).error_code
        == "migration.upload_intent_reconcile"
    )
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0


def test_source_media_dependency_cannot_overwrite_current_ticket_command(db):
    _, subject = owner(db)
    source = upload(subject)
    value, cmd = prepare(
        db, [record("upload", source), record("multipart", multipart(source))]
    )
    first = ApplySourceRecords(
        expected_snapshot_revision=1, record_ids=[cmd.record_ids[0]]
    )
    apply(db, value, first)
    db.commit()
    ticket = db.scalar(select(UploadTicket))
    ticket.status, ticket.revision = "cancelled", 2
    db.commit()
    assert apply(db, value, cmd)["states"] == {"applied": 1, "conflict": 1}
    assert db.scalar(select(UploadTicket)).revision == 2
    assert db.scalar(select(UploadTicket)).status == "cancelled"
    assert db.scalar(select(func.count()).select_from(MultipartUpload)) == 0


def test_two_media_apply_requests_preserve_one_ticket_and_one_session(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    from app.db import engine

    _, subject = owner(db)
    source = upload(subject)
    value, cmd = prepare(
        db, [record("upload", source), record("multipart", multipart(source))]
    )
    barrier = Barrier(2)

    def work(_):
        with Session(engine) as worker:
            barrier.wait(timeout=5)
            result = apply(worker, value, cmd)
            worker.commit()
            return result["states"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(work, range(2))) == [{"applied": 2}] * 2
    assert db.scalar(select(func.count()).select_from(UploadTicket)) == 1
    assert db.scalar(select(func.count()).select_from(MultipartUpload)) == 1


def test_assigned_inbox_preserves_entry_but_cannot_invent_original_assignment_receipt(
    db,
):
    from app.models.dimensions import DimPart
    from app.pn_catalog.media_models import GalleryEntry
    from app.pn_catalog.migration_state_models import MigrationRecordApplication

    _, subject = owner(db)
    part = DimPart(pn_std=f"ASSIGNED-MIGRATION-{uuid4()}")
    db.add(part)
    db.commit()
    value = manifest(target=part.id, status="gallery")
    image = value.records[0]
    image_payload = {
        **image.payload,
        "bytes": 100,
        "content_type": "image/jpeg",
        "width": 10,
        "height": 10,
        "sort_order": 0,
        "is_defect": False,
        "thumbnail_status": "ready",
    }
    image = image.model_copy(
        update={"payload": image_payload, "payload_sha256": fingerprint(image_payload)}
    )
    source = upload(subject)
    source["consumed_image_id"] = image.source_id
    source["target_pn_id"] = str(image.source_pn_id)
    ticket_record = record("upload", source).model_copy(
        update={"source_pn_id": image.source_pn_id}
    )
    inbox = {
        "id": str(uuid4()),
        "upload_id": source["id"],
        "group_id": str(uuid4()),
        "client_order": 0,
        "uploaded_by": str(subject),
        "status": "assigned",
        "revision": 4,
        "durable_key": image_payload["oss_key"],
        "sha256": "a" * 64,
        "bytes": 100,
        "content_type": "image/jpeg",
        "width": 10,
        "height": 10,
        "received_at": source["created_at"],
        "image_id": image.source_id,
        "target_pn_id": str(image.source_pn_id),
        "assignment_key": str(uuid4()),
    }
    inbox_record = record("inbox", inbox).model_copy(
        update={"source_pn_id": image.source_pn_id}
    )
    value = value.model_copy(update={"records": [image, ticket_record, inbox_record]})
    stage_manifest(
        db, value, verified_source=proof(value), context=context(), authorize=allow
    )
    register_snapshot(db, registration(db, value), context=context(), authorize=allow)
    db.commit()
    ids = list(
        db.scalars(
            select(LegacySourceRecord.id)
            .where(LegacySourceRecord.batch_id == value.batch_id)
            .order_by(LegacySourceRecord.id)
        )
    )
    result = apply(
        db, value, ApplySourceRecords(expected_snapshot_revision=1, record_ids=ids)
    )
    assert result["states"] == {"applied": 2, "recovery_required": 1}
    item = db.scalar(select(InboxItem))
    assert item.status == "assigned" and item.entry_id == db.scalar(
        select(GalleryEntry.id)
    )
    assert item.assignment_intent is None
    application = db.get(MigrationRecordApplication, ids[-1])
    assert application.reason_code == "migration.assignment_receipt_recovery"
    assert not result["live_complete"] and not result["cutover_ready"]
