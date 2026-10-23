"""Live resource access: identity, query scope, fields and transaction coherence."""
from dataclasses import replace
from datetime import datetime, timezone
from queue import Queue
from threading import Thread
import time
from types import MappingProxyType

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, false, func, select, text
from sqlalchemy.orm import Session

from app import config
from app.authz import engine, runtime, service
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant, SysAuthorizationCutover, SysCustomerAssignment
from app.models.dimensions import DimCustomer
from app.models.system import SysUser
from app.security import UserContext, is_field_hidden
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def enabled_module(db, monkeypatch, cerbos_url):
    settings = get_settings()
    monkeypatch.setattr(settings, "authorization_mode", "unified_modules")
    monkeypatch.setattr(settings, "authz_enabled_resources", ["sales_analysis"])
    monkeypatch.setattr(settings, "cerbos_url", cerbos_url)
    runtime.activate_configured_resources(db.get_bind())


@pytest.fixture
def account(db, enabled_module):
    user = SysUser(username="authz-test-account", password_hash="synthetic-not-used", role="maintenance_manager", token_version=3)
    db.add(user)
    db.flush()
    grant = SysAuthorizationGrant(user_id=user.id, resource="sales_analysis", actions=["read_orders"],
                                  scope="assigned_customers", field_permissions=["data_customer"], version=1)
    db.add(grant)
    db.commit()
    identity = VerifiedIdentity(user.username, 3, False, "sys_user", MappingProxyType({}))
    ctx = UserContext(user_id=user.username, role="admin", is_authenticated=True,
                      authn="sys_user", token_version=3,
                      permissions={"data_profit": True, "data_purchase_cost": True, "own_customers_only": True})
    return user, grant, identity, ctx


@pytest.mark.parametrize("changes,status", [
    ({"authentication_method": "shared"}, 401), ({"fallback": True}, 403),
    ({"username": None}, 401), ({"username": "missing-account"}, 401),
    ({"token_version": 2}, 401), ({"token_version": None}, 401),
    ({"authentication_method": None}, 401), ({"token_version": True}, 401),
])
def test_invalid_principal_never_contacts_pdp(db, account, monkeypatch, changes, status):
    _, _, identity, _ = account
    monkeypatch.setattr(engine, "plan_access", lambda *a: pytest.fail("invalid identity reached PDP"))
    with pytest.raises(HTTPException) as exc:
        service.resource_access(db, replace(identity, **changes), "sales_analysis", "read_orders")
    assert exc.value.status_code == status


