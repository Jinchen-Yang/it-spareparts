"""Real HTTP + PostgreSQL media contract with explicit local provider doubles."""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.dimensions import DimPart
from app.pn_catalog import jobs, media
from app.pn_catalog.authz import CatalogAccess
from app.pn_catalog.dto import ActorContext, AuthorizationDecision
from app.pn_catalog.http import install_http
from app.pn_catalog import http_media  # noqa: F401 - register standalone test routes
from app.pn_catalog.media_models import UploadTicket
from app.pn_catalog.media_ports import VerifiedImage
from app.services.pn_media_cloud import MultipartPart, PartSignature, UploadForm

PREFIX = "/api/pn-catalog/v1"


def allow(*_):
    return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"scope": "all"})


class Storage:
    def __init__(self):
        self.signed = 0
        self.started = 0
        self.listed = 0
        self.parts = []

    def sign_upload(self, **plan):
        self.signed += 1
        return UploadForm(
            "POST",
            f"https://storage.invalid/{self.signed}",
            {"key": plan["key"]},
            {},
            plan["expires_at"],
        )

    def signed_get(self, key, expires_in=300):
        self.signed += 1
        return f"https://storage.invalid/read/{self.signed}"

    def start_multipart(self, key, content_type, fingerprint):
        self.started += 1
        return "provider-upload-id"

    def list_parts(self, key, upload_id):
        self.listed += 1
        assert upload_id == "provider-upload-id"
        return list(self.parts)

    def sign_part(self, **plan):
        return PartSignature("https://storage.invalid/part", {}, plan["expires_at"])


@pytest.fixture
def media_client(db):
    app = FastAPI()
    install_http(app)
    actor = ActorContext(
        principal_id="http-media:1",
        client_id="shop",
        executor_id="http-test",
        entrypoint="shop_ui",
    )
    app.state.pn_catalog_access_resolver = lambda *_: CatalogAccess(
        actor, allow, b"test-secret-32-bytes-for-catalog"
    )
    app.state.pn_media_storage = Storage()
    return TestClient(app)


def post(client, path, body, key=None):
    return client.post(
        PREFIX + path, json=body, headers={"Idempotency-Key": key or str(uuid4())}
    )


