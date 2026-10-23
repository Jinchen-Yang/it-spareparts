"""HTTP contract over real PostgreSQL; injected auth port is not authz acceptance."""
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.pn_catalog.authz import CatalogAccess
from app.pn_catalog.dto import ActorContext, AuthorizationDecision
from app.pn_catalog.http import install_http


def _job_access(application):
    from dataclasses import replace
    resolver = application.state.pn_catalog_access_resolver
    application.state.pn_catalog_access_resolver = lambda request, db: replace(
        resolver(request, db), authorize_job=lambda *args: AuthorizationDecision(
            frozenset({"*"}), frozenset({"*"}), {"scope": "all"}))


def test_job_http_requires_resource_authorizer_and_redacts_worker_inputs(client, catalog_app, db):
    from app.pn_catalog import jobs
    from app.pn_catalog.dto import CommandContext
    from app.pn_catalog.job_dto import JobSpec
    actor = ActorContext(principal_id="test:1", client_id="itsp", executor_id="test-http", entrypoint="itsp_ui")
    job = jobs.enqueue(db, JobSpec(kind="test", resource_type="upload", resource_id=str(uuid4()),
        resource_generation=1, input_revision=1, payload={"private": "worker-only"},
        deduplication_key=str(uuid4())), context=CommandContext(actor=actor, request_id=uuid4()))
    db.commit()
    url = f"/api/pn-catalog/v1/jobs/{job.job_id}"
    missing = client.get(url)
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "auth.unavailable"
    _job_access(catalog_app)
    value = client.get(url)
    assert value.status_code == 200, value.text
    assert not {"payload", "context", "lease_token", "lease_owner"} & value.json()["data"].keys()
    assert "worker-only" not in value.text
    base = {"expected_revision": job.revision, "expected_attempt": 0, "expected_retry_count": 0}
    key = str(uuid4())
    cancelled = client.post(url + "/cancellation", headers={"Idempotency-Key": key}, json=base)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"
    replay = client.get(f"/api/pn-catalog/v1/requests/{key}")
    assert replay.status_code == 200, replay.text
    assert replay.json()["meta"]["replayed"]
    stale = client.post(url + "/retry", headers={"Idempotency-Key": str(uuid4())}, json=base)
    assert stale.status_code == 409, stale.text
    base["expected_revision"] = cancelled.json()["data"]["revision"]
    retried = client.post(url + "/retry", headers={"Idempotency-Key": str(uuid4())}, json=base)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["retry_count"] == 1


def test_identity_http_preview_merge_and_receipt(client):
    a, _ = _create(client, "HTTP-MERGE-A")
    b, _ = _create(client, "HTTP-MERGE-B")
    command = {"operation": "merge", "source_part_id": a["part_id"], "target_part_id": b["part_id"]}
    preview = client.post("/api/pn-catalog/v1/identity/preview", json=command)
    assert preview.status_code == 200, preview.text
    command["expected"] = preview.json()["data"]["expected"]
    key = str(uuid4())
    result = client.post("/api/pn-catalog/v1/identity/commands", headers={"Idempotency-Key": key}, json=command)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["recalculation_job"]["status"] == "pending"
    receipt = client.get(f"/api/pn-catalog/v1/requests/{key}")
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["data"]["outcome"] == result.json()["data"]["outcome"]
    assert receipt.json()["meta"]["replayed"]


@pytest.fixture
def catalog_app(db):
    application = FastAPI()
    install_http(application)
    return application


@pytest.fixture
def client(catalog_app):
    # The host must authenticate and recheck permissions. This fixture exercises
    # only the public transport/domain boundary, with an explicit trusted port.
    actor = ActorContext(principal_id="test:1", client_id="itsp", executor_id="test-http",
                         entrypoint="itsp_ui")
    catalog_app.state.pn_catalog_access_resolver = lambda request, db: CatalogAccess(
        actor, lambda *args: AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"scope": "all"}),
        b"test-catalog-confirmation-secret-32-bytes")
    return TestClient(catalog_app)


def _create(client, name="HTTP-PN-1"):
    key = str(uuid4())
    response = client.post("/api/pn-catalog/v1/parts", headers={"Idempotency-Key": key},
                           json={"pn_std": name, "changes": {"description": "before"}})
    assert response.status_code == 201, response.text
    return response.json()["data"], key