@pytest.mark.parametrize("column,value", [
    ("is_active", False), ("deleted_at", datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ("token_version", 4),
])
def test_account_changes_are_reloaded_over_session_identity_map(db, account, monkeypatch, column, value):
    user, _, identity, _ = account
    db.execute(text(f"UPDATE sys_user SET {column}=:value WHERE id=:id"), {"value": value, "id": user.id})
    monkeypatch.setattr(engine, "plan_access", lambda *a: pytest.fail("revoked account reached PDP"))
    with pytest.raises(HTTPException) as exc:
        service.resource_access(db, identity, "sales_analysis", "read_orders")
    assert exc.value.status_code == 401


def test_context_uses_only_one_live_grant_and_explicit_customer_ids(db, account, monkeypatch):
    user, _, identity, ctx = account
    customers = [DimCustomer(name_raw="authz-customer-a"), DimCustomer(name_raw="authz-customer-b")]
    db.add_all(customers)
    db.flush()
    db.add(SysCustomerAssignment(user_id=user.id, customer_id=customers[1].id))
    db.flush()
    captured = {}
    def planned(user_id, actual_grant, attributes, action, bindings):
        captured.update(user_id=user_id, grant=actual_grant, attributes=attributes, action=action)
        return engine.Plan(false(), frozenset({"data_customer"}))
    monkeypatch.setattr(engine, "plan_access", planned)
    access = service.sales_access(db, identity, ctx)
    assert captured["user_id"] == user.id
    assert captured["attributes"] == {"customer_ids": [str(customers[1].id)]}
    assert captured["grant"].field_permissions == ("data_customer",)
    assert access.context.role == "maintenance_manager"
    assert access.context.permissions["data_purchase_cost"] is False
    assert access.context.permissions["data_profit"] is False
    assert access.context.permissions["own_customers_only"] is False
    assert access.context.explicit_field_policy is True
    assert access.customer_count == 1
    assert access.access.require_binding("sales_analysis", "read_orders") is access.access
    with pytest.raises(HTTPException):
        access.access.require_binding("inventory", "read_orders")
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    assert is_field_hidden(access.context, "amount_ex_tax")
    assert not is_field_hidden(access.context, "customer")
    assert ctx.permissions["data_profit"] is True


def test_real_empty_assignment_still_allows_page_but_plan_is_empty(db, account):
    _, _, identity, ctx = account
    snapshot = service.my_snapshot(db, identity)["sales_analysis"]
    assert snapshot["read"] is True
    assert snapshot["export"] is False
    assert snapshot["scope"] == "assigned_customers"
    assert snapshot["customer_count"] == 0
    assert snapshot["field_permissions"] == ["data_customer"]
    assert "尚未分配" in snapshot["reason"]
    access = service.sales_access(db, identity, ctx)
    assert db.scalar(select(1).where(access.predicate)) is None


@pytest.mark.parametrize("invalid_fields", [["unknown"], ["data_profit"], [False], ["data_customer", "data_customer"]])
def test_corrupt_grant_does_not_fall_back_to_legacy_permissions(db, account, monkeypatch, invalid_fields):
    _, grant, identity, _ = account
    grant.field_permissions = invalid_fields
    db.flush()
    monkeypatch.setattr(engine, "plan_access", lambda *a: pytest.fail("invalid grant reached PDP"))
    with pytest.raises(HTTPException) as exc:
        service.resource_access(db, identity, "sales_analysis", "read_orders")
    assert exc.value.status_code == 403
    snapshot = service.my_snapshot(db, identity)
    assert snapshot["resources"]["sales_analysis"]["decisions"]["read_orders"]["effect"] == "deny"
    assert snapshot["sales_analysis"]["field_permissions"] == []


def test_missing_grant_and_unregistered_action_are_denied(db, account, monkeypatch):
    _, grant, identity, _ = account
    monkeypatch.setattr(engine, "plan_access", lambda *a: pytest.fail("missing grant reached PDP"))
    with pytest.raises(HTTPException) as exc:
        service.resource_access(db, identity, "sales_analysis", "delete")
    assert exc.value.status_code == 403
    db.delete(grant)
    db.flush()
    with pytest.raises(HTTPException) as exc:
        service.resource_access(db, identity, "sales_analysis", "read_orders")
    assert exc.value.status_code == 403


def test_engine_unavailable_is_503_without_legacy_fallback(db, account, monkeypatch):
    _, _, identity, _ = account
    def unavailable(*args):
        raise engine.AuthorizationUnavailable("synthetic outage")
    monkeypatch.setattr(engine, "plan_access", unavailable)
    monkeypatch.setattr(engine, "check_resources", unavailable)
    for call in (lambda: service.resource_access(db, identity, "sales_analysis", "read_orders"),
                 lambda: service.my_snapshot(db, identity)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "AUTHZ_ENGINE_UNAVAILABLE"


def test_disabled_module_reads_marker_but_does_not_contact_pdp(db, monkeypatch):
    monkeypatch.setattr(engine, "check_resources", lambda *a: pytest.fail("disabled module reached PDP"))
    identity = VerifiedIdentity("fallback", None, True, "shared", MappingProxyType({}))
    snapshot = service.my_snapshot(db, identity)
    assert snapshot["sales_analysis"]["enabled"] is False
    assert snapshot["sales_analysis"]["field_permissions"] == []
    assert db.scalar(select(func.count()).select_from(SysAuthorizationCutover)) == 0
    with pytest.raises(HTTPException) as exc:
        service.resource_access(db, identity, "sales_analysis", "read_orders")
    assert exc.value.status_code == 503



@pytest.mark.parametrize("mode", ["unified_modules", "legacy_compat"])
def test_suspension_keeps_new_chain_authoritative(db, account, monkeypatch, mode):
    _, _, identity, _ = account
    monkeypatch.setattr(get_settings(), "authorization_mode", mode)
    monkeypatch.setattr(get_settings(), "authz_enabled_resources", [])
    monkeypatch.setattr(engine, "check_resources", lambda *a: pytest.fail("suspended module reached PDP"))
    assert service.enabled(db) is True
    snapshot = service.my_snapshot(db, identity)["sales_analysis"]
    assert snapshot["enabled"] is True
    assert snapshot["read"] is False
    assert "暂停" in snapshot["reason"]
    with pytest.raises(HTTPException) as exc:
        service.resource_access(db, identity, "sales_analysis", "read_orders")
    assert exc.value.status_code == 503


def test_read_lock_keeps_grant_and_assignments_coherent_until_request_finishes(db, account):
    user, _, identity, _ = account
    user_id = user.id
    customers = [DimCustomer(name_raw="lock-customer-a"), DimCustomer(name_raw="lock-customer-b")]
    db.add_all(customers)
    db.flush()
    customer_ids = [customer.id for customer in customers]
    db.add(SysCustomerAssignment(user_id=user_id, customer_id=customer_ids[0]))
    db.commit()
    access = service.resource_access(db, identity, "sales_analysis", "read_orders")
    assert access.field_permissions == {"data_customer"}
    pid_queue = Queue()
    failures = []

    def replace_grant_and_assignment():
        try:
            with Session(db.get_bind()) as writer:
                pid_queue.put(writer.scalar(text("SELECT pg_backend_pid()")))
                target = writer.scalar(select(SysUser).where(SysUser.id == user_id).with_for_update())
                grant = writer.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == user_id))
                grant.field_permissions = ["data_purchase_cost"]
                grant.version += 1
                writer.execute(delete(SysCustomerAssignment).where(SysCustomerAssignment.user_id == user_id))
                writer.add(SysCustomerAssignment(user_id=user_id, customer_id=customer_ids[1]))
                target.token_version += 1
                writer.commit()
        except Exception as exc:
            failures.append(exc)

    writer = Thread(target=replace_grant_and_assignment, daemon=True)
    writer.start()
    try:
        pid = pid_queue.get(timeout=5)
        waiting = False
        with db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                wait_type = observer.scalar(text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid})
                if wait_type == "Lock":
                    waiting = True
                    break
                if failures or not writer.is_alive():
                    break
                time.sleep(.02)
        assert waiting, f"writer must wait for authorization snapshot: {failures}"
        assert access.field_permissions == {"data_customer"}
    finally:
        db.rollback()
        writer.join(timeout=8)
    assert not writer.is_alive()
    assert not failures
    with pytest.raises(HTTPException) as exc:
        service.resource_access(db, identity, "sales_analysis", "read_orders")
    assert exc.value.status_code == 401
    refreshed = service.resource_access(db, replace(identity, token_version=4), "sales_analysis", "read_orders")
    assert refreshed.field_permissions == {"data_purchase_cost"}
    assert refreshed.grant_version == 2
