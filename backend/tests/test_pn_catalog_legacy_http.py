"""Old HTTP URLs cannot bypass the shared authority after resource cutover."""
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import _make_token
from app.config import get_settings
from app.main import app
from app.models.authorization import SysAuthorizationGrant
from app.models.dimensions import DimPart
from app.pn_catalog.models import AuditEvent
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_pn_catalog_access import authority  # noqa: F401

client = TestClient(app)


def headers(user, key=None):
    token, _ = _make_token(user.role, user.username, user.display_name,
                          token_version=user.token_version, authn="sys_user")
    return {"Authorization": f"Bearer {token}", **({"Idempotency-Key": str(key)} if key else {})}


def seed(db, pn="LEGACY-ADAPTER"):
    part = DimPart(pn_std=pn, description="original", brand="old")
    db.add(part)
    db.commit()
    return part.id


def baseline(user, part_id):
    response = client.get(f"/api/parts/master/{part_id}/edit-state", headers=headers(user))
    assert response.status_code == 200, response.text
    return response.json()


def patch(user, state, key=None, **updates):
    return client.patch("/api/parts/master", headers=headers(user, key), json={
        "part_id": state["part_id"], "pn_std": state["pn_std"], "base_snapshot": state, **updates})


def test_old_urls_use_shared_versions_receipts_and_verified_actor(db, authority):  # noqa: F811
    user, _, _ = authority
    part_id = seed(db)
    state = baseline(user, part_id)  # readonly legacy role has an explicit new grant.
    key = uuid4()
    response = patch(user, state, key, brand="new")
    assert response.status_code == 200, response.text
    assert patch(user, state, key, brand="new").status_code == 200
    assert patch(user, state, uuid4(), brand="stale").status_code == 409
    assert patch(user, state, uuid4(), description="independent").status_code == 200
    db.expire_all()
    events = db.scalars(select(AuditEvent).where(AuditEvent.part_id == part_id)).all()
    assert len(events) == 2
    assert events[0].actor["principal_id"] == f"account:{user.id}"
    assert events[0].actor["authorization_epoch"] == user.token_version


def test_old_state_projects_fields_and_admin_cannot_bypass_grant(db, authority):  # noqa: F811
    user, admin, _ = authority
    part_id = seed(db)
    grant = db.scalar(select(SysAuthorizationGrant).where(
        SysAuthorizationGrant.user_id == user.id, SysAuthorizationGrant.resource == "part_master"))
    grant.actions = ["read", "edit"]
    grant.field_permissions = ["brand"]
    db.commit()
    state = baseline(user, part_id)
    assert state["fields"] == {"brand": "old"}
    assert state["specs"] == {}
    denied = patch(user, state, uuid4(), description="hidden")
    assert denied.status_code == 403, denied.text
    response = client.get(f"/api/parts/master/{part_id}/edit-state", headers=headers(admin))
    assert response.status_code == 403, response.text


def test_legacy_force_batch_and_missing_request_id_cannot_evade_commands(db, authority):  # noqa: F811
    user, _, _ = authority
    state = baseline(user, seed(db))
    assert patch(user, state, brand="without key").status_code == 428
    result = client.post("/api/parts/master", headers=headers(user, uuid4()),
                         json={"pn_std": "OTHER-PN", "force": True})
    assert result.status_code == 428
    malformed = patch(user, state, uuid4(), spec_updates={"capacity": {"value": "1TB", "unit": "GB", "numeric_value": 1000}})
    assert malformed.status_code == 422, malformed.text
    result = client.post("/api/parts/master/batch-apply", headers=headers(user, uuid4()),
                         json={"part_ids": [state["part_id"]]})
    assert result.status_code == 410, result.text


def test_removing_enabled_resource_suspends_old_urls_instead_of_role_fallback(db, authority, monkeypatch):  # noqa: F811
    user, _, _ = authority
    part_id = seed(db)
    baseline(user, part_id)
    monkeypatch.setattr(get_settings(), "authz_enabled_resources", [])
    response = client.get(f"/api/parts/master/{part_id}/edit-state", headers=headers(user))
    assert response.status_code in (403, 503), response.text
