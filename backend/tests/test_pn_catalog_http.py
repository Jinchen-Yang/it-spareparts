"""HTTP contract over real PostgreSQL; injected auth port is not authz acceptance."""
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.pn_catalog.authz import CatalogAccess
from app.pn_catalog.dto import ActorContext, AuthorizationDecision
from app.pn_catalog.http import install_http


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