def test_missing_new_framework_fails_closed_even_with_claimed_actor(catalog_app):
    client = TestClient(catalog_app)
    response = client.get("/api/pn-catalog/v1/parts/1", headers={
        "X-User": "admin", "X-Role": "admin", "Authorization": "Bearer arbitrary"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth.unavailable"
    assert response.headers["cache-control"] == "no-store"


def test_missing_routes_methods_and_invalid_ids_keep_catalog_error_contract(client):
    response = client.get("/api/pn-catalog/v1/unknown")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "request.route_not_found"
    response = client.delete("/api/pn-catalog/v1/parts/1")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "request.method_not_allowed"
    assert response.headers["cache-control"] == "no-store"
    response = client.get("/api/pn-catalog/v1/parts/0")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request.validation_failed"
    assert client.get("/another-host-path").json() == {"detail": "Not Found"}


def test_create_patch_replay_and_etag(client):
    part, _ = _create(client)
    url = f'/api/pn-catalog/v1/parts/{part["part_id"]}'
    initial = client.get(url)
    assert initial.status_code == 200
    baseline = initial.json()["data"]
    key = str(uuid4())
    headers = {"Idempotency-Key": key, "If-Match": initial.headers["etag"]}
    payload = {"expected_identity_revision": baseline["identity_revision"],
               "field_versions": {"description": baseline["field_versions"]["description"]},
               "changes": {"description": "after"}}
    first = client.patch(url + "/profile", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    replay = client.patch(url + "/profile", headers=headers, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["meta"]["replayed"] is True
    assert replay.json()["meta"]["audit_id"] == first.json()["meta"]["audit_id"]
    assert replay.headers["x-request-id"] == key
    assert replay.headers["cache-control"] == "no-store"
    stale = client.patch(url + "/profile", headers={**headers, "Idempotency-Key": str(uuid4())}, json=payload)
    assert stale.status_code == 412, stale.text
    assert stale.json()["error"]["code"] == "request.precondition_failed"


def test_missing_key_baseline_and_unknown_fields(client):
    response = client.post("/api/pn-catalog/v1/parts", json={"pn_std": "HTTP-MISSING"})
    assert response.status_code == 428, response.text
    part, _ = _create(client)
    url = f'/api/pn-catalog/v1/parts/{part["part_id"]}/profile'
    response = client.patch(url, headers={"Idempotency-Key": str(uuid4())},
                            json={"changes": {"description": "no base"}})
    assert response.status_code == 428
    response = client.patch(url, headers={"Idempotency-Key": str(uuid4())}, json={
        "expected_identity_revision": part["identity_revision"],
        "expected_profile_revision": part["profile_revision"],
        "changes": {"stock": "1"}, "actor": "admin"})
    assert response.status_code == 422
    assert "admin" not in response.text


def test_bounded_patch_without_trusted_content_length(client):
    response = client.patch("/api/pn-catalog/v1/parts/1/profile", content=b"x" * 65537)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request.payload_too_large"
    assert response.headers["cache-control"] == "no-store"


def test_conflicting_request_keys_cannot_change_receipt_identity(client):
    response = client.post("/api/pn-catalog/v1/parts", json={"pn_std": "DUPLICATE-HEADER"},
                           headers=[("Idempotency-Key", str(uuid4())),
                                    ("Idempotency-Key", str(uuid4()))])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request.validation_failed"


def test_delegation_receives_digest_of_actual_bounded_bytes(client, catalog_app):
    import hashlib
    resolver = catalog_app.state.pn_catalog_access_resolver
    seen = []
    def capturing(request, db):
        seen.append(request.state.pn_catalog_body_sha256)
        return resolver(request, db)
    catalog_app.state.pn_catalog_access_resolver = capturing
    raw = b'{ "pn_std" : "BODY-DIGEST-PN" }'
    response = client.post("/api/pn-catalog/v1/parts", content=raw,
        headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid4()),
                 "X-Body-SHA256": "spoofed"})
    assert response.status_code == 201, response.text
    assert seen == [hashlib.sha256(raw).hexdigest()]


def test_read_trace_id_preserves_delegation_binding_and_write_ids_must_match(client):
    part, _ = _create(client)
    trace_id = str(uuid4())
    response = client.get(f'/api/pn-catalog/v1/parts/{part["part_id"]}',
                          headers={"X-Request-ID": trace_id})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == response.json()["meta"]["request_id"] == trace_id
    response = client.post("/api/pn-catalog/v1/parts", json={"pn_std": "CONFLICT-IDS"},
                           headers={"X-Request-ID": trace_id, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 422


def test_availability_has_own_revision_and_keeps_identity(client):
    part, _ = _create(client)
    url = f'/api/pn-catalog/v1/parts/{part["part_id"]}'
    response = client.post(url + "/deactivation", headers={"Idempotency-Key": str(uuid4())}, json={
        "expected_identity_revision": part["identity_revision"],
        "expected_revision": part["availability_revision"], "reason": "temporary retirement"})
    assert response.status_code == 200, response.text
    current = client.get(url).json()["data"]
    assert current["availability"] == "inactive"
    assert current["identity"]["status"] == "active"
    assert current["profile_revision"] == part["profile_revision"]


def test_parts_audit_and_receipt_are_versioned_contracts(client):
    part, key = _create(client)
    listing = client.get("/api/pn-catalog/v1/parts", params={"q": "HTTP", "limit": 200})
    assert listing.status_code == 200, listing.text
    assert part["part_id"] in [p["part_id"] for p in listing.json()["data"]]
    audits = client.get(f'/api/pn-catalog/v1/parts/{part["part_id"]}/audit-events')
    assert audits.status_code == 200, audits.text
    assert len(audits.json()["data"]) == 1
    receipt = client.get(f"/api/pn-catalog/v1/requests/{key}")
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["meta"]["request_id"] == key
    assert receipt.json()["meta"]["replayed"] is True


def test_grade_content_http_receipt_and_stale_edit(client):
    part, _ = _create(client)
    url = f'/api/pn-catalog/v1/parts/{part["part_id"]}/content'
    key = str(uuid4())
    payload = {"expected_identity_revision": part["identity_revision"], "expected_revision": 0,
               "changes": {"description": "good description"}}
    saved = client.patch(url + "/good", headers={"Idempotency-Key": key}, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["status"] == "confirmed"
    assert client.get(url + "/bad").json()["data"]["revision"] == 0
    replay = client.get(f"/api/pn-catalog/v1/requests/{key}")
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["grade"] == "good"
    conflict = client.patch(url + "/good", headers={"Idempotency-Key": str(uuid4())}, json=payload)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "catalog.field_conflict"


def test_ai_candidate_http_adoption_keeps_source_and_real_confirmer(client):
    part, _ = _create(client)
    candidate = client.post("/api/pn-catalog/v1/candidates", headers={"Idempotency-Key": str(uuid4())}, json={
        "resource": "content", "part_id": part["part_id"], "grade": "bad",
        "expected_identity_revision": part["identity_revision"], "base_revision": 0,
        "changes": {"description": "proposed bad description"},
        "provenance": {"kind": "ai", "provider": "offline", "model": "fixture", "run_id": "run-1", "template_version": "v1"}})
    assert candidate.status_code == 201, candidate.text
    data = candidate.json()["data"]
    key = str(uuid4())
    adopted = client.post(f'/api/pn-catalog/v1/candidates/{data["id"]}/adoption',
        headers={"Idempotency-Key": key}, json={"expected_candidate_revision": 1,
            "expected_identity_revision": part["identity_revision"], "expected_target_revision": 0,
            "fields": ["description"]})
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["data"]["status"] == "adopted"
    receipt = client.get(f"/api/pn-catalog/v1/requests/{key}")
    assert receipt.status_code == 200 and receipt.json()["data"]["id"] == data["id"]
    effective = client.get(f'/api/pn-catalog/v1/parts/{part["part_id"]}/content/bad').json()["data"]
    provenance = effective["field_provenance"]["description"]
    assert provenance["kind"] == "ai" and provenance["confirmed_by"] == "test:1"
    listing = client.get(f'/api/pn-catalog/v1/parts/{part["part_id"]}/candidates')
    assert listing.status_code == 200, listing.text
    assert listing.json()["data"][0]["id"] == data["id"]
