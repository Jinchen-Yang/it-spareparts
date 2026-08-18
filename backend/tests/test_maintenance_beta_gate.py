"""Stable/Beta release boundary for the maintenance workspace."""

from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.maintenance_beta import require_maintenance_beta
from app.maintenance_boss import require_maintenance_boss
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
        "action_maintenance_collection_follow_up",
        "action_maintenance_collection_plan_import",
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
        for stable_path in ("/api/maintenance/projects",):
            stable = client.get(stable_path)
            assert stable.status_code == 200, (stable_path, stable.text)
        # /projects/stable（基础信息 CRUD）自 2026-08-17 起随 boss 总闸而非 beta
        # ——见本文件 test_boss_gate_keeps_panel_routers_live 与 main.py 挂载注释。
        for method, path, body in (
            ("get", "/api/maintenance/projects/stable/operations", None),
            ("post", "/api/maintenance/demands/search", {}),
        ):
            beta = (
                getattr(client, method)(path, json=body)
                if body is not None
                else getattr(client, method)(path)
            )
            assert beta.status_code == 404, (method, path, beta.text)

        settings.maintenance_beta_enabled = True
        for method, path, body in (
            ("get", "/api/maintenance/projects/stable/operations", None),
            ("post", "/api/maintenance/demands/search", {}),
        ):
            opened = (
                getattr(client, method)(path, json=body)
                if body is not None
                else getattr(client, method)(path)
            )
            assert opened.status_code == 200, (method, path, opened.text)
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
        denied = stable_only.post(beta_path, json={})
        assert denied.status_code == 403
        assert "未获得维保管理页面权限" in denied.text
        assert whitelisted.post(beta_path, json={}).status_code == 200
    finally:
        settings.maintenance_beta_enabled = original


def test_login_capability_requires_server_gate_and_real_whitelisted_account(db):
    real = _client(db, username="maintenance-beta-capability", role="admin")
    settings = get_settings()
    original_maintenance = settings.maintenance_beta_enabled
    original_replenishment = settings.replenishment_beta_enabled
    # 维保展示板（plan v1.3）与两个 Beta 同为服务端总闸；本用例聚焦 Beta 两闸，
    # 展示板闸恒关，其能力键随之恒 False。
    original_boss = settings.maintenance_boss_dashboard_enabled
    try:
        settings.maintenance_beta_enabled = False
        settings.replenishment_beta_enabled = False
        settings.maintenance_boss_dashboard_enabled = False
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
            "maintenance_boss": False,
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
            "maintenance_boss": False,
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
            "maintenance_boss": False,
        }
        shared_client = TestClient(app)
        shared_client.headers["Authorization"] = f"Bearer {shared.json()['token']}"
        assert shared_client.get("/api/auth/beta-features").json() == {
            "maintenance": False,
            "replenishment": False,
            "maintenance_boss": False,
        }
        denied = shared_client.post("/api/maintenance/demands/search", json={})
        assert denied.status_code == 403, denied.text
    finally:
        settings.maintenance_beta_enabled = original_maintenance
        settings.replenishment_beta_enabled = original_replenishment
        settings.maintenance_boss_dashboard_enabled = original_boss


