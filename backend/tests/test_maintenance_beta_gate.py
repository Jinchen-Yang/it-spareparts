"""Stable/Beta release boundary for the maintenance workspace."""

from fastapi.testclient import TestClient

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.system import SysUser


_PASSWORD = "synthetic-beta-password-123"


def _client(
    db,
    *,
    username: str,
    role: str = "readonly",
    overrides: dict[str, bool] | None = None,
) -> TestClient:
    base = permissions.effective(role, None)
    effective = permissions.effective_from_snapshot(base, overrides or {})
    db.add(
        SysUser(
            username=username,
            role=role,
            display_name=username,
            password_hash=hash_password(_PASSWORD),
            is_active=True,
            template_code=role,
            template_version=1,
            template_perms=base,
            perm_overrides=overrides or {},
            permissions=effective,
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def test_maintenance_beta_permission_is_additive_and_fail_closed():
    key = "page_maintenance_beta"
    assert key in permissions.PAGE_KEYS
    assert permissions.effective("admin", None)[key] is True
    for role in ("boss", "sales", "purchaser", "readonly", "guest"):
        assert permissions.effective(role, None)[key] is False

    invalid = permissions.effective("readonly", {
        "page_maintenance": False,
        key: True,
    })
    assert permissions.combo_errors(invalid)
    assert permissions.runtime_safe(invalid)[key] is False

    for action in (
        "action_maintenance_manager_workbook_apply",
        "action_maintenance_project_manage",
        "action_maintenance_demand_delete",
        "action_maintenance_site_issue_manage",
        "action_maintenance_bad_return_manage",
        "action_maintenance_acceptance_submit",
        "action_maintenance_acceptance_review",
        "action_maintenance_warehouse_manage",
        "action_maintenance_migration_review",
    ):
        assert (
            permissions.ACTION_ADDITIONAL_PAGE_DEPENDENCIES[action]
            == key
        )


def test_server_gate_keeps_stable_page_live_and_beta_closed(db):
    client = _client(db, username="maintenance-beta-admin", role="admin")
    settings = get_settings()
    original = settings.maintenance_beta_enabled
    try:
        settings.maintenance_beta_enabled = False
        for stable_path in (
            "/api/maintenance/projects",
            "/api/maintenance/projects/stable",
            "/api/maintenance/projects/stable/operations",
        ):
            stable = client.get(stable_path)
            assert stable.status_code == 200, (stable_path, stable.text)
        beta = client.post("/api/maintenance/demands/search", json={})
        assert beta.status_code == 404, beta.text

        settings.maintenance_beta_enabled = True
        opened = client.post("/api/maintenance/demands/search", json={})
        assert opened.status_code == 200, opened.text
    finally:
        settings.maintenance_beta_enabled = original


def test_beta_requires_explicit_user_whitelist_even_when_global_gate_is_open(db):
    stable_only = _client(
        db,
        username="maintenance-stable-only",
        overrides={"page_maintenance": True},
    )
    whitelisted = _client(
        db,
        username="maintenance-beta-whitelist",
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
        },
    )

    settings = get_settings()
    original = settings.maintenance_beta_enabled
    try:
        settings.maintenance_beta_enabled = True
        beta_path = "/api/maintenance/demands/search"
        assert TestClient(app).post(beta_path, json={}).status_code == 401
        assert stable_only.get("/api/maintenance/projects").status_code == 200
        assert stable_only.post(beta_path, json={}).status_code == 403
        assert whitelisted.post(beta_path, json={}).status_code == 200
    finally:
        settings.maintenance_beta_enabled = original


def test_login_capability_requires_server_gate_and_real_whitelisted_account(db):
    real = _client(db, username="maintenance-beta-capability", role="admin")
    settings = get_settings()
    original_maintenance = settings.maintenance_beta_enabled
    original_replenishment = settings.replenishment_beta_enabled
    try:
        settings.maintenance_beta_enabled = False
        settings.replenishment_beta_enabled = False
        closed = real.post(
            "/api/auth/login",
            json={
                "username": "maintenance-beta-capability",
                "password": _PASSWORD,
            },
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["beta_features"] == {
            "maintenance": False,
            "replenishment": False,
        }
        live_closed = real.get("/api/auth/beta-features")
        assert live_closed.status_code == 200, live_closed.text
        assert live_closed.headers["cache-control"] == "no-store"
        assert live_closed.json() == closed.json()["beta_features"]

        settings.maintenance_beta_enabled = True
        settings.replenishment_beta_enabled = True
        opened = real.post(
            "/api/auth/login",
            json={
                "username": "maintenance-beta-capability",
                "password": _PASSWORD,
            },
        )
        assert opened.status_code == 200, opened.text
        assert opened.json()["beta_features"] == {
            "maintenance": True,
            "replenishment": True,
        }
        live_opened = real.get("/api/auth/beta-features")
        assert live_opened.status_code == 200, live_opened.text
        assert live_opened.json() == opened.json()["beta_features"]

        shared = TestClient(app).post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        assert shared.status_code == 200, shared.text
        assert shared.json()["beta_features"] == {
            "maintenance": False,
            "replenishment": False,
        }
        shared_client = TestClient(app)
        shared_client.headers["Authorization"] = f"Bearer {shared.json()['token']}"
        assert shared_client.get("/api/auth/beta-features").json() == {
            "maintenance": False,
            "replenishment": False,
        }
        denied = shared_client.post("/api/maintenance/demands/search", json={})
        assert denied.status_code == 403, denied.text
    finally:
        settings.maintenance_beta_enabled = original_maintenance
        settings.replenishment_beta_enabled = original_replenishment


def test_every_beta_router_fails_closed_before_business_lookup(db):
    client = _client(db, username="maintenance-beta-router-admin", role="admin")
    settings = get_settings()
    original = settings.maintenance_beta_enabled
    try:
        settings.maintenance_beta_enabled = False
        for stable_path in (
            "/api/maintenance/projects/stable",
            "/api/maintenance/projects/stable/operations",
        ):
            response = client.get(stable_path)
            assert response.status_code == 200, (stable_path, response.text)
        calls = (
            ("get", "/api/maintenance/project-manager/workbooks/v3/status", None),
            ("get", "/api/maintenance/project-assignments/orders", None),
            ("get", "/api/maintenance/return-categories", None),
            ("get", "/api/maintenance/projects/stable/not-real/workbook", None),
            ("post", "/api/maintenance/demands/search", {}),
            ("post", "/api/maintenance/warehouse-documents/search", {}),
            ("post", "/api/maintenance/acceptance-deliverables/search", {}),
            ("post", "/api/maintenance/migration-runs/search", {}),
            ("post", "/api/maintenance/project-manager-assignments/search", {}),
        )
        for method, path, body in calls:
            response = (
                getattr(client, method)(path, json=body)
                if body is not None
                else getattr(client, method)(path)
            )
            assert response.status_code == 404, (method, path, response.text)
    finally:
        settings.maintenance_beta_enabled = original
