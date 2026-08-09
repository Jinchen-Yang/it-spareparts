"""Controlled stable maintenance project master writes."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.auth import _make_token, hash_password
from app.config import get_settings
from app.main import app
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectAuditLog
from app.models.system import SysUser
from app.services import maintenance_project_catalog as catalog


def _admin_client(db, username: str = "project_master_admin") -> TestClient:
    db.add(
        SysUser(
            username=username,
            role="admin",
            display_name="合成管理员",
            password_hash=hash_password("synthetic-password-123"),
            template_code="admin",
            template_version=1,
            template_perms=permissions.admin_account_defaults(),
            perm_overrides={"page_maintenance_beta": True},
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _permission_client(db, *, username: str, permissions: dict) -> TestClient:
    db.add(
        SysUser(
            username=username,
            role="boss",
            display_name="合成授权账号",
            password_hash=hash_password("synthetic-password-123"),
            permissions=permissions,
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def test_admin_can_create_project_and_read_it_back(db):
    client = _admin_client(db)

    created = client.post(
        "/api/maintenance/projects/stable",
        json={
            "project_code": "MAINT-SYNTH-MASTER-001",
            "display_name": "合成维保项目甲",
            "project_manager_id": "manager-synth-001",
            "reason": "建立合成项目主档",
        },
    )

    assert created.status_code == 201, created.text
    project = created.json()
    assert project["project_code"] == "MAINT-SYNTH-MASTER-001"
    assert project["display_name"] == "合成维保项目甲"
    assert project["project_manager_id"] == "manager-synth-001"
    assert project["is_active"] is True
    assert project["version"] == 1
    assert len(project["project_id"]) == 36

    read_back = client.get(
        f"/api/maintenance/projects/stable/{project['project_id']}"
    )
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["project"] == project
    audit = db.scalar(select(MaintenanceProjectAuditLog))
    assert audit is not None
    assert audit.project_id == project["project_id"]
    assert audit.action == "create"
    assert audit.operated_by == "project_master_admin"
    assert audit.reason == "建立合成项目主档"
    assert audit.after_json == project


def test_project_patch_is_explicit_versioned_and_stale_safe(db):
    client = _admin_client(db, "project_master_patch_admin")
    created = client.post(
        "/api/maintenance/projects/stable",
        json={
            "project_code": "MAINT-SYNTH-MASTER-PATCH",
            "display_name": "修改前名称",
            "project_manager_id": "manager-before",
            "reason": "建立待修改主档",
        },
    ).json()

    changed = client.patch(
        f"/api/maintenance/projects/stable/{created['project_id']}",
        json={
            "version": 1,
            "display_name": "修改后名称",
            "project_manager_id": None,
            "reason": "负责人变更并清空",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {
        **created,
        "display_name": "修改后名称",
        "project_manager_id": None,
        "version": 2,
    }

    stale = client.patch(
        f"/api/maintenance/projects/stable/{created['project_id']}",
        json={
            "version": 1,
            "display_name": "不应覆盖的名称",
            "reason": "过期草稿",
        },
    )
    assert stale.status_code == 409, stale.text
    read_back = client.get(
        f"/api/maintenance/projects/stable/{created['project_id']}"
    ).json()["project"]
    assert read_back == changed.json()
    audits = list(
        db.scalars(
            select(MaintenanceProjectAuditLog).order_by(
                MaintenanceProjectAuditLog.id
            )
        )
    )
    assert [row.action for row in audits] == ["create", "update"]


def test_project_patch_only_changes_explicit_fields(db):
    client = _admin_client(db, "project_master_explicit_admin")
    created = client.post(
        "/api/maintenance/projects/stable",
        json={
            "project_code": "MAINT-SYNTH-MASTER-EXPLICIT",
            "display_name": "显式字段修改前",
            "project_manager_id": "manager-must-stay",
            "reason": "建立显式字段测试主档",
        },
    ).json()

    changed = client.patch(
        f"/api/maintenance/projects/stable/{created['project_id']}",
        json={
            "version": 1,
            "display_name": "只修改项目名称",
            "reason": "名称更正",
        },
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["display_name"] == "只修改项目名称"
    assert changed.json()["project_manager_id"] == "manager-must-stay"


def test_project_archive_and_restore_preserve_history(db):
    client = _admin_client(db, "project_master_lifecycle_admin")
    created = client.post(
        "/api/maintenance/projects/stable",
        json={
            "project_code": "MAINT-SYNTH-MASTER-LIFECYCLE",
            "display_name": "待归档合成项目",
            "reason": "建立生命周期测试主档",
        },
    ).json()

    archived = client.post(
        f"/api/maintenance/projects/stable/{created['project_id']}/archive",
        json={"version": 1, "reason": "合成项目已结束，归档主档"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json() == {**created, "is_active": False, "version": 2}

    edit_archived = client.patch(
        f"/api/maintenance/projects/stable/{created['project_id']}",
        json={
            "version": 2,
            "display_name": "不允许修改",
            "reason": "归档后误改",
        },
    )
    assert edit_archived.status_code == 400

    stale_archive = client.post(
        f"/api/maintenance/projects/stable/{created['project_id']}/archive",
        json={"version": 1, "reason": "过期归档请求"},
    )
    assert stale_archive.status_code == 409

    stale_restore = client.post(
        f"/api/maintenance/projects/stable/{created['project_id']}/restore",
        json={"version": 1, "reason": "过期恢复请求"},
    )
    assert stale_restore.status_code == 409

    restored = client.post(
        f"/api/maintenance/projects/stable/{created['project_id']}/restore",
        json={"version": 2, "reason": "项目重新启用"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json() == {**created, "is_active": True, "version": 3}

    audits = list(
        db.scalars(
            select(MaintenanceProjectAuditLog).order_by(
                MaintenanceProjectAuditLog.id
            )
        )
    )
    assert [row.action for row in audits] == ["create", "archive", "restore"]


def test_project_management_permission_matrix_fails_closed(db):
    payload = {
        "project_code": "MAINT-SYNTH-PERMISSION",
        "display_name": "权限合成项目",
        "reason": "验证权限矩阵",
    }
    anonymous = TestClient(app).post(
        "/api/maintenance/projects/stable",
        json=payload,
    )
    assert anonymous.status_code == 401

    no_page = _permission_client(
        db,
        username="project_master_no_page",
        permissions={
            "page_maintenance": False,
            "page_maintenance_beta": True,
            "data_purchase_cost": True,
            "data_profit": True,
            "action_maintenance_project_manage": True,
        },
    )
    assert no_page.post("/api/maintenance/projects/stable", json=payload).status_code == 403

    no_action = _permission_client(
        db,
        username="project_master_no_action",
        permissions={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_purchase_cost": True,
            "data_profit": True,
            "action_maintenance_project_manage": False,
        },
    )
    assert no_action.post("/api/maintenance/projects/stable", json=payload).status_code == 403

    no_profit = _permission_client(
        db,
        username="project_master_no_profit",
        permissions={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_purchase_cost": True,
            "data_profit": False,
            "action_maintenance_project_manage": True,
        },
    )
    assert no_profit.post("/api/maintenance/projects/stable", json=payload).status_code == 403

    allowed = _permission_client(
        db,
        username="project_master_allowed",
        permissions={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_purchase_cost": True,
            "data_profit": True,
            "action_maintenance_project_manage": True,
        },
    )
    assert allowed.post("/api/maintenance/projects/stable", json=payload).status_code == 201


def test_project_code_is_case_insensitive_unique_and_noop_does_not_audit(db):
    client = _admin_client(db, "project_master_unique_admin")
    payload = {
        "project_code": "MAINT-SYNTH-CASE-001",
        "display_name": "大小写唯一项目",
        "project_manager_id": "manager-same",
        "reason": "建立唯一性测试项目",
    }
    created = client.post(
        "/api/maintenance/projects/stable",
        json=payload,
    )
    assert created.status_code == 201, created.text

    duplicate = client.post(
        "/api/maintenance/projects/stable",
        json={**payload, "project_code": "maint-synth-case-001"},
    )
    assert duplicate.status_code == 409, duplicate.text

    unchanged = client.patch(
        f"/api/maintenance/projects/stable/{created.json()['project_id']}",
        json={
            "version": 1,
            "display_name": "大小写唯一项目",
            "project_manager_id": "manager-same",
            "reason": "重复保存同一内容",
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json() == created.json()

    audits = list(db.scalars(select(MaintenanceProjectAuditLog)))
    assert len(audits) == 1
    assert audits[0].action == "create"


def test_shared_password_admin_is_not_a_real_project_master_operator(db):
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": get_settings().admin_password},
    )
    assert login.status_code == 200, login.text
    assert login.json()["permissions"]["action_maintenance_project_manage"] is False
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    response = client.post(
        "/api/maintenance/projects/stable",
        json={
            "project_code": "MAINT-SYNTH-SHARED-ADMIN",
            "display_name": "共享账号禁止写入",
            "reason": "验证实名门禁",
        },
    )
    assert response.status_code == 403
    assert "实名系统账号" in response.json()["detail"]


def test_legacy_shared_admin_token_cannot_become_real_after_account_is_created(db):
    # Pre-release shared-admin tokens did not carry fb/authn provenance.  Construct
    # that exact legacy shape before the same-name account exists.
    legacy_token, _ = _make_token("admin", "admin", None, token_version=0)
    db.add(
        SysUser(
            username="admin",
            role="admin",
            display_name="后建实名管理员",
            password_hash=hash_password("different-real-password-123"),
        )
    )
    db.commit()

    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {legacy_token}"
    response = client.post(
        "/api/maintenance/projects/stable",
        json={
            "project_code": "MAINT-SYNTH-SHARED-ADMIN-TRANSITION",
            "display_name": "共享令牌不得转正",
            "reason": "验证共享身份不会因同名账号后建而转为实名",
        },
    )

    assert response.status_code == 403
    assert "实名系统账号" in response.json()["detail"]
    assert db.scalar(
        select(MaintenanceProject).where(
            MaintenanceProject.project_code
            == "MAINT-SYNTH-SHARED-ADMIN-TRANSITION"
        )
    ) is None


def test_project_and_audit_are_one_transaction(db, monkeypatch):
    authenticated = _admin_client(db, "project_master_atomic_admin")

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(catalog, "_audit", fail_audit)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update(authenticated.headers)
    response = client.post(
        "/api/maintenance/projects/stable",
        json={
            "project_code": "MAINT-SYNTH-ATOMIC",
            "display_name": "原子性合成项目",
            "reason": "验证审计失败整体回滚",
        },
    )
    assert response.status_code == 500
    db.expire_all()
    assert db.scalar(
        select(MaintenanceProject).where(
            MaintenanceProject.project_code == "MAINT-SYNTH-ATOMIC"
        )
    ) is None
    assert db.scalar(select(MaintenanceProjectAuditLog)) is None
