from dataclasses import replace
from datetime import datetime, timezone
from queue import Queue
from threading import Thread
import time
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, false, select, text
from sqlalchemy.orm import Session

from app import config
from app.authz import engine, service
from app.models.authorization import SysAuthorizationGrant, SysCustomerAssignment
from app.models.dimensions import DimCustomer
from app.models.system import SysUser
from app.security import UserContext, is_field_hidden
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def enabled_module(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(authz_sales_analysis_enabled=True))


@pytest.fixture
def account(db, enabled_module):
    user = SysUser(username="authz-test-account", password_hash="synthetic-not-used", role="maintenance_manager", token_version=3)
    db.add(user)
    db.flush()
    grant = SysAuthorizationGrant(user_id=user.id, resource="sales_analysis", actions=["read"],
                                  scope="assigned_customers", field_permissions=["data_customer"], version=1)
    db.add(grant)
    db.commit()
    ctx = UserContext(user_id=user.username, role="admin", is_authenticated=True,
                      authn="sys_user", token_version=3,
                      permissions={"data_profit": True, "data_purchase_cost": True, "own_customers_only": True})
    return user, grant, ctx


@pytest.mark.parametrize("changes", [
    {"authn": "shared"}, {"is_fallback": True}, {"is_authenticated": False},
    {"user_id": None}, {"user_id": "missing-account"}, {"token_version": 2},
    {"token_version": None}, {"authn": None},
])
def test_invalid_principal_never_contacts_pdp(db, account, monkeypatch, changes):
    _, _, ctx = account
    monkeypatch.setattr(engine, "plan_sales", lambda *a: pytest.fail("invalid identity reached PDP"))
    with pytest.raises(HTTPException) as exc:
        service.sales_access(db, replace(ctx, **changes))
    assert exc.value.status_code == 403
    assert service.sales_snapshot(db, replace(ctx, **changes))["read"] is False


