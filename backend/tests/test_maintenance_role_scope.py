"""维保负责人角色 + own_maintenance_projects_only 行级隔离（2026-08-21 客户反馈）。

覆盖：模板矩阵（整套维保页面/无成本/行键开）、越权矩阵（销售 A 看不到销售 B、
负责人只见指派项目、admin/boss 全量）、boss 板/分析/验收清单读口的收敛、
未归属桶与汇总不泄露。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser
from app.security import UserContext
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_catalog as catalog

_PASSWORD = "synthetic-role-scope-1"


@pytest.fixture(autouse=True)
def _flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def _project(db, name, *, salesperson=None) -> MaintenanceProject:
    proj = MaintenanceProject(project_id=str(uuid.uuid4()), project_code=name,
                              display_name=name, lifecycle_status="ongoing",
                              salesperson=salesperson)
    db.add(proj)
    db.commit()
    return proj


def _user(db, username, *, role="readonly", salesperson_name=None, perms=None):
    user = SysUser(username=username, role=role, display_name=username,
                   salesperson_name=salesperson_name,
                   password_hash=hash_password(_PASSWORD), is_active=True,
                   permissions=perms or {})
    db.add(user)
    db.commit()
    return user


def _ctx(user, perms) -> UserContext:
    return UserContext(user_id=user.username, role=user.role,
                       salesperson_name=user.salesperson_name,
                       permissions=perms, is_authenticated=True)


def _assign_manager(db, project, user):
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        responsibility_type="primary_manager", user_id=user.id,
        source_manager_text=user.username, version=1,
        assigned_by="tester", assignment_reason="test"))
    db.commit()


# ---------------------------------------------------------------- 模板矩阵

def test_maintenance_manager_template_shape():
    tpl = permissions.ROLE_TEMPLATES["maintenance_manager"]
    assert tpl["page_maintenance"] is True
    assert tpl["own_maintenance_projects_only"] is True
    # 2026-08-24 客户拍板：验收提交开放（提交即生效）
    assert tpl["action_maintenance_acceptance_submit"] is True
    # 成本数据组全关（客户原话「这个权限还有点高」）
    assert not any(tpl.get(k) for k in permissions.DATA_GROUPS)
    # 其余页面/动作全关
    assert not any(v for k, v in tpl.items()
                   if k not in ("page_maintenance", "own_maintenance_projects_only",
                                "action_maintenance_acceptance_submit"))
    # 既有角色模板不收敛（fail-closed）——含 sales：2026-08-24 验收开放只经
    # 迁移 a9e2f7c4d1b8 改 DB 模板+账号快照，代码兜底保持历史冻结口径
    # （防漂移契约 test_frozen_templates_match_current_code）。
    for role in ("admin", "boss", "sales", "purchaser", "readonly"):
        assert permissions.ROLE_TEMPLATES[role].get(
            "own_maintenance_projects_only") is not True
    # sales 的 DB 侧开放由迁移负责；代码兜底不开放（旧 token 回退口径）。
    sales = permissions.ROLE_TEMPLATES["sales"]
    assert sales["page_maintenance"] is False
    assert sales["own_maintenance_projects_only"] is False
    assert sales["action_maintenance_acceptance_submit"] is False


# ---------------------------------------------------------------- 越权矩阵

def test_scope_union_salesperson_and_assignment(db):
    proj_a = _project(db, "销售A项目", salesperson="销售A")
    proj_b = _project(db, "销售B项目", salesperson="销售B")
    proj_c = _project(db, "负责人项目")
    proj_other = _project(db, "无关项目")

    sales_a = _user(db, "sales-a", salesperson_name="销售A")
    manager = _user(db, "mgr-1")
    _assign_manager(db, proj_c, manager)
    admin = _user(db, "adm-1", role="admin")

    row_key = {"own_maintenance_projects_only": True}

    # 销售A：只见自己销售的项目
    ctx = _ctx(sales_a, row_key)
    assert assignments.maintenance_scope_project_ids(db, ctx) == {proj_a.project_id}
    assert assignments.can_access_project(db, project_id=proj_a.project_id, user_ctx=ctx)
    assert not assignments.can_access_project(db, project_id=proj_b.project_id, user_ctx=ctx)

    # 维保负责人（有指派，无销售名）：只见指派项目
    ctx = _ctx(manager, row_key)
    assert assignments.maintenance_scope_project_ids(db, ctx) == {proj_c.project_id}
    assert assignments.can_access_project(db, project_id=proj_c.project_id, user_ctx=ctx)
    assert not assignments.can_access_project(db, project_id=proj_other.project_id, user_ctx=ctx)

    # admin：全量（FULL_SCOPE 双保险——即使误开行键也不收敛）
    ctx = _ctx(admin, {"own_maintenance_projects_only": True})
    assert assignments.maintenance_scope_project_ids(db, ctx) is None

    # 未开行键：维持 #205 挂靠口径（销售A看不到销售B，也看不到自己销售的项目——
    # 只有挂靠负责人才可见）
    ctx = _ctx(sales_a, {"own_maintenance_projects_only": False})
    assert assignments.maintenance_scope_project_ids(db, ctx) is None
    assert not assignments.can_access_project(
        db, project_id=proj_b.project_id, user_ctx=ctx)

    # 开行键但两条件皆空：空集（绝不误放全量）
    nobody = _user(db, "nobody-1")
    ctx = _ctx(nobody, row_key)
    assert assignments.maintenance_scope_project_ids(db, ctx) == set()
    assert not assignments.can_access_project(
        db, project_id=proj_a.project_id, user_ctx=ctx)


def test_explicit_salesperson_clear_removes_sales_scope(db):
    project = _project(db, "销售范围人工清空项目", salesperson="销售A")
    salesperson = _user(db, "sales-clear-scope", salesperson_name="销售A")
    ctx = _ctx(salesperson, {"own_maintenance_projects_only": True})
    assert assignments.maintenance_scope_project_ids(db, ctx) == {
        project.project_id
    }

    changed = catalog.update_project(
        db,
        project_id=project.project_id,
        version=project.version,
        updates={"salesperson": None},
        reason="人工确认项目暂无销售人员",
        operated_by="scope-admin",
    )
    db.commit()

    assert changed is not None
    assert changed["salesperson"] is None
    assert changed["salesperson_override_active"] is True
    assert assignments.maintenance_scope_project_ids(db, ctx) == set()
    assert not assignments.can_access_project(
        db,
        project_id=project.project_id,
        user_ctx=ctx,
    )


def test_boss_board_projects_scoped_by_row_key(db):
    proj_a = _project(db, "板销售A项目", salesperson="销售A")
    _project(db, "板销售B项目", salesperson="销售B")
    user = _user(db, "board-sales-a", salesperson_name="销售A", perms={
        "page_maintenance": True, "own_maintenance_projects_only": True})
    client = TestClient(app)
    resp = client.post("/api/auth/login",
                       json={"username": user.username, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    body = client.get("/api/maintenance/boss-board/projects").json()
    names = [r["display_name"] for r in body["rows"]]
    assert names == ["板销售A项目"]
    assert body["total"] == 1
    # 未归属桶不出现（无主数据不属于任何人）
    assert all(r["project_id"] != "unassigned" for r in body["rows"])
    # 需关注队列同样收敛：范围内没有需关注项目 → 空队列（不泄露他人项目）
    attention = client.get("/api/maintenance/boss-board/attention").json()
    assert attention["items"] == []


def test_boss_board_full_scope_unchanged_without_row_key(db):
    _project(db, "全量甲")
    _project(db, "全量乙")
    user = _user(db, "board-plain", perms={"page_maintenance": True})
    client = TestClient(app)
    resp = client.post("/api/auth/login",
                       json={"username": user.username, "password": _PASSWORD})
    assert resp.status_code == 200
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    body = client.get("/api/maintenance/boss-board/projects").json()
    # M0-B 口径不变：能进页面即全量（含未归属桶）
    assert body["total"] == 2
    assert any(r["project_id"] == "unassigned" for r in body["rows"])


def test_pn_ranking_scoped_by_row_key(db):
    from datetime import date

    from sqlalchemy import select

    from app.etl import loader
    from app.models.maintenance import FMaintenanceOrder
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )
    from app.models.system import SysImportBatch
    from tests import factories as f

    proj_a = _project(db, "析销售A项目", salesperson="销售A")
    proj_b = _project(db, "析销售B项目", salesperson="销售B")
    batch = SysImportBatch(filename="synthetic-scope.xlsx", file_type="maintenance",
                           file_hash="scope" + "0" * 58, status="success")
    db.add(batch)
    db.flush()
    heads = {}
    for proj, rid in ((proj_a, "WBDD-SC-A"), (proj_b, "WBDD-SC-B")):
        heads[rid] = f.maintenance_head(rid, order_no=rid, project=proj.display_name)
    lines = [f.maintenance_line(rid, f"{rid}-L1", "PN-SCOPE-1") for rid in heads]
    loader.load(db, f.maintenance_result(heads, lines), batch.id,
                date(2026, 8, 21), mode="upsert")
    orders = db.execute(select(FMaintenanceOrder)).scalars().all()
    for order in orders:
        proj = proj_a if order.order_no.endswith("A") else proj_b
        db.add(MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid.uuid4()), source_order_id=order.raw_order_id,
            project_id=proj.project_id, is_active=True, version=1,
            created_by="tester"))
    db.commit()

    user = _user(db, "analytics-sales-a", salesperson_name="销售A", perms={
        "page_maintenance": True, "own_maintenance_projects_only": True})
    client = TestClient(app)
    resp = client.post("/api/auth/login",
                       json={"username": user.username, "password": _PASSWORD})
    assert resp.status_code == 200
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    body = client.get("/api/maintenance/analytics/pn-ranking",
                      params={"range": "all"}).json()
    # 只有销售A项目的行：行次数 1、项目数 1（不是 2）
    assert body["total"] == 1
    assert body["rows"][0]["project_count"] == 1
    assert body["summary"]["part_count"] == 1