def test_every_beta_router_fails_closed_before_business_lookup(db):
    client = _client(db, username="maintenance-beta-router-admin", role="admin")
    settings = get_settings()
    original = settings.maintenance_beta_enabled
    try:
        settings.maintenance_beta_enabled = False
        # /projects/stable 与 /project-assignments/orders 已迁 boss 闸（面板依赖），
        # 不在本清单——它们的 fail-closed 看守见 test_boss_gate_keeps_panel_routers_live。
        calls = (
            ("get", "/api/maintenance/projects/stable/operations", None),
            ("get", "/api/maintenance/project-manager/workbooks/v3/status", None),
            ("get", "/api/maintenance/return-categories", None),
            ("get", "/api/maintenance/projects/stable/not-real/workbook", None),
            ("post", "/api/maintenance/demands/search", {}),
            ("post", "/api/maintenance/warehouse-documents/search", {}),
            ("post", "/api/maintenance/collection-reminders/search", {}),
            ("get", "/api/maintenance/projects/stable/not-real/collection-milestones", None),
            ("get", "/api/maintenance/collection-plan-imports/not-real/source-file", None),
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


def test_every_registered_beta_route_has_the_server_gate_dependency():
    """New routes in a Beta module cannot silently bypass the release kill switch."""

    beta_endpoint_modules = {
        "app.api.maintenance_acceptance",
        "app.api.maintenance_bad_returns",
        "app.api.maintenance_collection_plan_imports",
        "app.api.maintenance_collection_reminders",
        "app.api.maintenance_demands",
        "app.api.maintenance_ckd_import",
        "app.api.maintenance_doc_import",
        "app.api.maintenance_front_stock",
        "app.api.maintenance_ledger",
        "app.api.maintenance_manager_workbooks",
        "app.api.maintenance_migration",
        "app.api.maintenance_project_assignments",
        "app.api.maintenance_project_operations",
        "app.api.maintenance_project_workbooks",
        "app.api.maintenance_warehouse",
    }
    # 面板依赖的两个模块（基础信息 CRUD、归属挂靠）随 boss 总闸——同等强度看守：
    # 新路由同样不得绕开各自的发布 kill switch（2026-08-17 迁移，main.py 挂载注释）。
    boss_endpoint_modules = {
        "app.api.maintenance_projects",
        "app.api.maintenance_source_assignments",
    }

    def _routes(modules: set[str]) -> list[APIRoute]:
        return [
            route
            for route in app.routes
            if isinstance(route, APIRoute) and route.endpoint.__module__ in modules
        ]

    def _missing(routes: list[APIRoute], gate) -> list[str]:
        return [
            f"{','.join(sorted(route.methods))} {route.path}"
            for route in routes
            if not any(
                dependency.call is gate
                for dependency in route.dependant.dependencies
            )
        ]

    beta_routes = _routes(beta_endpoint_modules)
    assert beta_routes
    assert _missing(beta_routes, require_maintenance_beta) == []

    boss_routes = _routes(boss_endpoint_modules)
    assert boss_routes
    assert _missing(boss_routes, require_maintenance_boss) == []


def test_boss_gate_keeps_panel_routers_live(db):
    """面板依赖的两个 router（基础信息 CRUD、归属挂靠）随 boss 总闸开合。

    2026-08-17 生产实发：beta 总闸按 v1.23 审计置 false 后，新面板的基础信息与
    归属挂靠整组 404「页面不存在」——两个 router 当时还挂在 beta 闸上。迁到
    boss 闸后必须双向看守：boss 关＝404（与未发布不可区分），boss 开＝可服务。
    """
    client = _client(db, username="maintenance-boss-panel-admin", role="admin")
    settings = get_settings()
    original_boss = settings.maintenance_boss_dashboard_enabled
    original_beta = settings.maintenance_beta_enabled
    calls = (
        ("get", "/api/maintenance/projects/stable", None),
        (
            "get",
            "/api/maintenance/project-assignments/orders"
            "?page=1&page_size=20&assignment_status=unassigned",
            None,
        ),
    )
    try:
        # beta 开关不影响这两组路由（这正是本次迁移要解耦的）
        settings.maintenance_beta_enabled = False

        settings.maintenance_boss_dashboard_enabled = False
        for method, path, body in calls:
            closed = (
                getattr(client, method)(path, json=body)
                if body is not None
                else getattr(client, method)(path)
            )
            assert closed.status_code == 404, (method, path, closed.text)

        settings.maintenance_boss_dashboard_enabled = True
        for method, path, body in calls:
            opened = (
                getattr(client, method)(path, json=body)
                if body is not None
                else getattr(client, method)(path)
            )
            assert opened.status_code == 200, (method, path, opened.text)
    finally:
        settings.maintenance_boss_dashboard_enabled = original_boss
        settings.maintenance_beta_enabled = original_beta
