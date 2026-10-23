"""Fake-provider integration: network-stage DB separation and real fenced publication."""
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import engine
from app.pn_catalog import jobs, media
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_dto import CompleteMultipart, CreateDerivative, ExpectedRevision, MultipartPart
from app.pn_catalog.media_models import Asset, Derivative, MultipartUpload, UploadTicket
from app.services.pn_media_cloud import ObjectNotFound
from app.services.pn_media_execution import MediaTransport, media_handlers
from tests.test_pn_catalog_media import add, allow, context, create_ticket, seed


class FakeStorage:
    def __init__(self):
        self.metadata = {}
        self.complete_calls = []
        self.abort_calls = []
        self.start_hook = None
        self.parts = []

    def head(self, key):
        assert engine.pool.checkedout() == 0
        if key not in self.metadata:
            raise ObjectNotFound()
        return self.metadata[key]

    def complete_multipart(self, key, upload_id, parts):
        assert engine.pool.checkedout() == 0
        self.complete_calls.append((key, upload_id, parts))
        self.metadata[key] = SimpleNamespace(size_bytes=sum(p.size_bytes for p in parts),
            request_fingerprint=self.fingerprint)

    def abort_multipart(self, key, upload_id, *, include_orphans=True):
        assert engine.pool.checkedout() == 0
        self.abort_calls.append((key, upload_id, include_orphans))

    def start_multipart(self, key, content_type, fingerprint):
        assert engine.pool.checkedout() == 0
        self.fingerprint = fingerprint
        if self.start_hook:
            self.start_hook()
        return "provider-upload-1"

    def list_parts(self, key, upload_id):
        assert engine.pool.checkedout() == 0
        return self.parts


class FakeCloud:
    def __init__(self, on_inspect=None):
        self.on_inspect, self.copies, self.thumbnails = on_inspect, [], []

    def inspect(self, key, content_type, **limits):
        assert engine.pool.checkedout() == 0
        if self.on_inspect:
            self.on_inspect()
        return SimpleNamespace(sha256="a" * 64, width=10, height=10, bytes=100,
                               content_type=content_type, source_etag="original-etag")

    def copy(self, *args, **kwargs):
        assert engine.pool.checkedout() == 0
        self.copies.append((args, kwargs))

    def thumbnail(self, **kwargs):
        assert engine.pool.checkedout() == 0
        self.thumbnails.append(kwargs)


def run(storage, cloud, *, kind=None):
    handlers = media_handlers(storage=storage, cloud=cloud, authorize=allow, session_factory=sessionmaker(engine))
    return jobs.run_once(sessionmaker(engine), worker_id="offline-media", queues=["media"],
        handlers={kind: handlers[kind]} if kind else handlers, lease_seconds=300)


def test_upload_and_thumbnail_publish_only_after_provider_stage(db):
    pid = seed(db)
    upload_id = create_ticket(db, pid, ready=False)
    media.complete_upload(db, upload_id, ExpectedRevision(expected_revision=1), context=context(), authorize=allow)
    db.commit()
    storage, cloud = FakeStorage(), FakeCloud()
    job = run(storage, cloud)
    assert job.status == "succeeded" and len(cloud.copies) == 1
    upload = db.get(UploadTicket, upload_id)
    assert upload.status == "ready"
    result = add(db, pid, upload_id)
    db.commit()
    entry_id = UUID(result["data"]["entry_id"])
    asset_id = upload.asset_id
    media.create_derivative(db, asset_id, CreateDerivative(entry_id=entry_id, expected_revision=1),
        context=context(), authorize=allow)
    db.commit()
    result = run(storage, cloud, kind="media.thumbnail")
    assert result.status == "succeeded" and len(cloud.thumbnails) == 1
    assert db.scalar(select(Derivative.status)) == "ready"
    assert cloud.thumbnails[0]["target_key"].startswith("thumb/pn/")
    assert cloud.thumbnails[0]["expected_sha256"] == "a" * 64