def upload(client, part_id=None):
    body = {
        "client_request_id": str(uuid4()),
        "filename": "a.jpg",
        "size_bytes": 100,
        "content_type": "image/jpeg",
    }
    if part_id:
        body.update(part_id=part_id, expected_identity_revision=0)
    response = post(client, "/uploads", body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def verify(db, client, ticket):
    identifier = UUID(ticket["upload_id"])
    complete = post(
        client,
        f"/uploads/{identifier}/completion",
        {"expected_revision": ticket["revision"]},
    )
    assert complete.status_code == 202, complete.text
    lease = jobs.claim(
        db,
        worker_id="http-test-worker",
        queues=["media"],
        kinds=["media.verify_upload"],
    )
    db.commit()
    image = VerifiedImage(
        "f" * 64,
        10,
        10,
        100,
        "image/jpeg",
        "etag",
        f"img/pn-catalog/assets/{identifier}/original",
    )
    jobs.complete(
        db,
        lease,
        verify_resource=lambda session, current: media.verify_upload_resource(
            session, current, authorize=allow
        ),
        publish=lambda session, current: media.publish_verified_upload(
            session, current, image
        ),
    )
    db.commit()


def test_upload_json_uuid_strict_body_fresh_transport_and_receipt(media_client, db):
    client = media_client
    body = {
        "client_request_id": str(uuid4()),
        "filename": "a.jpg",
        "size_bytes": 100,
        "content_type": "image/jpeg",
    }
    key = str(uuid4())
    first = post(client, "/uploads", body, key)
    assert first.status_code == 201, first.text
    replay = post(client, "/uploads", body, key)
    assert replay.status_code == 201, replay.text
    assert replay.json()["meta"]["replayed"]
    assert (
        replay.json()["data"]["upload_form"]["url"]
        != first.json()["data"]["upload_form"]["url"]
    )
    receipt = client.get(PREFIX + f"/requests/{key}")
    assert receipt.status_code == 200, receipt.text
    assert "upload_form" not in receipt.json()["data"]
    invalid = post(client, "/uploads", {**body, "size_bytes": "100"})
    assert invalid.status_code == 422
    forged = post(client, "/uploads", {**body, "owner_principal_id": "admin"})
    assert forged.status_code == 422
    assert db.scalar(select(func.count()).select_from(UploadTicket)) == 1


def test_missing_provider_fails_closed_before_ticket_mutation(media_client, db):
    del media_client.app.state.pn_media_storage
    response = post(
        media_client,
        "/uploads",
        {
            "client_request_id": str(uuid4()),
            "filename": "a.jpg",
            "size_bytes": 100,
            "content_type": "image/jpeg",
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "media.provider_unavailable"
    assert db.scalar(select(func.count()).select_from(UploadTicket)) == 0


def test_gallery_http_archive_delete_restore_and_typed_receipt(media_client, db):
    client = media_client
    part = DimPart(pn_std="HTTP-MEDIA")
    db.add(part)
    db.commit()
    pid = part.id
    db.commit()
    ticket = upload(client, pid)
    verify(db, client, ticket)
    added = post(
        client,
        f"/parts/{pid}/gallery/entries",
        {"expected_identity_revision": 0, "upload_id": ticket["upload_id"]},
    )
    assert added.status_code == 201, added.text
    eid = added.json()["data"]["entry_id"]
    gallery = client.get(PREFIX + f"/parts/{pid}/gallery")
    assert gallery.status_code == 200, gallery.text
    assert [row["entry_id"] for row in gallery.json()["data"]["entries"]] == [eid]
    missing_base = client.delete(
        PREFIX + f"/gallery/entries/{eid}", headers={"Idempotency-Key": str(uuid4())}
    )
    assert missing_base.status_code == 428
    key = str(uuid4())
    deleted = client.delete(
        PREFIX + f"/gallery/entries/{eid}?expected_revision=1",
        headers={"Idempotency-Key": key},
    )
    assert deleted.status_code == 200, deleted.text
    assert client.get(PREFIX + f"/requests/{key}").json()["data"]["status"] == "deleted"
    restore = post(
        client,
        f"/gallery/entries/{eid}/restoration",
        {"expected_deleted_revision": 2, "reason": "恢复"},
    )
    assert restore.status_code == 200, restore.text
    links = client.get(PREFIX + f"/gallery/entries/{eid}/links")
    assert links.status_code == 200, links.text
    assert links.headers["cache-control"] == "no-store"


def test_multipart_http_verifies_server_parts_and_replays_without_provider(
    media_client,
):
    client = media_client
    ticket = upload(client)
    path = f"/uploads/{ticket['upload_id']}/multipart"
    key = str(uuid4())
    started = post(client, path, {"expected_revision": 1}, key)
    assert started.status_code == 201, started.text
    assert started.json()["data"]["multipart"]["state"] == "uploading"
    again = post(client, path, {"expected_revision": 1}, key)
    assert again.status_code == 201, again.text
    assert client.app.state.pn_media_storage.started == 1
    signed = post(
        client, path + "/part-urls", {"expected_revision": 3, "part_numbers": [1]}
    )
    assert signed.status_code == 200, signed.text
    assert (
        post(
            client, path + "/part-urls", {"expected_revision": 3, "part_numbers": [2]}
        ).status_code
        == 422
    )
    storage = client.app.state.pn_media_storage
    storage.parts = [MultipartPart(1, "actual", 100)]
    partkey = str(uuid4())
    part = client.put(
        PREFIX + path + "/parts/1",
        json={"expected_revision": 3, "etag": "forged", "size_bytes": 100},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert part.status_code == 422, part.text
    body = {"expected_revision": 3, "etag": "actual", "size_bytes": 100}
    part = client.put(
        PREFIX + path + "/parts/1", json=body, headers={"Idempotency-Key": partkey}
    )
    assert part.status_code == 200, part.text
    completebody = {
        "expected_revision": 4,
        "parts": [{"part_number": 1, "etag": "actual", "size_bytes": 100}],
    }
    completekey = str(uuid4())
    complete = post(client, path + "/completion", completebody, completekey)
    assert complete.status_code == 202, complete.text
    before = storage.listed
    storage.parts = []  # OSS may no longer expose parts after its successful complete.
    replay = client.put(
        PREFIX + path + "/parts/1", json=body, headers={"Idempotency-Key": partkey}
    )
    assert replay.status_code == 200, replay.text
    assert (
        post(client, path + "/completion", completebody, completekey).status_code == 202
    )
    assert storage.listed == before


def test_inbox_http_claim_assignment_and_group_json_uuid(media_client, db):
    client = media_client
    part = DimPart(pn_std="HTTP-INBOX")
    db.add(part)
    db.commit()
    pid = part.id
    db.commit()
    ticket = upload(client)
    group = post(
        client,
        "/inbox-groups",
        {"client_group_id": str(uuid4()), "capture_mode": "same_item"},
    )
    assert group.status_code == 201, group.text
    gid = group.json()["data"]["group_id"]
    add = post(
        client,
        f"/inbox-groups/{gid}/items",
        {"upload_id": ticket["upload_id"], "position": 0, "expected_group_revision": 1},
    )
    assert add.status_code == 201, add.text
    assert client.get(PREFIX + "/inbox-items?scope=shared").json()["data"] == []
    verify(db, client, ticket)
    items = client.get(PREFIX + "/inbox-items?scope=shared")
    assert items.status_code == 200, items.text
    row = items.json()["data"][0]
    claim = post(
        client,
        f"/inbox-items/{row['inbox_id']}/claim",
        {"expected_revision": row["revision"]},
    )
    assert claim.status_code == 200, claim.text
    assign = post(
        client,
        f"/inbox-items/{row['inbox_id']}/assignment",
        {
            "claim_revision": claim.json()["data"]["claim_revision"],
            "part_id": pid,
            "expected_identity_revision": 0,
            "assignment_key": str(uuid4()),
        },
    )
    assert assign.status_code == 200, assign.text
    assert assign.json()["data"]["status"] == "assigned"
