"""维保需求单按项目负责人范围隔离的端到端契约（PR2）。

- admin 看全部（含未归属需求单）
- 维保负责人只看本人项目下、已归属的需求单
- 无项目负责人看空列表
- 删除意图只能覆盖本人项目需求单，越界返回 403
"""

from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import permissions
from app.auth import hash_password
from app.db import SessionLocal
from app.etl import loader
from app.main import app
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysUser
from tests import factories as f


def _account_perms(**overrides: bool) -> dict[str, bool]:
    """Snapshot-style permission map: everything open except Beta pages."""
    graph = permissions.admin_account_defaults()
    graph.update(overrides)
    return graph


_PASSWORD = "synthetic-password-123"


@pytest.fixture(autouse=True)
def _maintenance_beta_enabled(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_beta_enabled", True)


def _seed_demand(db, raw_id: str, project_name: str) -> None:
    batch = SysImportBatch(
        filename="synthetic-scope-maintenance.xlsx",
        file_type="maintenance",
        file_hash=f"synthetic-scope-{raw_id}",
        status="processing",
    )
    db.add(batch)
    db.flush()
    orders = {
        raw_id: f.maintenance_head(
            raw_id,
            order_no=f"WBDD-{raw_id}",
            on=date(2026, 8, 1),
            project=project_name,
        )
    }
    lines = [
        f.maintenance_line(
            raw_id,
            f"LINE-{raw_id}",
            f"PN-SCOPE-{raw_id}",
            description="合成范围测试备件",
        )
    ]
    loader.load(db, f.maintenance_result(orders, lines), batch.id, date(2026, 8, 1))
    batch.status = "success"
    db.commit()


def _login(username: str) -> TestClient:
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _assign_manager(
    db,
    *,
    project: MaintenanceProject,
    user: SysUser,
    assigned_by: str,
) -> None:
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id=str(uuid4()),
            project_id=project.project_id,
            responsibility_type="primary_manager",
            user_id=user.id,
            version=1,
            assigned_by=assigned_by,
            assignment_reason="合成范围测试指派",
        )
    )
    db.commit()


def _assign_source_order(
    db,
    *,
    source_order_id: str,
    project: MaintenanceProject,
    created_by: str,
) -> None:
    db.add(
        MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()),
            source_order_id=source_order_id,
            project_id=project.project_id,
            is_active=True,
            version=1,
            created_by=created_by,
        )
    )
    db.commit()


@pytest.fixture()
def seeded(db):
    own = MaintenanceProject(
        project_id="scope-own-project",
        project_code="SCOPE-OWN",
        display_name="合成本人项目",
        project_manager_id="来源负责人甲",
        lifecycle_status="ongoing",
    )
    other = MaintenanceProject(
        project_id="scope-other-project",
        project_code="SCOPE-OTHER",
        display_name="合成他人项目",
        project_manager_id="来源负责人乙",
        lifecycle_status="ongoing",
    )
    admin = SysUser(
        username="scope_admin",
        role="admin",
        display_name="合成范围管理员",
        password_hash=hash_password(_PASSWORD),
        template_code="admin",
        template_version=1,
        template_perms=_account_perms(page_maintenance_beta=True),
    )
    manager = SysUser(
        username="scope_manager",
        role="purchaser",
        display_name="合成维保负责人",
        password_hash=hash_password(_PASSWORD),
        template_code="purchaser",
        template_version=1,
        template_perms=_account_perms(page_maintenance_beta=True),
    )
    stranger = SysUser(
        username="scope_stranger",
        role="purchaser",
        display_name="合成无项目账号",
        password_hash=hash_password(_PASSWORD),
        template_code="purchaser",
        template_version=1,
        template_perms=_account_perms(page_maintenance_beta=True),
    )
    db.add_all([own, other, admin, manager, stranger])
    db.commit()

    _seed_demand(db, "RAW-OWN-001", "合成本人项目")
    _seed_demand(db, "RAW-OTHER-001", "合成他人项目")
    _seed_demand(db, "RAW-UNASSIGNED-001", "合成未归属项目")

    _assign_manager(db, project=own, user=manager, assigned_by="scope_admin")
    _assign_source_order(db, source_order_id="RAW-OWN-001", project=own, created_by="scope_admin")
    _assign_source_order(db, source_order_id="RAW-OTHER-001", project=other, created_by="scope_admin")

    return {
        "own": own,
        "other": other,
        "admin_client": _login("scope_admin"),
        "manager_client": _login("scope_manager"),
        "stranger_client": _login("scope_stranger"),
    }


def test_admin_sees_all_demands_including_unassigned(db, seeded):
    response = seeded["admin_client"].post(
        "/api/maintenance/demands/search",
        json={"q": "", "page": 1, "page_size": 50},
    )
    assert response.status_code == 200, response.text
    source_ids = {item["source_order_id"] for item in response.json()["items"]}
    assert source_ids == {"RAW-OWN-001", "RAW-OTHER-001", "RAW-UNASSIGNED-001"}


def test_manager_sees_only_demands_of_own_projects(db, seeded):
    response = seeded["manager_client"].post(
        "/api/maintenance/demands/search",
        json={"q": "", "page": 1, "page_size": 50},
    )
    assert response.status_code == 200, response.text
    source_ids = {item["source_order_id"] for item in response.json()["items"]}
    assert source_ids == {"RAW-OWN-001"}


def test_manager_without_projects_sees_empty_list(db, seeded):
    response = seeded["stranger_client"].post(
        "/api/maintenance/demands/search",
        json={"q": "", "page": 1, "page_size": 50},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


def test_manager_delete_intent_on_other_project_demand_is_forbidden(db, seeded):
    response = seeded["manager_client"].post(
        "/api/maintenance/demands/delete-intents",
        json={
            "source_order_ids": ["RAW-OTHER-001"],
            "reason": "合成越界删除尝试",
            "idempotency_key": "scope-cross-project-delete",
        },
    )
    assert response.status_code == 403


def test_manager_delete_intent_on_own_project_demand_is_allowed(db, seeded):
    response = seeded["manager_client"].post(
        "/api/maintenance/demands/delete-intents",
        json={
            "source_order_ids": ["RAW-OWN-001"],
            "reason": "合成本人项目删除",
            "idempotency_key": "scope-own-project-delete",
        },
    )
    assert response.status_code == 201, response.text


def test_manager_delete_intent_on_unassigned_demand_is_forbidden(db, seeded):
    response = seeded["manager_client"].post(
        "/api/maintenance/demands/delete-intents",
        json={
            "source_order_ids": ["RAW-UNASSIGNED-001"],
            "reason": "合成未归属删除尝试",
            "idempotency_key": "scope-unassigned-delete",
        },
    )
    assert response.status_code == 403


def test_admin_delete_intent_on_unassigned_demand_is_allowed(db, seeded):
    response = seeded["admin_client"].post(
        "/api/maintenance/demands/delete-intents",
        json={
            "source_order_ids": ["RAW-UNASSIGNED-001"],
            "reason": "合成管理员清理未归属单",
            "idempotency_key": "scope-admin-unassigned-delete",
        },
    )
    assert response.status_code == 201, response.text
