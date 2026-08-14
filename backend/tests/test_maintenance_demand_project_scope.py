"""维保需求单按项目负责人范围隔离的端到端契约（PR2）。

- admin 看全部（含未归属需求单）
- 维保负责人只看本人项目下、已归属的需求单
- 无项目负责人看空列表
- 删除意图只能覆盖本人项目需求单，越界返回 403
"""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
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


# ── TOCTOU：执行时重新鉴权（PR2 审查修复）──────────────────────────────

def _create_armed_intent(client, source_order_ids: list[str], idempotency: str) -> tuple[str, str]:
    """Create + arm a delete intent; returns (intent_id, selection_digest)."""
    created = client.post(
        "/api/maintenance/demands/delete-intents",
        json={
            "source_order_ids": source_order_ids,
            "reason": "合成 TOCTOU 测试删除",
            "idempotency_key": idempotency,
        },
    )
    assert created.status_code == 201, created.text
    intent_id = created.json()["intent_id"]
    digest = created.json()["selection_digest"]
    armed = client.post(
        f"/api/maintenance/demands/delete-intents/{intent_id}/arm",
        json={"digest": digest},
    )
    assert armed.status_code == 200, armed.text
    return intent_id, digest


def test_execute_after_revocation_is_forbidden_with_zero_tombstones(db, seeded):
    """创建意图后被撤权：execute 时重新鉴权返回 403，且不产生任何 tombstone。"""
    from app.models.maintenance import MaintenanceDemandTombstone
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from app.services import maintenance_project_assignments as assignments

    from app.services import maintenance_demands as svc

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    intent = svc.create_delete_intent(
        db,
        source_order_ids=["RAW-OWN-001"],
        reason="合成 TOCTOU 撤权删除",
        idempotency_key="toctou-revoke-delete",
        operated_by="scope_manager",
        now=now,
    )
    db.commit()
    svc.arm_delete_intent(
        db,
        intent_id=intent["intent_id"],
        digest=intent["selection_digest"],
        operated_by="scope_manager",
        now=now,
    )
    db.commit()

    # 撤权：经服务层归档负责人指派（模拟管理员改派）
    assignment = db.scalar(
        select(MaintenanceProjectUserAssignment).where(
            MaintenanceProjectUserAssignment.project_id == seeded["own"].project_id,
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
    )
    assert assignment is not None
    assignments.archive_primary_manager(
        db,
        assignment_id=assignment.assignment_id,
        version=assignment.version,
        reason="TOCTOU 测试撤权",
        operated_by="scope_admin",
    )
    db.commit()

    from app.services.maintenance_demands import MaintenanceDemandForbidden
    try:
        svc.execute_delete_intent(
            db,
            intent_id=intent["intent_id"],
            digest=intent["selection_digest"],
            operated_by="scope_manager",
            allowed_project_ids=set(),
            now=now + timedelta(seconds=7),
        )
        raise AssertionError("撤权后 execute 应被拒绝")
    except MaintenanceDemandForbidden:
        db.rollback()

    tombstones = list(
        db.scalars(
            select(MaintenanceDemandTombstone).where(
                MaintenanceDemandTombstone.source_order_id == "RAW-OWN-001"
            )
        ).all()
    )
    assert tombstones == []


def test_execute_after_reassignment_is_conflict_with_zero_tombstones(db, seeded):
    """创建意图后需求单被改派到他人项目：digest 变化 → 409 conflicted 且零删除。"""
    from app.models.maintenance import MaintenanceDemandTombstone
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment

    from app.services import maintenance_demands as svc

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    intent = svc.create_delete_intent(
        db,
        source_order_ids=["RAW-OWN-001"],
        reason="合成 TOCTOU 改派删除",
        idempotency_key="toctou-reassign-delete",
        operated_by="scope_manager",
        now=now,
    )
    db.commit()
    svc.arm_delete_intent(
        db,
        intent_id=intent["intent_id"],
        digest=intent["selection_digest"],
        operated_by="scope_manager",
        now=now,
    )
    db.commit()

    # 改派：admin 经 assign API 把 RAW-OWN-001 归属切到他人项目（服务层归档+新建）
    old_link = db.scalar(
        select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == "RAW-OWN-001",
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    )
    assert old_link is not None
    reassigned = seeded["admin_client"].post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": seeded["other"].project_id,
            "items": [
                {
                    "source_order_id": "RAW-OWN-001",
                    "expected_assignment_id": old_link.assignment_id,
                    "expected_version": old_link.version,
                }
            ],
            "reason": "TOCTOU 测试改派",
        },
    )
    assert reassigned.status_code == 200, reassigned.text

    from app.services.maintenance_demands import DeleteIntentConflict
    try:
        svc.execute_delete_intent(
            db,
            intent_id=intent["intent_id"],
            digest=intent["selection_digest"],
            operated_by="scope_manager",
            allowed_project_ids=None,
            now=now + timedelta(seconds=7),
        )
        raise AssertionError("改派后 execute 应产生 digest 冲突")
    except DeleteIntentConflict:
        db.rollback()

    tombstones = list(
        db.scalars(
            select(MaintenanceDemandTombstone).where(
                MaintenanceDemandTombstone.source_order_id == "RAW-OWN-001"
            )
        ).all()
    )
    assert tombstones == []
