from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from app.pn_catalog import http_enrichment  # noqa: F401
from app.pn_catalog.authz import CatalogAccess
from app.pn_catalog.http import install_http
from app.pn_catalog.job_models import CatalogJob
from app.services.pn_catalog_enrichment import authorize_job
from tests.test_pn_catalog_media import context, allow, seed


def client(enabled):
    app = FastAPI()
    install_http(app)
    app.state.pn_catalog_capabilities = {"research": enabled, "recognition": enabled, "bundle": enabled}
    app.state.pn_catalog_access_resolver = lambda *_: CatalogAccess(context().actor, allow,
        authorize_job=lambda db, ctx, action, job: authorize_job(db, ctx, action, job, authorize=allow))
    return TestClient(app)


def test_http_manual_command_receipt_and_job_reads_never_enqueue_more_work(db):
    part_id = seed(db)
    http = client(True)
    key = str(uuid4())
    response = http.post(f"/api/pn-catalog/v1/parts/{part_id}/research", headers={"Idempotency-Key": key}, json={
        "expected_identity_revision": 0, "expected_profile_revision": 0,
        "expected_content_revisions": {"good": 0, "bad": 0}, "capabilities": ["text"]})
    assert response.status_code == 200, response.text
    identifier = response.json()["data"]["job_id"]
    for path in [f"jobs/{identifier}", f"requests/{key}"]:
        read = http.get("/api/pn-catalog/v1/" + path)
        assert read.status_code == 200, read.text
        assert "payload" not in read.json()["data"]
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 1


def test_disabled_capability_and_missing_baselines_do_not_enqueue(db):
    part_id = seed(db)
    body = {"expected_identity_revision": 0, "expected_profile_revision": 0,
            "expected_content_revisions": {"good": 0, "bad": 0}, "capabilities": ["text"]}
    response = client(False).post(f"/api/pn-catalog/v1/parts/{part_id}/research",
                                 headers={"Idempotency-Key": str(uuid4())}, json=body)
    assert response.status_code == 503
    body.pop("expected_profile_revision")
    response = client(True).post(f"/api/pn-catalog/v1/parts/{part_id}/research",
                                headers={"Idempotency-Key": str(uuid4())}, json=body)
    assert response.status_code == 428
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0


def test_http_bundle_accepts_uuid_selection_and_recognition_accepts_inbox_baseline(db):
    from tests.test_pn_catalog_media import create_ticket, add
    from app.pn_catalog import media
    from app.pn_catalog.media_models import InboxItem
    part_id = seed(db)
    assigned = create_ticket(db, part_id)
    entry_id = add(db, part_id, assigned)["data"]["entry_id"]
    db.commit()
    snapshot = media.bundle_snapshot(db, part_id, grade="good", context=context(), authorize=allow)
    db.commit()
    http = client(True)
    response = http.post(f"/api/pn-catalog/v1/parts/{part_id}/bundles", headers={"Idempotency-Key": str(uuid4())}, json={
        "expected_identity_revision": 0, "expected_gallery_revision": snapshot["gallery_revision"],
        "grade": "good", "entry_ids": [entry_id]})
    assert response.status_code == 200, response.text
    unassigned = create_ticket(db)
    inbox = db.scalar(select(InboxItem).where(InboxItem.upload_id == unassigned))
    identifier, revision = inbox.id, inbox.revision
    db.commit()
    response = http.post(f"/api/pn-catalog/v1/inbox/{identifier}/recognition", headers={"Idempotency-Key": str(uuid4())},
                         json={"expected_revision": revision})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["kind"] == "enrichment.recognize"
