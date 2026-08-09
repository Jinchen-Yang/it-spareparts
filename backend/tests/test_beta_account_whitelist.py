"""Account-scoped Beta allowlists override only the two Beta page keys for admins."""

from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app import config, permissions, security
from app.auth import hash_password
from app.api.replenishment import (
    _beta_page_whitelist,
    router as replenishment_router,
)
from app.beta_access import beta_feature_availability
from app.config import get_settings
from app.main import app
from app.models.system import SysAuditLog, SysUser


_PASSWORD = "synthetic-beta-admin-password"
_BETA_KEYS = ("page_maintenance_beta", "page_replenishment_beta")


def _closed_admin_snapshot() -> dict[str, bool]:
    graph = permissions.effective("admin", None)
    for key in _BETA_KEYS:
        graph[key] = False
    return graph


def _named_admin(db, username: str) -> SysUser:
    user = SysUser(
        username=username,
        password_hash=hash_password(_PASSWORD),
        role="admin",
        display_name=username,
        is_active=True,
        template_code="admin",
        template_version=1,
        template_perms=_closed_admin_snapshot(),
        perm_overrides={},
    )
    db.add(user)
    db.flush()
    user.permissions = permissions.effective_for_user(user)
    return user


def _login(username: str) -> tuple[TestClient, dict]:
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    client.headers["Authorization"] = f"Bearer {payload['token']}"
    return client, payload


def test_named_admin_keeps_normal_admin_power_but_beta_pages_fail_closed():
    class Admin:
        role = "admin"
        permissions = {"data_supplier": False, "page_accounts": False}
        template_perms = _closed_admin_snapshot()
        perm_overrides = {}

    effective = permissions.effective_for_user(Admin())

    assert effective["data_supplier"] is True
    assert effective["page_accounts"] is True
    assert effective["action_account_manage"] is True
    assert effective["action_replenishment_review"] is True
    assert effective["page_maintenance_beta"] is False
    assert effective["page_replenishment_beta"] is False
    assert permissions.page_permission_allowed(
        role="admin",
        permission_map=effective,
        page_key="page_accounts",
    ) is True
    assert permissions.page_permission_allowed(
        role="admin",
        permission_map=effective,
        page_key="page_maintenance_beta",
    ) is False


def test_beta_page_dependencies_do_not_short_circuit_for_admin_context():
    ctx = security.UserContext(
        user_id="named-admin",
        role="admin",
        permissions=_closed_admin_snapshot(),
        is_authenticated=True,
    )

    beta_gate = security.require_page("page_replenishment_beta")
    with pytest.raises(HTTPException) as exc_info:
        beta_gate(ctx)
    assert exc_info.value.status_code == 403

    assert security.require_page("page_accounts")(ctx) is None


def test_account_center_can_auditably_whitelist_a_different_named_admin(db):
    operator = _named_admin(db, "beta-allowlist-operator")
    pilot = _named_admin(db, "beta-allowlist-pilot")
    db.commit()

    operator_client, _ = _login(operator.username)
    before_client, before_login = _login(pilot.username)
    assert before_login["beta_features"] == {
        "maintenance": False,
        "replenishment": False,
    }

    settings = get_settings()
    original_maintenance = settings.maintenance_beta_enabled
    original_replenishment = settings.replenishment_beta_enabled
    try:
        settings.maintenance_beta_enabled = True
        settings.replenishment_beta_enabled = True
        assert before_client.get("/api/auth/beta-features").status_code == 200
        assert before_client.get("/api/replenishment-beta/capabilities").status_code == 403
        account_row = next(
            row
            for row in operator_client.get("/api/accounts").json()
            if row["username"] == pilot.username
        )
        assert account_row["permission_combo_errors"] == []

        response = operator_client.put(
            f"/api/accounts/{pilot.username}",
            json={
                "overrides": {
                    "page_maintenance_beta": True,
                    "page_replenishment_beta": True,
                }
            },
        )
        assert response.status_code == 200, response.text
        assert all(response.json()["permissions"][key] for key in _BETA_KEYS)

        # Permission changes revoke the old token.  A fresh login receives the
        # account-bound capability snapshot instead of an admin-role bypass.
        assert before_client.get("/api/auth/beta-features").status_code == 401
        pilot_client, pilot_login = _login(pilot.username)
        assert pilot_login["beta_features"] == {
            "maintenance": True,
            "replenishment": True,
        }
        assert pilot_client.get("/api/replenishment-beta/capabilities").status_code == 200

        audit = db.scalar(
            select(SysAuditLog)
            .where(
                SysAuditLog.entity_type == "sys_user",
                SysAuditLog.entity_id == pilot.id,
                SysAuditLog.action == "account_update",
            )
            .order_by(SysAuditLog.id.desc())
        )
        assert audit is not None
        assert audit.operated_by == operator.username
        assert audit.before_json["overrides"] == {}
        assert audit.after_json["overrides"] == {
            "page_maintenance_beta": True,
            "page_replenishment_beta": True,
        }
    finally:
        settings.maintenance_beta_enabled = original_maintenance
        settings.replenishment_beta_enabled = original_replenishment


def test_feature_snapshot_requires_explicit_page_bits_even_for_admin():
    graph = _closed_admin_snapshot()
    settings = get_settings()
    original_maintenance = settings.maintenance_beta_enabled
    original_replenishment = settings.replenishment_beta_enabled
    try:
        settings.maintenance_beta_enabled = True
        settings.replenishment_beta_enabled = True
        assert beta_feature_availability(
            role="admin",
            permission_map=graph,
            real_identity=True,
            settings=settings,
        ) == {"maintenance": False, "replenishment": False}
        graph.update(
            page_maintenance_beta=True,
            page_replenishment_beta=True,
        )
        assert beta_feature_availability(
            role="admin",
            permission_map=graph,
            real_identity=True,
            settings=settings,
        ) == {"maintenance": True, "replenishment": True}
    finally:
        settings.maintenance_beta_enabled = original_maintenance
        settings.replenishment_beta_enabled = original_replenishment


def test_replenishment_allowlist_cannot_be_bypassed_by_legacy_rbac_switch(
    db,
    monkeypatch,
):
    admin = _named_admin(db, "beta-rbac-bypass-admin")
    db.commit()
    client, _ = _login(admin.username)
    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        monkeypatch.setattr(config, "ENABLE_RBAC", False)
        assert client.get("/api/replenishment-beta/capabilities").status_code == 403
        assert client.get("/api/replenishment-beta/catalog").status_code == 403
    finally:
        settings.replenishment_beta_enabled = original


def test_every_human_replenishment_route_has_account_allowlist_dependency():
    exceptions: list[APIRoute] = []
    for route in replenishment_router.routes:
        if not isinstance(route, APIRoute):
            continue
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        if route.path.endswith("/review-results"):
            exceptions.append(route)
            assert _beta_page_whitelist not in dependencies
        else:
            assert _beta_page_whitelist in dependencies, route.path

    assert len(exceptions) == 1
    assert exceptions[0].methods == {"POST"}
