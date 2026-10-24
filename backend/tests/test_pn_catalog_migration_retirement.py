from uuid import UUID, uuid4
import pytest
from sqlalchemy import func, select
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_models import (
    UploadTicket,
    MultipartUpload,
    InboxItem,
    GalleryEntry,
)
from app.pn_catalog.migration_state_models import (
    MigrationRecoveryDecision,
    MigrationRecordApplication,
)
from app.authz.pn_models import CatalogSubjectBinding
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.migration_dto import fingerprint
from app.models.dimensions import DimPart
from app.pn_catalog.migration_recovery import recover_record
from app.pn_catalog.migration_recovery_dto import RetireLegacyWork
from app.pn_catalog.migration_models import LegacySourceRecord
from app.pn_catalog.migration_state_dto import ApplySourceRecords
from app.pn_catalog.migration import stage_manifest
from app.pn_catalog.migration_state import register_snapshot
from tests.test_pn_catalog_migration_media_state import (
    owner,
    upload,
    multipart,
    record,
    apply,
)
from tests.test_pn_catalog_migration_state import context, command as registration
from tests.test_pn_catalog_migration import allow, manifest, proof


def uncertain_pair(db):
    _, subject = owner(db)
    part = DimPart(pn_std=f"RECOVERY-PROBE-{uuid4()}")
    db.add(part)
    db.commit()
    value = manifest(target=part.id, status="gallery")
    source_pn = value.records[0].source_pn_id
    source = upload(subject)
    source["target_pn_id"] = str(source_pn)
    source["expires_at"] = source["created_at"]
    inbox_id = uuid4()
    inbox = {
        "id": str(inbox_id),
        "upload_id": source["id"],
        "group_id": str(uuid4()),
        "uploaded_by": str(subject),
        "status": "assigning",
        "revision": 2,
        "client_order": 0,
        "target_pn_id": str(source_pn),
        "durable_key": f"img/inbox/{inbox_id}/original.jpg",
        "sha256": "a" * 64,
        "bytes": 100,
        "width": 10,
        "height": 10,
        "content_type": "image/jpeg",
        "received_at": source["created_at"],
        "assignment_key": str(uuid4()),
    }
    # Legacy resumable.cancel(expired=True) keeps completed for a received inbox,
    # and commits operation_token before abort/delete_staging external calls.
    records = [
        record("upload", source).model_copy(update={"source_pn_id": source_pn}),
        record("multipart", multipart(source, state="completed", leased=True)),
        record("inbox", inbox).model_copy(update={"source_pn_id": source_pn}),
    ]
    value = value.model_copy(update={"records": records})
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
    command = ApplySourceRecords(expected_snapshot_revision=1, record_ids=ids)
    assert apply(db, value, command)["states"] == {"applied": 1, "recovery_required": 2}
    db.commit()
    selected = {
        db.get(LegacySourceRecord, identifier).kind: db.get(
            LegacySourceRecord, identifier
        )
        for identifier in ids
    }
    return selected


def decision(row, **changes):
    return RetireLegacyWork(
        decision="retire_fenced",
        expected_revision=1,
        source_sha256=row.payload_sha256,
        evidence_ref="isolated-review:verified-retired",
        reason="核对同一上传旧外部结果",
        external_outcome="retained_effect_reviewed",
        **changes,
    )


@pytest.mark.parametrize("first_kind", ["multipart", "inbox"])
def test_two_uncertain_records_for_same_upload_can_both_retire(db, first_kind):
    selected = uncertain_pair(db)
    for kind in [first_kind, "inbox" if first_kind == "multipart" else "multipart"]:
        row = selected[kind]
        result = recover_record(
            db, row.id, decision(row), context=context(), authorize=allow
        )
        db.commit()
        assert result["data"]["state"] == "historical_terminal"
    assert db.scalar(select(func.count()).select_from(MigrationRecoveryDecision)) == 2
    assert db.scalar(select(UploadTicket)).revision == 2
    assert db.scalar(select(MultipartUpload)).revision == 2
    assert db.scalar(select(InboxItem)).revision == 3
    second = selected["inbox" if first_kind == "multipart" else "multipart"]
    evidence = db.get(MigrationRecordApplication, second.id).target_ref
    assert (
        evidence["compatible_retirement"]["source_record_id"] == selected[first_kind].id
    )
    assert "staging_key" not in str(evidence) and "operation_token" not in str(evidence)