def test_cancel_during_provider_processing_prevents_asset_publication(db):
    upload_id = create_ticket(db, ready=False)
    media.complete_upload(db, upload_id, ExpectedRevision(expected_revision=1), context=context(), authorize=allow)
    db.commit()
    def cancel():
        with Session(engine) as session:
            current = session.get(UploadTicket, upload_id)
            media.cancel_upload(session, upload_id, ExpectedRevision(expected_revision=current.revision),
                context=context(), authorize=allow)
            session.commit()
    cloud = FakeCloud(on_inspect=cancel)
    result = run(FakeStorage(), cloud)
    assert result.status == "cancelled"
    assert len(cloud.copies) == 0  # Cancellation between inspect/copy prevents the next external call.
    assert db.scalar(select(Asset.id)) is None
    assert db.get(UploadTicket, upload_id).status == "cancelled"


def multipart(db):
    upload_id = create_ticket(db, ready=False)
    media.start_multipart(db, upload_id, ExpectedRevision(expected_revision=1), context=context(), authorize=allow)
    db.commit()
    return upload_id


def test_multipart_resume_complete_lost_response_and_verify_followup(db):
    upload_id = multipart(db)
    storage, cloud = FakeStorage(), FakeCloud()
    transport = MediaTransport(sessionmaker(engine), storage=storage, authorize=allow)
    assert transport.initialize_multipart(upload_id, context=context()) == "provider-upload-1"
    assert transport.initialize_multipart(upload_id, context=context()) == "provider-upload-1"
    actual = MultipartPart(part_number=1, etag="part-etag", size_bytes=100)
    storage.parts = [actual]
    assert transport.list_parts(upload_id, context=context()) == [actual]
    ticket = db.get(UploadTicket, upload_id)
    media.complete_multipart(db, upload_id, CompleteMultipart(expected_revision=ticket.revision, parts=[actual]),
        server_parts=[actual], context=context(), authorize=allow)
    db.commit()
    # Provider completion succeeded but its response and worker lease were lost.
    storage.metadata[f"uploads/pn-catalog/{upload_id}"] = SimpleNamespace(size_bytes=100,
        request_fingerprint=f"pn-upload:{upload_id}:1")
    result = run(storage, cloud, kind="media.complete_multipart")
    assert result.status == "succeeded" and not storage.complete_calls
    assert run(storage, cloud, kind="media.verify_upload").status == "succeeded"
    assert db.get(UploadTicket, upload_id, populate_existing=True).status == "ready"


def test_multipart_provider_object_fingerprint_mismatch_fails_closed(db):
    upload_id = multipart(db)
    storage = FakeStorage()
    MediaTransport(sessionmaker(engine), storage=storage, authorize=allow).initialize_multipart(upload_id, context=context())
    actual = MultipartPart(part_number=1, etag="part-etag", size_bytes=100)
    ticket = db.get(UploadTicket, upload_id)
    media.complete_multipart(db, upload_id, CompleteMultipart(expected_revision=ticket.revision, parts=[actual]),
        server_parts=[actual], context=context(), authorize=allow)
    db.commit()
    storage.metadata[f"uploads/pn-catalog/{upload_id}"] = SimpleNamespace(size_bytes=100, request_fingerprint="other-ticket")
    result = run(storage, FakeCloud(), kind="media.complete_multipart")
    assert result.status == "failed" and result.error_code == "media.object_conflict"
    assert not storage.complete_calls
    assert db.get(MultipartUpload, upload_id).state == "completing"


def test_cancel_during_multipart_initialize_discards_only_orphan_then_durable_abort(db):
    upload_id = multipart(db)
    storage = FakeStorage()
    def cancel():
        with Session(engine) as session:
            current = session.get(UploadTicket, upload_id)
            media.cancel_upload(session, upload_id, ExpectedRevision(expected_revision=current.revision),
                context=context(), authorize=allow)
            session.commit()
    storage.start_hook = cancel
    transport = MediaTransport(sessionmaker(engine), storage=storage, authorize=allow)
    with pytest.raises(CatalogError):
        transport.initialize_multipart(upload_id, context=context())
    assert storage.abort_calls == [(f"uploads/pn-catalog/{upload_id}", "provider-upload-1", False)]
    result = run(storage, FakeCloud(), kind="media.abort_multipart")
    assert result.status == "succeeded"
    assert storage.abort_calls[-1] == (f"uploads/pn-catalog/{upload_id}", None, True)
    assert db.get(MultipartUpload, upload_id).aborted_at is not None
