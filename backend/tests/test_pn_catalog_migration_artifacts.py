"""Historical results remain usable only under current gallery/inbox access."""

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
import pytest
from sqlalchemy import select
from app.models.dimensions import DimPart
from app.pn_catalog import media
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_models import GalleryEntry, InboxItem
from app.pn_catalog.migration import stage_manifest
from app.pn_catalog.migration_artifacts import bundle_downloads
from app.pn_catalog.migration_dto import fingerprint
from app.pn_catalog.migration_models import LegacySourceRecord
from app.pn_catalog.migration_state import register_snapshot
from tests.test_pn_catalog_migration_media_state import (
    owner,
    record,
    prepare,
    apply,
    actor,
    upload,
)
from tests.test_pn_catalog_migration import allow, manifest, proof
from tests.test_pn_catalog_migration_state import context, command as registration
from app.pn_catalog.migration_state_dto import ApplySourceRecords


def test_legacy_recognition_hints_preserve_candidates_without_old_match_details(db):
    uid, subject = owner(db)
    source = upload(subject)
    identifier = uuid4()
    inbox = {
        "id": str(identifier),
        "upload_id": source["id"],
        "group_id": str(uuid4()),
        "client_order": 2147483647,
        "uploaded_by": str(subject),
        "status": "ready",
        "revision": 1,
        "durable_key": f"legacy/inbox/{identifier}",
        "sha256": "a" * 64,
        "bytes": 100,
        "width": 10,
        "height": 10,
        "content_type": "image/jpeg",
        "received_at": source["created_at"],
        "recognition_status": "done",
        "candidates": [
            {
                "raw_pn": "OLD-PN-42",
                "pn_norm": "OLDPN42",
                "confidence": 0.93,
                "pn": {"id": "old-hidden", "secret": "private match"},
                "item_category": "硬盘",
                "category_confidence": 0.9,
            }
        ],
    }
    value, cmd = prepare(db, [record("upload", source), record("inbox", inbox)])
    assert apply(db, value, cmd)["states"] == {"applied": 2}
    result = media.get_inbox(db, identifier, context=actor(uid), authorize=allow)
    assert result["data"]["client_order"] == 2147483647
    from app.pn_catalog.media_dto import AddInboxGroupItem
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AddInboxGroupItem(
            upload_id=uuid4(),
            position=0,
            expected_group_revision=1,
            client_order=2147483647,
        )
    recognition = result["data"]["recognition"]
    assert recognition["result"]["candidates"] == [
        {"pn": "OLD-PN-42", "confidence": 0.93}
    ]
    assert recognition["result"]["item_category"] == "硬盘"
    assert recognition["result"]["provider"] == "legacy"
    assert "private match" not in str(result) and "legacy/inbox" not in str(result)
    db.get(InboxItem, identifier).generation += 1
    db.flush()
    assert (
        media.get_inbox(db, identifier, context=actor(uid), authorize=allow)["data"][
            "recognition"
        ]
        is None
    )


def bundle_fixture(db, *, expired=False):
    uid, subject = owner(db)
    part = DimPart(pn_std=f"MIGRATE-BUNDLE-{uuid4()}")
    db.add(part)
    db.commit()
    value = manifest(target=part.id, status="gallery")
    image = value.records[0]
    image_payload = {
        **image.payload,
        "bytes": 100,
        "content_type": "image/jpeg",
        "sort_order": 0,
        "is_defect": False,
        "thumbnail_status": "ready",
        "filename": "old.jpg",
        "width": 10,
        "height": 10,
    }
    image = image.model_copy(
        update={"payload": image_payload, "payload_sha256": fingerprint(image_payload)}
    )
    bundle_id, job_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc) - (timedelta(days=2) if expired else timedelta())
    bundle = {
        "id": str(bundle_id),
        "job_id": str(job_id),
        "user_id": str(subject),
        "pn_id": str(image.source_pn_id),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "grade": "good",
        "images": [
            {
                "image_id": image.source_id,
                "oss_key": image_payload["oss_key"],
                "sha256": "a" * 64,
                "bytes": 100,
                "content_type": "image/jpeg",
                "is_defect": False,
            }
        ],
        "archives": [
            {
                "part": 1,
                "filename": "old-images.zip",
                "image_count": 1,
                "bytes": 150,
                "oss_key": f"bundle/{bundle_id}/part-1.zip",
                "sha256": "b" * 64,
            }
        ],
    }
    job = {
        "id": str(job_id),
        "status": "done",
        "kind": "bundle",
        "pn_id": str(image.source_pn_id),
    }
    records = [
        image,
        *(
            record(kind, payload).model_copy(
                update={"source_pn_id": image.source_pn_id}
            )
            for kind, payload in [("agent_job", job), ("bundle", bundle)]
        ),
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
    assert apply(
        db, value, ApplySourceRecords(expected_snapshot_revision=1, record_ids=ids)
    )["live_complete"]
    return uid, ids[-1], image.source_id


def test_legacy_bundle_current_owner_visibility_and_no_private_keys(db):
    uid, record_id, image_id = bundle_fixture(db)
    signed = []

    def sign(key, **kwargs):
        signed.append((key, kwargs))
        return "https://signed.invalid/opaque"

    storage = SimpleNamespace(signed_get=sign)
    result = bundle_downloads(
        db, record_id, context=actor(uid), authorize=allow, storage=storage
    )
    assert result["data"]["archives"][0]["url"] == "https://signed.invalid/opaque"
    assert "oss_key" not in str(result) and len(signed) == 1
    with pytest.raises(CatalogError) as denied:
        bundle_downloads(
            db, record_id, context=actor(uid + 100), authorize=allow, storage=storage
        )
    assert denied.value.status_code == 403 and len(signed) == 1
    db.get(GalleryEntry, UUID(image_id)).status = "deleted"
    db.flush()
    with pytest.raises(CatalogError) as hidden:
        bundle_downloads(
            db, record_id, context=actor(uid), authorize=allow, storage=storage
        )
    assert hidden.value.status_code == 404 and len(signed) == 1


def test_legacy_bundle_expiry_never_extended_by_migration(db):
    uid, record_id, _ = bundle_fixture(db, expired=True)
    with pytest.raises(CatalogError) as failure:
        bundle_downloads(
            db,
            record_id,
            context=actor(uid),
            authorize=allow,
            storage=SimpleNamespace(
                signed_get=lambda *_a, **_k: pytest.fail("expired file signed")
            ),
        )
    assert failure.value.code == "enrichment.bundle_expired"