@pytest.mark.parametrize("column,value", [
    ("is_active", False), ("deleted_at", datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ("token_version", 4),
])
def test_account_changes_are_reloaded_over_session_identity_map(db, account, monkeypatch, column, value):
    user, _, ctx = account
    # Raw SQL leaves the ORM object cached; service must explicitly refresh it.
    db.execute(text(f"UPDATE sys_user SET {column}=:value WHERE id=:id"), {"value": value, "id": user.id})
    monkeypatch.setattr(engine, "plan_sales", lambda *a: pytest.fail("revoked account reached PDP"))
    with pytest.raises(HTTPException) as exc:
        service.sales_access(db, ctx)
    assert exc.value.status_code == 403


def test_context_uses_only_one_live_grant_and_explicit_customer_ids(db, account, monkeypatch):
    user, grant, ctx = account
    customers = [DimCustomer(name_raw="authz-customer-a"), DimCustomer(name_raw="authz-customer-b")]
    db.add_all(customers)
    db.flush()
    db.add(SysCustomerAssignment(user_id=user.id, customer_id=customers[1].id))
    db.flush()
    captured = {}
    def planned(user_id, actual_grant, customer_ids, action, table, customer_column):
        captured.update(user_id=user_id, grant=actual_grant, customer_ids=customer_ids, action=action)
        return engine.SalesPlan(false(), frozenset({"data_customer"}))
    monkeypatch.setattr(engine, "plan_sales", planned)
    access = service.sales_access(db, ctx)
    assert captured["user_id"] == user.id
    assert captured["customer_ids"] == (customers[1].id,)
    assert captured["grant"].field_permissions == ("data_customer",)
    assert access.context.role == "maintenance_manager"  # live account, not forged stale ctx role
    assert access.context.permissions["data_purchase_cost"] is False
    assert access.context.permissions["data_profit"] is False
    assert access.context.permissions["own_customers_only"] is False
    assert access.context.explicit_field_policy is True
    assert access.customer_count == 1
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    assert is_field_hidden(access.context, "amount_ex_tax")
    assert not is_field_hidden(access.context, "customer")
    assert ctx.permissions["data_profit"] is True  # caller/token context untouched


def test_real_empty_assignment_still_allows_page_but_plan_is_empty(db, account, monkeypatch, cerbos_url):
    _, _, ctx = account
    monkeypatch.setattr(engine, "get_settings", lambda: SimpleNamespace(cerbos_url=cerbos_url, cerbos_timeout_seconds=2.0))
    snapshot = service.sales_snapshot(db, ctx)
    assert snapshot["read"] is True
    assert snapshot["export"] is False
    assert snapshot["scope"] == "assigned_customers"
    assert snapshot["customer_count"] == 0
    assert snapshot["field_permissions"] == ["data_customer"]
    assert "尚未分配" in snapshot["reason"]
    access = service.sales_access(db, ctx)
    assert db.scalar(select(1).where(access.predicate)) is None


@pytest.mark.parametrize("invalid_fields", [["unknown"], ["data_profit"], [False], ["data_customer", "data_customer"]])
def test_corrupt_grant_does_not_fall_back_to_legacy_permissions(db, account, monkeypatch, invalid_fields):
    _, grant, ctx = account
    grant.field_permissions = invalid_fields
    db.flush()
    monkeypatch.setattr(engine, "plan_sales", lambda *a: pytest.fail("invalid grant reached PDP"))
    with pytest.raises(HTTPException) as exc:
        service.sales_access(db, ctx)
    assert exc.value.status_code == 403


def test_missing_grant_and_unregistered_action_are_denied(db, account, monkeypatch):
    _, grant, ctx = account
    monkeypatch.setattr(engine, "plan_sales", lambda *a: pytest.fail("missing grant reached PDP"))
    with pytest.raises(HTTPException) as exc:
        service.sales_access(db, ctx, "delete")
    assert exc.value.status_code == 403
    db.delete(grant)
    db.flush()
    with pytest.raises(HTTPException) as exc:
        service.sales_access(db, ctx)
    assert exc.value.status_code == 403


def test_engine_unavailable_is_503_without_legacy_fallback(db, account, monkeypatch):
    _, _, ctx = account
    def unavailable(*args):
        raise engine.AuthorizationUnavailable("synthetic outage")
    monkeypatch.setattr(engine, "plan_sales", unavailable)
    monkeypatch.setattr(engine, "check_sales", unavailable)
    for call in (service.sales_access, service.sales_snapshot):
        with pytest.raises(HTTPException) as exc:
            call(db, ctx)
        assert exc.value.status_code == 503


def test_disabled_module_does_not_query_identity_or_pdp(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(authz_sales_analysis_enabled=False))
    ctx = UserContext(None, "admin")
    snapshot = service.sales_snapshot(None, ctx)
    assert snapshot["enabled"] is False
    assert snapshot["field_permissions"] == []
    with pytest.raises(HTTPException) as exc:
        service.sales_access(None, ctx)
    assert exc.value.status_code == 503


@pytest.mark.parametrize("module_enabled", [False, True])
def test_suspension_keeps_new_chain_authoritative_and_does_not_query(monkeypatch, module_enabled):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(
        authz_sales_analysis_enabled=module_enabled, authz_sales_analysis_suspended=True,
    ))
    ctx = UserContext(None, "admin")
    assert service.enabled() is True
    snapshot = service.sales_snapshot(None, ctx)
    assert snapshot["enabled"] is True
    assert snapshot["read"] is False
    assert "暂停" in snapshot["reason"]
    with pytest.raises(HTTPException) as exc:
        service.sales_access(None, ctx)
    assert exc.value.status_code == 503


def test_read_lock_keeps_grant_and_assignments_coherent_until_request_finishes(db, account):
    user, _, ctx = account
    user_id = user.id
    customers = [DimCustomer(name_raw="lock-customer-a"), DimCustomer(name_raw="lock-customer-b")]
    db.add_all(customers)
    db.flush()
    customer_ids = [customer.id for customer in customers]
    db.add(SysCustomerAssignment(user_id=user_id, customer_id=customer_ids[0]))
    db.commit()
    loaded = service._load(db, ctx)  # Holds FOR SHARE through the request transaction.
    assert loaded.customer_ids == (customer_ids[0],)
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
        assert waiting, f"writer must wait for the reader's authorization snapshot: {failures}"
        assert loaded.grant.field_permissions == ("data_customer",)
        assert loaded.customer_ids == (customer_ids[0],)
    finally:
        db.rollback()  # Request end releases the shared account lock.
        writer.join(timeout=8)
    assert not writer.is_alive()
    assert not failures
    with pytest.raises(HTTPException) as exc:
        service._load(db, ctx)
    assert exc.value.status_code == 403  # Old token was revoked atomically.
    refreshed = service._load(db, replace(ctx, token_version=4))
    assert refreshed.grant.field_permissions == ("data_purchase_cost",)
    assert refreshed.customer_ids == (customer_ids[1],)
