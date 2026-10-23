"""Real PostgreSQL management transactions and frozen legacy migration boundaries."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.orm import Session

from app import permissions
from app.api import authorization as api
from app.auth import _make_token
from app.authz.catalog import SALES_FIELD_PERMISSIONS
from app.config import get_settings
from app.db import SessionLocal
from sqlalchemy.exc import OperationalError
from app.models.authorization import SysAuthorizationGrant, SysCustomerAssignment
from app.models.dimensions import DimCustomer
from app.models.system import SysAuditLog, SysUser


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "authz_sales_analysis_enabled", False)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    with TestClient(app) as value:
        yield value


def _user(db, username, role="sales", **kwargs):
    user = SysUser(username=username, role=role, password_hash="synthetic-no-password", **kwargs)
    db.add(user)
    db.commit()
    return user


def _headers(user=None, *, sub=None, role=None, fallback=False, authn="sys_user", tv=None):
    token, _ = _make_token(
        role or (user.role if user else "admin"), sub or (user.username if user else "missing"), None,
        fallback=fallback, authn=authn, token_version=(user.token_version if user else 0) if tv is None else tv,
    )
    return {"Authorization": f"Bearer {token}"}


def _body(**kwargs):
    return {"expected_version": 0, "actions": ["read", "export"], "scope": "assigned_customers",
            "field_permissions": ["data_customer"], "customer_ids": [], **kwargs}


def _customers(db):
    rows = [DimCustomer(name_raw="old-A", name_normalized="客户甲"),
            DimCustomer(name_raw="客户乙", name_normalized=None),
            DimCustomer(name_raw="literal_100%", name_normalized="")]
    db.add_all(rows)
    db.commit()
    return rows


def test_full_replacement_audit_version_revocation_and_no_legacy_dual_write(db, client):
    admin = _user(db, "admin", "admin")
    sales = _user(db, "sales", template_perms={"page_sales_analysis": False}, permissions={"data_profit": False})
    customers = _customers(db)
    headers, old_sales_headers = _headers(admin), _headers(sales)
    path = "/api/authz/users/sales"
    empty = client.get(path, headers=headers)
    assert empty.status_code == 200
    assert empty.headers["cache-control"] == "no-store"
    assert empty.json() == {"username": "sales", "version": 0, "actions": [], "scope": "assigned_customers",
                            "field_permissions": [], "customer_ids": [], "customers": []}
    first = client.put(path, headers=headers, json=_body(customer_ids=[customers[1].id, customers[0].id]))
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1
    assert first.json()["customer_ids"] == sorted([customers[0].id, customers[1].id])
    assert first.json()["customers"][0]["name"] == "客户甲"
    assert client.get("/api/authz/me", headers=old_sales_headers).status_code == 401
    second = client.put(path, headers=headers, json=_body(expected_version=1, actions=[], field_permissions=[],
                                                         customer_ids=[customers[2].id]))
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2
    assert second.json()["actions"] == []
    assert second.json()["customer_ids"] == [customers[2].id]
    db.expire_all()
    assert sales.token_version == 2
    assert admin.token_version == 0
    assert sales.template_perms == {"page_sales_analysis": False}
    assert sales.permissions == {"data_profit": False}
    logs = list(db.scalars(select(SysAuditLog).order_by(SysAuditLog.id)))
    assert len(logs) == 2
    assert logs[1].operated_by == "admin"
    assert logs[1].before_json == {"resource": "sales_analysis", **first.json()}
    assert logs[1].after_json == {"resource": "sales_analysis", **second.json()}


def test_shared_customer_assignment_is_explicit_and_allowed(db, client):
    admin = _user(db, "admin", "admin")
    _user(db, "first")
    _user(db, "second")
    customers = _customers(db)
    for username in ("first", "second"):
        response = client.put(f"/api/authz/users/{username}", headers=_headers(admin),
                              json=_body(customer_ids=[customers[0].id]))
        assert response.status_code == 200, response.text
    assert db.scalar(select(func.count()).select_from(SysCustomerAssignment)) == 2


@pytest.mark.parametrize("identity_case", ["ordinary", "delegated", "boss", "shared", "missing", "old_token", "demoted", "disabled", "deleted", "stale"])
def test_only_live_named_admin_can_read_or_write_management(db, client, identity_case):
    actor = _user(db, "actor", "admin")
    _user(db, "target")
    headers = _headers(actor)
    if identity_case in {"ordinary", "delegated", "boss", "demoted"}:
        actor.role = "boss" if identity_case == "boss" else "sales"
        actor.permissions = {"action_account_manage": True, "page_accounts": True} if identity_case == "delegated" else {}
    elif identity_case == "shared":
        headers = _headers(actor, fallback=True)
    elif identity_case == "missing":
        headers = _headers(sub="no-real-account")
    elif identity_case == "old_token":
        headers = _headers(actor, authn=None)
    elif identity_case == "disabled":
        actor.is_active = False
    elif identity_case == "deleted":
        actor.deleted_at = datetime.now(timezone.utc)
    elif identity_case == "stale":
        actor.token_version += 1
    db.commit()
    for path in ("/api/authz/users/target", "/api/authz/customers", "/api/authz/catalog"):
        assert client.get(path, headers=headers).status_code in (401, 403)
    assert client.put("/api/authz/users/target", headers=headers, json=_body()).status_code in (401, 403)
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 0


@pytest.mark.parametrize("change", [
    {"actions": ["export"]}, {"actions": ["read", "unregistered"]}, {"actions": ["read", "read"]},
    {"scope": "team"}, {"field_permissions": ["data_profit"]}, {"field_permissions": ["data_reconciliation"]}, {"field_permissions": ["data_unknown"]},
    {"field_permissions": ["data_customer", "data_customer"]}, {"actions": [1]},
    {"customer_ids": [True]}, {"customer_ids": [-1]}, {"customer_ids": [1, 1]},
    {"expected_version": True}, {"expected_version": -1}, {"role": "admin"},
])
def test_invalid_grant_is_rejected_without_mutation(db, client, change):
    admin = _user(db, "admin", "admin")
    target = _user(db, "target")
    response = client.put("/api/authz/users/target", headers=_headers(admin), json=_body(**change))
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == 0
    db.refresh(target)
    assert target.token_version == 0


def test_missing_and_deleted_targets_and_missing_customer_are_not_partially_applied(db, client):
    admin = _user(db, "admin", "admin")
    target = _user(db, "target")
    _user(db, "deleted", deleted_at=datetime.now(timezone.utc))
    for username, body in (("missing", _body()), ("deleted", _body()), ("target", _body(customer_ids=[9999]))):
        response = client.put(f"/api/authz/users/{username}", headers=_headers(admin), json=body)
        assert response.status_code == 404, response.text
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 0
    db.refresh(target)
    assert target.token_version == 0


def test_concurrent_first_save_has_one_winner_and_one_version_conflict(db, client, monkeypatch):
    admin = _user(db, "admin", "admin")
    target = _user(db, "target")
    headers = _headers(admin)
    barrier = Barrier(2, timeout=10)
    original = api._target

    def synchronized_target(*args):
        value = original(*args)
        barrier.wait()
        return value

    monkeypatch.setattr(api, "_target", synchronized_target)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.put, "/api/authz/users/target", headers=headers,
                               json=_body(scope=scope)) for scope in ("all", "assigned_customers")]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 1
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == 1
    db.refresh(target)
    assert target.token_version == 1


def test_actor_is_rechecked_after_account_lock(db, client, monkeypatch):
    admin = _user(db, "admin", "admin")
    target = _user(db, "target")
    headers, admin_id = _headers(admin), admin.id
    original = api._target

    def revoke_after_dependency(*args):
        value = original(*args)
        with SessionLocal() as other:
            other.execute(update(SysUser).where(SysUser.id == admin_id).values(role="sales"))
            other.commit()
        return value

    monkeypatch.setattr(api, "_target", revoke_after_dependency)
    response = client.put("/api/authz/users/target", headers=headers, json=_body())
    assert response.status_code == 403
    db.refresh(target)
    assert target.token_version == 0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 0


def test_audit_failure_rolls_back_grant_assignments_and_session_revocation(db, client):
    admin = _user(db, "admin", "admin")
    target = _user(db, "target")
    customers = _customers(db)
    headers = _headers(admin)
    first = client.put("/api/authz/users/target", headers=headers, json=_body(customer_ids=[customers[0].id]))
    assert first.status_code == 200

    def reject_audit(session, _context, _instances):
        if any(isinstance(value, SysAuditLog) for value in session.new):
            raise RuntimeError("synthetic audit storage failure")

    event.listen(Session, "before_flush", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit storage failure"):
            client.put("/api/authz/users/target", headers=headers,
                       json=_body(expected_version=1, scope="all", customer_ids=[customers[1].id]))
    finally:
        event.remove(Session, "before_flush", reject_audit)
    assert client.get("/api/authz/users/target", headers=headers).json() == first.json()
    db.refresh(target)
    assert target.token_version == 1
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == 1


def test_self_edit_invalidates_own_session_after_successful_atomic_response(db, client):
    admin = _user(db, "admin", "admin")
    headers = _headers(admin)
    before = client.get("/api/authz/me", headers=headers)
    assert before.json()["username"] == "admin"
    assert before.json()["can_manage"] is True
    response = client.put("/api/authz/users/admin", headers=headers, json=_body(actions=[]))
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert client.get("/api/authz/me", headers=headers).status_code == 401


@pytest.mark.parametrize("endpoint", ["me", "users/target"])
def test_read_snapshot_and_version_hold_one_lock_until_concurrent_save_finishes(db, client, monkeypatch, endpoint):
    admin = _user(db, "admin", "admin")
    target = _user(db, "target")
    customers = _customers(db)
    admin_headers = _headers(admin)
    initial = client.put("/api/authz/users/target", headers=admin_headers,
                         json=_body(customer_ids=[customers[0].id]))
    assert initial.status_code == 200
    db.refresh(target)
    target_id, old_customer_id, new_customer_id = target.id, customers[0].id, customers[1].id
    read_headers = _headers(target) if endpoint == "me" else admin_headers
    db.rollback()
    snapshot_ready, release_snapshot = Event(), Event()
    original = api._snapshot

    def pause_snapshot(db_session, loaded_target, grant):
        result = original(db_session, loaded_target, grant)
        if not snapshot_ready.is_set():
            snapshot_ready.set()
            assert release_snapshot.wait(10), "snapshot release timed out"
        return result

    if endpoint == "me":
        def service_snapshot(db_session, ctx):
            user = db_session.scalar(select(SysUser).where(SysUser.username == ctx.user_id))
            grant = api._grant(db_session, user.id)
            result = pause_snapshot(db_session, user, grant)
            return {"enabled": True, "read": True, "export": True, "scope": result["scope"],
                    "customer_count": len(result["customer_ids"]), "field_permissions": result["field_permissions"],
                    "reason": "synthetic snapshot under real database lock"}
        monkeypatch.setattr(api.service, "sales_snapshot", service_snapshot)
    else:
        monkeypatch.setattr(api, "_snapshot", pause_snapshot)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(client.get, f"/api/authz/{endpoint}", headers=read_headers)
        writer = None
        try:
            assert snapshot_ready.wait(10), "snapshot did not start"
            # PostgreSQL itself proves the read holds a conflicting account
            # lock; this is not inferred from timing or a mocked lock method.
            with SessionLocal() as observer:
                with pytest.raises(OperationalError) as blocked:
                    observer.scalar(select(SysUser).where(SysUser.id == target_id).with_for_update(nowait=True))
                assert blocked.value.orig.sqlstate == "55P03"
                observer.rollback()
            writer = pool.submit(client.put, "/api/authz/users/target", headers=admin_headers,
                                 json=_body(expected_version=1, scope="all", customer_ids=[new_customer_id]))
        finally:
            release_snapshot.set()
        read_response = reader.result(timeout=15)
        assert read_response.status_code == 200, read_response.text
        written = writer.result(timeout=15)
        assert written.status_code == 200, written.text
    if endpoint == "me":
        assert read_response.json()["version"].endswith(":1:1")
        assert read_response.json()["sales_analysis"]["scope"] == "assigned_customers"
        assert read_response.json()["sales_analysis"]["customer_count"] == 1
    else:
        assert read_response.json()["version"] == 1
        assert read_response.json()["customer_ids"] == [old_customer_id]
    assert written.json()["version"] == 2
    assert written.json()["scope"] == "all"
    assert written.json()["customer_ids"] == [new_customer_id]


def test_me_distinguishes_shared_missing_and_real_identity_and_delegates_snapshot(db, client, monkeypatch):
    admin = _user(db, "admin", "admin")
    received = []
    snapshot = {"enabled": True, "read": True, "export": False, "scope": "assigned_customers",
                "customer_count": 2, "field_permissions": ["data_customer"], "reason": "synthetic service result"}

    def capture(db_session, ctx):
        received.append(ctx)
        return snapshot

    monkeypatch.setattr(api.service, "sales_snapshot", capture)
    result = client.get("/api/authz/me", headers=_headers(admin))
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["sales_analysis"] == snapshot
    assert result.json()["can_manage"] is True
    assert received[-1].authn == "sys_user"
    assert received[-1].token_version == 0
    for headers in (_headers(admin, fallback=True), _headers(sub="missing"), _headers(admin, authn=None)):
        response = client.get("/api/authz/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["username"] is None
        assert response.json()["can_manage"] is False
    assert client.get("/api/authz/me").status_code == 401


def test_catalog_and_customer_search_mark_legacy_and_preserve_literal_names(db, client):
    admin = _user(db, "admin", "admin")
    customers = _customers(db)
    headers = _headers(admin)
    catalog = client.get("/api/authz/catalog", headers=headers).json()
    migrated = [item for item in catalog["modules"] if item["status"] == "migrated"]
    assert len(migrated) == 1 and migrated[0]["id"] == "sales_analysis"
    assert migrated[0]["enabled"] is False
    assert "旧账号权限" in migrated[0]["notice"]
    assert {item["id"] for item in migrated[0]["field_permissions"]} == SALES_FIELD_PERMISSIONS
    assert {item["permission_key"] for item in catalog["modules"] if item["status"] == "legacy"} == set(permissions.PAGE_KEYS) - {"page_sales_analysis"}
    for query, expected in (("客户甲", customers[0]), ("old-A", customers[0]), ("_100%", customers[2])):
        response = client.get("/api/authz/customers", headers=headers, params={"q": query})
        assert response.status_code == 200
        assert response.json()["items"] == [{"id": expected.id, "name": expected.name_normalized or expected.name_raw,
                                              "raw_name": expected.name_raw}]
    assert len(client.get("/api/authz/customers", headers=headers, params={"limit": 1}).json()["items"]) == 1
    assert client.get("/api/authz/customers", headers=headers, params={"limit": 101}).status_code == 422


def test_customer_choices_and_assignments_disambiguate_same_normalized_name_by_raw_name_and_id(db, client):
    admin = _user(db, "admin", "admin")
    _user(db, "target")
    customers = [DimCustomer(name_raw="客户甲-总公司", name_normalized="客户甲"),
                 DimCustomer(name_raw="客户甲-分公司", name_normalized="客户甲")]
    db.add_all(customers)
    db.commit()
    expected = [{"id": row.id, "name": "客户甲", "raw_name": row.name_raw} for row in customers]
    headers = _headers(admin)
    options = client.get("/api/authz/customers", headers=headers, params={"q": "客户甲"})
    assert options.status_code == 200
    assert options.json()["items"] == expected
    saved = client.put("/api/authz/users/target", headers=headers,
                       json=_body(customer_ids=[customers[1].id]))
    assert saved.status_code == 200
    assert saved.json()["customers"] == [expected[1]]
    loaded = client.get("/api/authz/users/target", headers=headers)
    assert loaded.json()["customers"] == [expected[1]]


def _migration():
    path = Path(__file__).parents[1] / "alembic/versions/f1a3c5e7b9d2_authorization_sales_customers.py"
    spec = importlib.util.spec_from_file_location("sales_authorization_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("role", ["admin", "boss", "sales", "purchaser", "readonly", "maintenance_manager", "unknown"])
@pytest.mark.parametrize("snapshot", [None, {}, {"page_sales_analysis": True, "data_profit": True, "own_customers_only": True}])
def test_frozen_migration_projection_matches_current_legacy_semantics(role, snapshot):
    migration = _migration()
    values = {"role": role, "template_perms": snapshot,
              "perm_overrides": {"data_supplier": False, "data_purchase_cost": False},
              "permissions": {"page_sales_analysis": True, "own_customers_only": True, "data_purchase_cost": False}}
    expected = permissions.runtime_safe(permissions.effective_for_user(SimpleNamespace(**values)))
    assert migration._legacy_permissions(values) == {key: expected[key] for key in migration._KEYS}


def test_real_postgres_migration_seeds_only_old_page_entitlement_and_never_guesses_ownership(db):
    backend = Path(__file__).parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    db.rollback()
    command.downgrade(config, "d8f6a2c4b9e7")
    try:
        admin = _user(db, "admin", "admin", template_perms={"page_sales_analysis": False, "own_customers_only": True})
        boss = _user(db, "boss", "boss", template_perms={"page_sales_analysis": True, "own_customers_only": True})
        sales = _user(db, "sales", template_perms={"page_sales_analysis": True, "data_profit": True,
                                                   "data_purchase_cost": False, "data_customer": True, "data_reconciliation": True},
                      salesperson_name="甲")
        _user(db, "blocked", "boss", template_perms={"page_sales_analysis": False})
        _user(db, "deleted", "admin", deleted_at=datetime.now(timezone.utc))
        inactive = _user(db, "inactive", "boss", is_active=False)
        ids = {row.username: row.id for row in (admin, boss, sales, inactive)}
        db.rollback()
        command.upgrade(config, "f1a3c5e7b9d2")
        grants = {row.user_id: row for row in db.scalars(select(SysAuthorizationGrant))}
        assert set(grants) == set(ids.values())
        assert grants[ids["admin"]].scope == "all"  # legacy effective admin ignores raw own=true
        assert grants[ids["boss"]].scope == "assigned_customers"
        assert grants[ids["sales"]].scope == "assigned_customers"
        assert grants[ids["inactive"]].scope == "all"
        assert grants[ids["sales"]].field_permissions == ["data_customer"]
        assert all(set(row.actions) == {"read", "export"} and row.version == 1 for row in grants.values())
        assert db.scalar(select(func.count()).select_from(SysCustomerAssignment)) == 0
        assert db.scalar(select(func.sum(SysUser.token_version))) == 0
        _user(db, "new-after-migration", "admin")
        assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 4
        db.rollback()
        command.downgrade(config, "d8f6a2c4b9e7")
        assert db.execute(text("SELECT to_regclass('sys_authorization_grant')")).scalar_one() is None
        db.rollback()
    finally:
        db.rollback()
        command.upgrade(config, "head")