@pytest.mark.parametrize(
    "changed",
    [
        "ticket",
        "multipart",
        "job",
        "mapping_version",
        "mapping_disabled",
        "ledger_only",
    ],
)
def test_only_immutable_audited_retirement_can_be_reused(db, changed):
    selected = uncertain_pair(db)
    first = selected["multipart"]
    recover_record(db, first.id, decision(first), context=context(), authorize=allow)
    db.commit()
    ticket, child = db.scalar(select(UploadTicket)), db.scalar(select(MultipartUpload))
    if changed == "ticket":
        ticket.revision += 1
    elif changed == "multipart":
        child.operation_token = uuid4()
        child.revision += 1
    elif changed == "job":
        ticket.job_id = uuid4()
    elif changed == "mapping_version":
        db.scalar(select(CatalogSubjectBinding)).version += 1
    elif changed == "mapping_disabled":
        binding = db.scalar(select(CatalogSubjectBinding))
        binding.active = False
        binding.version += 1
    else:
        # A mutable ledger summary alone is not proof of a recovery decision.
        from app.pn_catalog.migration_retirement import _digest

        ticket.revision += 1
        application = db.get(MigrationRecordApplication, first.id)
        application.target_ref = {
            **application.target_ref,
            "retired_upload": {
                **application.target_ref["retired_upload"],
                "state_sha256": _digest(ticket, child),
            },
        }
        application.revision += 1
    db.commit()
    second = selected["inbox"]
    with pytest.raises(CatalogError) as error:
        recover_record(
            db, second.id, decision(second), context=context(), authorize=allow
        )
    assert error.value.code in {
        "migration.target_changed",
        "migration.owner_binding_required",
    }
    db.rollback()
    assert db.get(MigrationRecordApplication, second.id).state == "recovery_required"
    assert db.scalar(select(func.count()).select_from(MigrationRecoveryDecision)) == 1
    assert db.scalar(select(InboxItem)).revision == 2


def test_committed_image_survives_reviewed_expired_multipart_cleanup(db):
    _, subject = owner(db)
    part = DimPart(pn_std=f"RETAIN-COMMITTED-{uuid4()}")
    db.add(part)
    db.commit()
    value = manifest(target=part.id, status="gallery")
    image = value.records[0]
    payload = {
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
        update={"payload": payload, "payload_sha256": fingerprint(payload)}
    )
    source = upload(subject)
    source.update(
        target_pn_id=str(image.source_pn_id),
        consumed_image_id=image.source_id,
        expires_at=source["created_at"],
    )
    records = [
        image,
        record("upload", source).model_copy(
            update={"source_pn_id": image.source_pn_id}
        ),
        record("multipart", multipart(source, state="completed", leased=True)),
    ]
    value = value.model_copy(update={"records": records})
    stage_manifest(
        db, value, verified_source=proof(value), context=context(), authorize=allow
    )
    register_snapshot(db, registration(db, value), context=context(), authorize=allow)
    db.commit()
    ids = list(
        db.scalars(select(LegacySourceRecord.id).order_by(LegacySourceRecord.id))
    )
    assert apply(
        db, value, ApplySourceRecords(expected_snapshot_revision=1, record_ids=ids)
    )["states"] == {"applied": 2, "recovery_required": 1}
    db.commit()
    row = db.get(LegacySourceRecord, ids[-1])
    rejected = decision(row).model_copy(
        update={"external_outcome": "no_external_effect"}
    )
    with pytest.raises(CatalogError) as error:
        recover_record(db, row.id, rejected, context=context(), authorize=allow)
    assert error.value.code == "migration.target_changed"
    db.rollback()
    assert (
        recover_record(db, row.id, decision(row), context=context(), authorize=allow)[
            "data"
        ]["state"]
        == "historical_terminal"
    )
    ticket = db.scalar(select(UploadTicket))
    assert ticket.status == "committed" and ticket.revision == 1
    assert ticket.entry_id == UUID(image.source_id)
    assert db.get(GalleryEntry, ticket.entry_id).status == "gallery"
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0
    assert db.scalar(select(MultipartUpload)).revision == 2
    db.rollback()
    assert db.get(MigrationRecordApplication, row.id).state == "recovery_required"
    assert db.scalar(select(MultipartUpload)).revision == 1


def test_related_recoveries_serialize_without_losing_either_decision(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session

    rows = uncertain_pair(db)
    commands = [
        (rows[kind].id, decision(rows[kind])) for kind in ("inbox", "multipart")
    ]
    db.commit()
    bind, barrier = db.get_bind(), Barrier(2)

    def submit(item):
        identifier, command = item
        with Session(bind) as session:
            barrier.wait(timeout=5)
            result = recover_record(
                session, identifier, command, context=context(), authorize=allow
            )
            session.commit()
            return result["data"]["state"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, commands)) == ["historical_terminal"] * 2
    assert db.scalar(select(func.count()).select_from(MigrationRecoveryDecision)) == 2
    assert db.scalar(select(UploadTicket)).revision == 2
    assert db.scalar(select(MultipartUpload)).revision == 2
