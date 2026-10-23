"""独立项目备注复用主档权限、实名、审计和并发契约。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from app.auth import _make_token
from app.db import SessionLocal, engine
from app.main import app
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectAuditLog
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.models.system import SysUser
from app.services import maintenance_project_catalog as catalog
from tests.test_maintenance_project_foundation import _alembic_cfg
from tests.test_maintenance_project_management_api import _admin_client, _permission_client


def _project(db, username="project_note_admin"):
    client = _admin_client(db, username)
    response = client.post("/api/maintenance/projects/stable", json={
        "project_code": "PROJECT-NOTE", "display_name": "项目备注测试",
        "reason": "建立合成项目",
    })
    assert response.status_code == 201, response.text
    return client, response.json()["project_id"]


def _patch(client, pid, version=1, **updates):
    return client.patch(f"/api/maintenance/projects/stable/{pid}", json={
        "version": version, "reason": "维护项目自由备注", **updates,
    })


def test_project_note_coexists_with_no_return_note_and_reads_same_master_field(db):
    client, pid = _project(db)
    changed = _patch(client, pid, project_note="  现场曾更换机柜\n后续联系甲方  ",
                     no_return_note="硬盘免返")
    assert changed.status_code == 200, changed.text
    assert changed.json()["project_note"] == "现场曾更换机柜\n后续联系甲方"
    assert changed.json()["no_return_note"] == "硬盘免返"
    assert changed.json()["version"] == 2
    assert client.get(f"/api/maintenance/projects/stable/{pid}").json()["project"]["project_note"] == "现场曾更换机柜\n后续联系甲方"
    assert client.get("/api/maintenance/projects/stable").json()["rows"][0]["project_note"] == "现场曾更换机柜\n后续联系甲方"
    audit = db.scalar(select(MaintenanceProjectAuditLog).where(
        MaintenanceProjectAuditLog.action == "update"))
    assert audit.before_json["project_note"] is None
    assert audit.after_json["project_note"] == "现场曾更换机柜\n后续联系甲方"
    assert audit.after_json["no_return_note"] == "硬盘免返"
    assert audit.operated_by == "project_note_admin"
    assert audit.reason == "维护项目自由备注"
    assert db.scalar(select(MaintenanceProjectWorkbookState.revision)) == 1


@pytest.mark.parametrize("clear", [None, "", " \n\t "])
def test_omission_preserves_note_and_explicit_clear_and_noop_bump_once(db, clear):
    client, pid = _project(db)
    assert _patch(client, pid, project_note="原项目备注", no_return_note="原免返标注").status_code == 200
    response = _patch(client, pid, 2, display_name="更新项目名称")
    assert response.json()["project_note"] == "原项目备注"
    assert response.json()["version"] == 3
    assert _patch(client, pid, 3, project_note=" 原项目备注 ").json()["version"] == 3
    cleared = _patch(client, pid, 3, project_note=clear)
    assert cleared.json()["project_note"] is None
    assert cleared.json()["no_return_note"] == "原免返标注"
    assert cleared.json()["version"] == 4
    assert _patch(client, pid, 4, project_note=clear).json()["version"] == 4
    assert db.scalar(select(MaintenanceProjectWorkbookState.revision)) == 3
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectAuditLog).where(
        MaintenanceProjectAuditLog.action == "update")) == 3


@pytest.mark.parametrize("value,status", [
    ("中" * 500, 200), ("😀" * 500, 200),
    ("中" * 501, 422), ("😀" * 501, 422), (" " * 501, 422),
    (17, 422), ({"text": "项目备注"}, 422),
])
def test_api_counts_original_unicode_characters_and_never_truncates(db, value, status):
    client, pid = _project(db)
    response = _patch(client, pid, project_note=value)
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["project_note"] == value
    else:
        if isinstance(value, str):
            assert "项目备注最多 500 个字符" in response.text
        assert client.get(f"/api/maintenance/projects/stable/{pid}").json()["project"]["version"] == 1


def test_catalog_service_rejects_invalid_raw_text_and_length(db):
    client, pid = _project(db)
    for value in ["中" * 501, " " * 501, 17, {"text": "备注"}]:
        with pytest.raises(catalog.MaintenanceProjectCatalogError, match="最多 500 个字符"):
            catalog.update_project(db, project_id=pid, version=1,
                updates={"project_note": value}, reason="不能绕过API长度校验",
                operated_by="project_note_admin")
        db.rollback()
    assert _patch(client, pid, project_note="项目通用备注", no_return_note="中" * 2000).status_code == 200
    assert _patch(client, pid, 2, no_return_note="中" * 2001).status_code == 422


def test_unauthenticated_note_write_is_401(db):
    _, pid = _project(db)
    assert _patch(TestClient(app), pid, project_note="未登录写入").status_code == 401


@pytest.mark.parametrize("missing", ["page_maintenance", "data_profit", "action_maintenance_project_manage"])
def test_note_uses_same_master_permission_matrix(db, missing):
    client, pid = _project(db)
    perms = {"page_maintenance": True, "page_maintenance_beta": True,
             "data_purchase_cost": True, "data_profit": True,
             "action_maintenance_project_manage": True, missing: False}
    limited = _permission_client(db, username="project_note_limited", permissions=perms)
    assert _patch(limited, pid, project_note="越权备注").status_code == 403
    project = client.get(f"/api/maintenance/projects/stable/{pid}").json()["project"]
    assert project["project_note"] is None
    assert project["version"] == 1


def test_shared_identity_and_unowned_project_cannot_write_note(db):
    client, pid = _project(db)
    token, _ = _make_token("admin", "shared-admin", None, fallback=True,
                           perms={"action_maintenance_project_manage": True})
    shared = TestClient(app)
    shared.headers["Authorization"] = f"Bearer {token}"
    assert _patch(shared, pid, project_note="共享身份写入").status_code == 403
    limited = _permission_client(db, username="project_note_own_only", permissions={
        "page_maintenance": True, "data_profit": True,
        "action_maintenance_project_manage": True, "own_maintenance_projects_only": True,
    })
    account = db.scalar(select(SysUser).where(SysUser.username == "project_note_own_only"))
    account.role = "sales"
    db.commit()
    login = limited.post("/api/auth/login", json={
        "username": account.username, "password": "synthetic-password-123",
    })
    limited.headers["Authorization"] = f"Bearer {login.json()['token']}"
    assert _patch(limited, pid, project_note="越项目写入").status_code == 403
    assert client.get(f"/api/maintenance/projects/stable/{pid}").json()["project"]["project_note"] is None


@pytest.mark.parametrize("second_field", ["project_note", "no_return_note", "display_name"])
def test_concurrent_master_changes_refresh_old_snapshot_before_version_cas(db, second_field):
    client, pid = _project(db)
    barrier = Barrier(2, timeout=30)

    def save(updates):
        with SessionLocal() as session:
            assert session.get(MaintenanceProject, pid).version == 1
            barrier.wait()
            try:
                catalog.update_project(session, project_id=pid, version=1,
                    updates=updates, reason="并发维护项目备注", operated_by="project_note_admin")
                session.commit()
                return "saved"
            except catalog.MaintenanceProjectCatalogConflict:
                session.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [{"project_note": "甲的项目备注"}, {second_field: "乙的修改"}]))
    assert sorted(results) == ["conflict", "saved"]
    project = client.get(f"/api/maintenance/projects/stable/{pid}").json()["project"]
    assert project["version"] == 2
    assert _patch(client, pid, 1, project_note="旧草稿覆盖").status_code == 409
    assert db.scalar(select(MaintenanceProjectWorkbookState.revision)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectAuditLog).where(
        MaintenanceProjectAuditLog.action == "update")) == 1


def test_note_viewer_combination_bumps_once_and_archive_rejects_note(db):
    client, pid = _project(db)
    _permission_client(db, username="project_note_viewer", permissions={"page_maintenance": True})
    changed = _patch(client, pid, project_note="组合变更", no_return_note="独立标注",
                     visible_usernames=["project_note_viewer"])
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2
    assert db.scalar(select(MaintenanceProjectWorkbookState.revision)) == 1
    assert client.post(f"/api/maintenance/projects/stable/{pid}/archive", json={
        "version": 2, "reason": "归档合成项目",
    }).status_code == 200
    archived = _patch(client, pid, 3, project_note="归档后写入")
    assert archived.status_code == 400
    assert "已归档" in archived.text


def test_audit_failure_rolls_back_note_version_and_workbook_revision(db):
    client, pid = _project(db)

    def fail_audit(_mapper, _connection, target):
        if target.action == "update":
            raise RuntimeError("synthetic project note audit storage failure")

    event.listen(MaintenanceProjectAuditLog, "before_insert", fail_audit)
    try:
        safe = TestClient(app, raise_server_exceptions=False)
        safe.headers.update(client.headers)
        assert _patch(safe, pid, project_note="不得孤立保存").status_code == 500
    finally:
        event.remove(MaintenanceProjectAuditLog, "before_insert", fail_audit)
    project = client.get(f"/api/maintenance/projects/stable/{pid}").json()["project"]
    assert project["project_note"] is None
    assert project["version"] == 1
    assert db.scalar(select(MaintenanceProjectWorkbookState.revision)) is None


def test_note_migration_single_head_roundtrip_and_nonempty_downgrade_guard(db):
    client, pid = _project(db)
    # Model the legacy account present before this historical schema boundary.
    account = db.scalar(select(SysUser).where(SysUser.username == "project_note_admin"))
    account.authorization_legacy_seed_eligible = True
    db.commit()
    db.close()
    cfg = _alembic_cfg()
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert script.get_revision("a40c9e1d7b52").down_revision == "f307604fa4d7"
    try:
        command.downgrade(cfg, "f307604fa4d7")
        with engine.connect() as connection:
            columns = connection.scalars(text("SELECT column_name FROM information_schema.columns WHERE table_name='maintenance_project'")).all()
            assert "project_note" not in columns
            assert "no_return_note" in columns
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert _patch(client, pid, project_note="旧会话不能写入").status_code == 401
        login = client.post("/api/auth/login", json={
            "username": "project_note_admin", "password": "synthetic-password-123",
        })
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
        assert _patch(client, pid, project_note="有内容必须保留").status_code == 200
        with pytest.raises(DBAPIError, match="downgrade blocked.*project_note"):
            command.downgrade(cfg, "f307604fa4d7")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == heads[0]
            assert connection.scalar(text("SELECT project_note FROM maintenance_project WHERE project_id=:pid"), {"pid": pid}) == "有内容必须保留"
    finally:
        command.upgrade(cfg, "head")


@pytest.mark.parametrize("value", ["前\x00后", "垂直\x0b制表", "换页\x0c", "\x1f结尾", "非字符\ufffe", "非字符\uffff"])
def test_api_rejects_excel_illegal_control_characters(db, value):
    client, pid = _project(db)
    response = _patch(client, pid, project_note=value)
    assert response.status_code == 422, response.text
    assert "不可见控制字符" in response.text
    assert client.get(f"/api/maintenance/projects/stable/{pid}").json()["project"]["version"] == 1


def test_api_keeps_tab_newline_and_carriage_return(db):
    client, pid = _project(db)
    response = _patch(client, pid, project_note="第一行\t制表\r\n第二行")
    assert response.status_code == 200, response.text
    assert response.json()["project_note"] == "第一行\t制表\r\n第二行"


@pytest.mark.parametrize("field", ["project_note", "no_return_note", "display_name", "salesperson"])
def test_catalog_service_rejects_control_characters_on_workbook_text_fields(db, field):
    _project(db)
    pid = db.scalar(select(MaintenanceProject.project_id).where(
        MaintenanceProject.project_code == "PROJECT-NOTE"))
    with pytest.raises(catalog.MaintenanceProjectCatalogError, match="不可见控制字符"):
        catalog.update_project(db, project_id=pid, version=1,
            updates={field: "坏\x0b字符"}, reason="服务层也要拦截",
            operated_by="project_note_admin")
    db.rollback()
