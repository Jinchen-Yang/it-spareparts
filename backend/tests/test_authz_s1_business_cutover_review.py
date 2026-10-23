"""Third-angle review: old business privileges and persisted cutover authority.

Uses real PostgreSQL, signed HTTP identities and real Cerbos. Catalog mutation
cases model an inconsistent candidate build; they never change policy decisions.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import time
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import auth, config
from app.api import authorization, sales_analytics
from app.auth import _make_token
from app.authz import catalog, engine, runtime
from app.config import get_settings
from app.models.authorization import SysAuthorizationCutover, SysAuthorizationGrant, SysCustomerAssignment
from app.models.system import SysUser
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_sales_dashboard_d44 import seeded  # noqa: F401; synthetic business facts only


@pytest.fixture
def business_client(db, monkeypatch, cerbos_url):
    settings = get_settings()
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    monkeypatch.setattr(settings, "authorization_mode", "unified_modules")
    monkeypatch.setattr(settings, "authz_enabled_resources", ["sales_analysis"])
    monkeypatch.setattr(settings, "authz_suspended_resources", [])
    monkeypatch.setattr(settings, "authz_sales_analysis_enabled", False)
    monkeypatch.setattr(settings, "authz_sales_analysis_suspended", False)
    monkeypatch.setattr(settings, "cerbos_url", cerbos_url)
    runtime.activate_configured_resources(db.get_bind())
    app = FastAPI()
    app.include_router(authorization.router, prefix="/api")
    app.include_router(sales_analytics.router, prefix="/api")
    with TestClient(app) as client:
        yield client


def _user(db, role="admin", *, actions=None, fields=(), customer_id=None):
    permissions = {"page_sales_analysis": True, **{field: True for field in catalog.SALES_FIELD_PERMISSIONS}}
    user = SysUser(username="third-angle-user", role=role, password_hash="synthetic-unused",
                   permissions=permissions)
    db.add(user)
    db.flush()
    if actions is not None:
        configuration = {"actions": actions, "scope": "assigned_customers", "field_permissions": list(fields)}
        db.add(SysAuthorizationGrant(user_id=user.id, resource="sales_analysis", version=1, **configuration))
        user.authorization_profile = {"schema_version": 1, "template_snapshot": None,
                                      "resource_overrides": {"sales_analysis": configuration}}
    if customer_id is not None:
        db.add(SysCustomerAssignment(user_id=user.id, customer_id=customer_id))
    db.commit()
    token, _ = _make_token(role, user.username, None, perms=permissions,
                          authn="sys_user", token_version=user.token_version)
    return user, {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("role", ["admin", "boss", "sales"])
@pytest.mark.parametrize("actions", [None, []], ids=["missing-grant", "deny-tombstone"])
def test_legacy_role_and_full_token_permissions_never_restore_sales_allow(
        db, business_client, monkeypatch, role, actions):
    _, headers = _user(db, role, actions=actions)
    if actions is None:
        monkeypatch.setattr(engine, "_client", lambda: pytest.fail("missing Grant reached PDP"))
    snapshot = business_client.get("/api/authz/me", headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    sales = snapshot.json()["resources"]["sales_analysis"]
    assert all(item["effect"] == "deny" for item in sales["decisions"].values())
    assert sales["field_permissions"] == []
    for suffix in ("options", "orders", "orders/export"):
        response = business_client.get("/api/sales/analytics/" + suffix, headers=headers)
        assert response.status_code == 403, response.text


def test_same_snapshot_fields_scope_and_export_deny_survive_legacy_admin_bypass(
        db, seeded, business_client, monkeypatch):
    _, headers = _user(db, actions=["read"], fields=["data_customer"],
                       customer_id=seeded["a"][2].customer_id)
    # Exercise the replacement context even when the legacy context is a
    # bypass-role placeholder and the old token asserts every sensitive field.
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    snapshot = business_client.get("/api/authz/me", headers=headers).json()
    resource = snapshot["resources"]["sales_analysis"]
    assert resource["field_permissions"] == ["data_customer"]
    assert resource["scope_kind"] == "assigned_customers"
    assert resource["decisions"]["export"]["effect"] == "deny"
    response = business_client.get("/api/sales/analytics/orders", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 2
    assert {row["order_no"] for row in data["items"]} == {"SO-A3", "SO-A6"}
    assert all(row["customer"] == "B公司" and row["amount_ex_tax"] is None for row in data["items"])
    for suffix in ("orders/export", "amount-histogram", "orders?amount_min=1"):
        assert business_client.get("/api/sales/analytics/" + suffix, headers=headers).status_code == 403
    assert business_client.get(f"/api/sales/analytics/orders/{seeded['a'][0].id}", headers=headers).status_code == 404


def test_existing_legacy_connection_observes_other_instance_activation(db, monkeypatch):
    old = SimpleNamespace(authorization_mode="legacy_compat", authz_enabled_resources=[],
                          authz_suspended_resources=[], authz_sales_analysis_enabled=False,
                          authz_sales_analysis_suspended=False)
    new = SimpleNamespace(**{**vars(old), "authorization_mode": "unified_modules",
                             "authz_enabled_resources": ["sales_analysis"]})
    monkeypatch.setattr(runtime, "get_settings", lambda: old)
    with Session(db.get_bind()) as old_instance:
        assert runtime.is_authoritative(old_instance, "sales_analysis") is False
        monkeypatch.setattr(runtime, "get_settings", lambda: new)
        runtime.activate_configured_resources(db.get_bind())
        monkeypatch.setattr(runtime, "get_settings", lambda: old)
        assert runtime.is_authoritative(old_instance, "sales_analysis") is True
        assert runtime.runtime_states(old_instance)["sales_analysis"].runtime_state == "suspended"
        assert old_instance.scalar(select(func.count()).select_from(SysAuthorizationCutover)) == 1


def _corrupt_catalog(monkeypatch, corruption):
    descriptors = deepcopy(catalog.resource_descriptors())
    sales = next(row for row in descriptors if row["id"] == "sales_analysis")
    if corruption == "planned":
        sales["migration_state"] = "planned"
    elif corruption == "missing_action":
        sales["actions"] = [item for item in sales["actions"] if item["id"] != "export"]
    elif corruption == "unknown_action_dependency":
        sales["actions"][0]["requires_actions"] = ["unknown_action"]
    elif corruption == "unknown_field_dependency":
        sales["actions"][0]["requires_fields"] = ["unknown_field"]
    else:
        sales["denial_scope"] = None
    monkeypatch.setattr(catalog, "_DESCRIPTORS", descriptors)


@pytest.mark.parametrize("corruption", ["planned", "missing_action", "unknown_action_dependency",
                                        "unknown_field_dependency", "missing_denial_scope"])
def test_startup_rejects_inconsistent_executable_catalog_before_cutover_write(db, monkeypatch, corruption):
    settings = get_settings()
    monkeypatch.setattr(settings, "authorization_mode", "unified_modules")
    monkeypatch.setattr(settings, "authz_enabled_resources", ["sales_analysis"])
    monkeypatch.setattr(settings, "authz_suspended_resources", [])
    monkeypatch.setattr(settings, "authz_sales_analysis_enabled", False)
    monkeypatch.setattr(settings, "authz_sales_analysis_suspended", False)
    _corrupt_catalog(monkeypatch, corruption)
    with pytest.raises((HTTPException, ValueError)):
        runtime.activate_configured_resources(db.get_bind())
    assert db.scalar(select(func.count()).select_from(SysAuthorizationCutover)) == 0


def test_persisted_marker_cannot_be_overruled_by_regressed_catalog_metadata(
        db, business_client, monkeypatch):
    _, headers = _user(db)
    assert business_client.get("/api/sales/analytics/options", headers=headers).status_code == 403
    assert db.scalar(select(func.count()).select_from(SysAuthorizationCutover)) == 1
    _corrupt_catalog(monkeypatch, "planned")
    response = business_client.get("/api/sales/analytics/options", headers=headers)
    assert response.status_code == 503, response.text


@pytest.mark.parametrize("revocation", ["epoch", "deleted"])
def test_password_writer_waiting_on_real_row_lock_cannot_renew_revoked_identity(db, revocation):
    user, headers = _user(db, role="sales")
    user.password_hash = auth.hash_password("synthetic-original-password")
    db.commit()
    user_id = user.id
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    with Session(db.get_bind()) as locker:
        locked = locker.scalar(select(SysUser).where(SysUser.id == user_id).with_for_update())
        if revocation == "epoch":
            locked.token_version += 1
        else:
            locked.deleted_at = datetime.now(timezone.utc)
        locker.flush()
        locker_pid = locker.scalar(text("SELECT pg_backend_pid()"))
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(client.post, "/api/auth/change-password", headers=headers,
                                    json={"current_password": "synthetic-original-password",
                                          "new_password": "synthetic-replacement-password"})
            try:
                # The earlier plain identity read sees the old committed row;
                # the credential writer must then wait on this actual PG lock.
                deadline = time.monotonic() + 10
                blocked = False
                with db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                    while time.monotonic() < deadline:
                        blocked = observer.scalar(text("""
                            SELECT EXISTS (
                                SELECT 1 FROM pg_stat_activity
                                WHERE :locker = ANY(pg_blocking_pids(pid))
                                  AND query ILIKE '%sys_user%'
                                  AND query ILIKE '%FOR UPDATE%'
                            )
                        """), {"locker": locker_pid})
                        if blocked:
                            break
                        time.sleep(.02)
                assert blocked, "password route never reached the real user row lock"
                locker.commit()
            finally:
                locker.rollback()
            response = future.result(timeout=10)
    assert response.status_code == 401, response.text
    db.refresh(user)
    assert auth.verify_password("synthetic-original-password", user.password_hash)
    assert user.token_version == (1 if revocation == "epoch" else 0)
