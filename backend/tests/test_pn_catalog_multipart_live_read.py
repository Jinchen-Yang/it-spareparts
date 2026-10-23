"""Resume sees actual OSS parts; revocation/cancellation during I/O never publishes them."""
from uuid import UUID

import pytest
from sqlalchemy import select

from app.pn_catalog.media_models import UploadTicket
from app.services.pn_media_cloud import MultipartPart
from tests.test_pn_catalog_media_http import media_client, post, upload, PREFIX  # noqa: F401


def started(client):
    ticket = upload(client)
    result = post(client, f"/uploads/{ticket['upload_id']}/multipart", {"expected_revision": ticket["revision"]})
    assert result.status_code == 201, result.text
    return ticket["upload_id"]


def test_actual_parts_do_not_mutate_database(media_client, db):  # noqa: F811
    identifier = started(media_client)
    media_client.app.state.pn_media_storage.parts = [MultipartPart(1, "actual", 100)]
    before = db.scalar(select(UploadTicket.revision).where(UploadTicket.id == UUID(identifier)))
    result = media_client.get(PREFIX + f"/uploads/{identifier}/multipart/parts")
    assert result.status_code == 200, result.text
    assert result.json()["data"] == [{"part_number": 1, "etag": "actual", "size_bytes": 100}]
    db.expire_all()
    assert db.scalar(select(UploadTicket.revision).where(UploadTicket.id == UUID(identifier))) == before
    cached = media_client.get(PREFIX + f"/uploads/{identifier}/multipart").json()
    assert cached["data"]["parts"] == []


def test_cancel_during_provider_read_fails_closed(media_client, db):  # noqa: F811
    identifier = started(media_client)
    storage = media_client.app.state.pn_media_storage
    def listing(*_):
        # This commit uses an independent connection; a held domain row lock would timeout.
        from app.db import SessionLocal
        with SessionLocal() as other:
            ticket = other.get(UploadTicket, UUID(identifier))
            ticket.status = "cancelled"
            ticket.generation += 1
            other.commit()
        return [MultipartPart(1, "actual", 100)]
    storage.list_parts = listing
    result = media_client.get(PREFIX + f"/uploads/{identifier}/multipart/parts")
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "media.deleted"
    assert "actual" not in result.text


@pytest.mark.parametrize("parts", [[MultipartPart(1,"e",100)] * 2, [MultipartPart(1,"e",99)]])
def test_storage_invalid_parts_rejected(media_client, parts):  # noqa: F811
    identifier = started(media_client)
    media_client.app.state.pn_media_storage.parts = parts
    result = media_client.get(PREFIX + f"/uploads/{identifier}/multipart/parts")
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "media.invalid_part"


def test_upload_inbox_exact_lookup_without_group_scan(media_client, db):  # noqa: F811
    from tests.test_pn_catalog_media_http import verify
    ticket = upload(media_client)
    identifier = ticket["upload_id"]
    assert media_client.get(PREFIX + f"/uploads/{identifier}/inbox").status_code == 404
    verify(db, media_client, ticket)
    response = media_client.get(PREFIX + f"/uploads/{identifier}/inbox")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["upload_id"] == identifier
