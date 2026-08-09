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
        stable = client.get("/api/maintenance/projects")
        assert stable.status_code == 200, stable.text
        beta = client.get("/api/maintenance/projects/stable")
        assert beta.status_code == 404, beta.text

        settings.maintenance_beta_enabled = True
        opened = client.get("/api/maintenance/projects/stable")
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
        assert TestClient(app).get("/api/maintenance/projects/stable").status_code == 401
        assert stable_only.get("/api/maintenance/projects").status_code == 200
        assert stable_only.get("/api/maintenance/projects/stable").status_code == 403
        assert whitelisted.get("/api/maintenance/projects/stable").status_code == 200
    finally:
        settings.maintenance_beta_enabled = original


def test_every_beta_router_fails_closed_before_business_lookup(db):
    client = _client(db, username="maintenance-beta-router-admin", role="admin")
    settings = get_settings()
    original = settings.maintenance_beta_enabled
    try:
        settings.maintenance_beta_enabled = False
        calls = (
            ("get", "/api/maintenance/projects/stable", None),
            ("get", "/api/maintenance/projects/stable/operations", None),
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
