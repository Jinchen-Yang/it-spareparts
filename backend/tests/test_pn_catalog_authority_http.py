"""Native and delegated HTTP requests use actual PostgreSQL + Cerbos authority."""
# ruff: noqa: F811 -- imported pytest fixtures are resolved by argument name.

import hashlib
import json
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.auth import _make_token
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.models.dimensions import DimPart
from app.pn_catalog.http import install_http
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.models import AuditEvent
from app.services import pn_catalog_access as access
from tests.test_pn_catalog_access import authority, proof  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def http(authority):  # noqa: F811
    app = FastAPI()
    install_http(app)
    app.state.pn_catalog_access_resolver = access.resolve_catalog_access
    app.state.pn_catalog_capabilities = {
        "media": True,
        "research": True,
        "recognition": True,
        "bundle": True,
    }
    return TestClient(app)


def native(user):
    token, _ = _make_token(
        user.role,
        user.username,
        user.display_name,
        token_version=user.token_version,
        authn="sys_user",
    )
    return {"Authorization": "Bearer " + token}


def seed(db):
    part = DimPart(pn_std="AUTH-HTTP-1", description="before")
    db.add(part)
    db.commit()
    return part.id


def exchange(http, subject, method, target, body=b"", *, key=None, if_match=None):
    key = key or uuid4()
    bound = access.RequestBinding(
        method=method,
        target=target,
        body_sha256=hashlib.sha256(body).hexdigest(),
        request_id=key,
        if_match=if_match,
    )
    signed = proof(subject, bound)
    response = http.post("/api/pn-catalog/v1/auth/delegations", json={"proof": signed})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert set(result) == {"token", "expires_at", "token_type"}
    headers = {
        "Authorization": "PN-Delegation " + result["token"],
        "X-Request-ID": str(key),
    }
    if method != "GET":
        headers["Idempotency-Key"] = str(key)
    if if_match is not None:
        headers["If-Match"] = if_match
    return headers, signed


def test_legacy_route_catalog_guard_error_preserves_status_and_safe_envelope():
    app = FastAPI()
    install_http(app)

    @app.post("/api/legacy-test")
    def legacy():
        raise CatalogError("auth.forbidden", "当前主数据权限不足", status_code=403)

    response = TestClient(app).post("/api/legacy-test")
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "auth.forbidden"
    assert response.json()["error"]["request_id"]


def test_native_http_access_has_current_explicit_fields_and_no_migration_controls(
    db, authority, http
):
    pid = seed(db)
    response = http.get(
        f"/api/pn-catalog/v1/parts/{pid}/access", headers=native(authority[0])
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert {"update_profile", "update_content", "gallery.archive"} <= set(
        result["allowed_actions"]
    )
    assert "description" in result["writable_fields"]
    assert "content.bad.description" in result["readable_fields"]
    assert not access.MIGRATION_ACTIONS & set(result["allowed_actions"])
    assert "legacy_evidence" not in result["readable_fields"]
    row = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == authority[0].id,
            SysAuthorizationGrant.resource == "part_master",
        )
    )
    row.actions = ["read"]
    row.field_permissions = ["description"]
    row.version += 1
    db.commit()
    result = http.get(
        f"/api/pn-catalog/v1/parts/{pid}/access", headers=native(authority[0])
    ).json()["data"]
    assert (
        "description" in result["readable_fields"]
        and "description" not in result["writable_fields"]
    )
    assert "update_profile" not in result["allowed_actions"]
    denied = http.get(
        f"/api/pn-catalog/v1/parts/{pid}/access", headers=native(authority[1])
    )
    assert denied.status_code == 403 and "readable_fields" not in denied.text


def test_delegation_exchange_native_login_confusion_and_bounded_errors(
    db, authority, http
):
    pid = seed(db)
    path = f"/api/pn-catalog/v1/parts/{pid}"
    headers, signed = exchange(http, authority[2], "GET", path)
    result = http.get(path, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["part_id"] == pid
    replay = http.post("/api/pn-catalog/v1/auth/delegations", json={"proof": signed})
    assert (
        replay.status_code == 401
        and replay.json()["error"]["code"] == "auth.proof_replayed"
    )
    injected = http.post(
        "/api/pn-catalog/v1/auth/delegations",
        json={"proof": signed, "actor": {"role": "admin"}},
    )
    assert injected.status_code == 422 and signed not in injected.text
    too_large = http.post("/api/pn-catalog/v1/auth/delegations", content=b"x" * 65537)
    assert (
        too_large.status_code == 413
        and too_large.headers["cache-control"] == "no-store"
    )
    malformed = http.post(
        "/api/pn-catalog/v1/auth/delegations",
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 400
    bad = http.get(
        path,
        headers={
            "Authorization": headers["Authorization"].replace("PN-Delegation", "Bearer")
        },
    )
    assert bad.status_code == 401


def test_delegated_write_replay_body_binding_and_current_revoke(db, authority, http):
    pid = seed(db)
    path = f"/api/pn-catalog/v1/parts/{pid}/profile"
    key = uuid4()
    body = json.dumps(
        {
            "expected_identity_revision": 0,
            "field_versions": {"description": 0},
            "changes": {"description": "after"},
        },
        separators=(",", ":"),
    ).encode()
    headers, _ = exchange(
        http, authority[2], "PATCH", path, body, key=key, if_match='"profile-0"'
    )
    headers["Content-Type"] = "application/json"
    tampered = http.patch(
        path, content=body.replace(b"after", b"other"), headers=headers
    )
    assert (
        tampered.status_code == 401
        and tampered.json()["error"]["code"] == "auth.request_binding_mismatch"
    )
    first = http.patch(path, content=body, headers=headers)
    assert first.status_code == 200, first.text
    headers, _ = exchange(
        http, authority[2], "PATCH", path, body, key=key, if_match='"profile-0"'
    )
    headers["Content-Type"] = "application/json"
    second = http.patch(path, content=body, headers=headers)
    assert second.status_code == 200 and second.json()["meta"]["replayed"]
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.part_id == pid)
        )
        == 1
    )
    user = authority[0]
    user.token_version += 1
    db.commit()
    revoked = http.patch(path, content=body, headers=headers)
    assert revoked.status_code == 401, revoked.text
    db.expire_all()
    assert db.get(DimPart, pid).description == "after"


def test_delegated_access_intersects_client_fields_and_actions(
    db, authority, http, monkeypatch
):
    pid = seed(db)
    monkeypatch.setattr(
        get_settings(), "pn_shop_allowed_actions", ["read", "update_profile"]
    )
    monkeypatch.setattr(
        get_settings(), "pn_shop_allowed_fields", {"part_master": ["description"]}
    )
    path = f"/api/pn-catalog/v1/parts/{pid}/access"
    headers, _ = exchange(http, authority[2], "GET", path)
    response = http.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "part_id": pid,
        "allowed_actions": ["read", "update_profile"],
        "readable_fields": ["description"],
        "writable_fields": ["description"],
    }
