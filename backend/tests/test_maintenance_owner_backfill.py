"""维保负责人/销售自动回填（2026-08-21 客户反馈：导入销售订单提取销售人员列）。

覆盖三件事：
1. salesperson_modes_by_project：活单销售众数（含并列稳定）；
2. _backfill_project_owner / backfill_owner_fields：只补空、不覆盖人工编辑、
   账号匹配才建 primary_manager 指派、幂等重跑不重复建；
3. auto_assign_unassigned 端到端：挂销售合同 XSDD owner 后即带销售/负责人/
   指派回填；负责人账号搜索端点放宽到 page_maintenance。
"""

from datetime import date
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password
from app.etl import loader
from app import auth as auth_api
from app.api import maintenance_manager_directory as directory_api
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysUser
from app.security import UserContext
from app.services import maintenance_project_assignments as project_assignments
from app.services import maintenance_project_catalog as project_catalog
from app.services import maintenance_project_operations as operations
from app.services import maintenance_source_assignments as sa
from tests import factories as f

_PASSWORD = "synthetic-owner-backfill-1"


def _load_orders(db, *, project: str, n: int = 1,
                 salesperson: str | None = None,
                 sales_order: str | None = None) -> list[FMaintenanceOrder]:
    """造 n 张未归属的活需求单（salesperson 可覆盖，默认夹具值=测试销售）。"""
    batch = SysImportBatch(
        filename=f"synthetic-bf-{project}.xlsx",
        file_type="maintenance",
        file_hash=f"bf-{project}-{n}-{sales_order}".ljust(64, "0"),
        status="success",
    )
    db.add(batch)
    db.flush()
    heads = {}
    for i in range(n):
        raw_id = f"WBDD-BF-{project}-{i}"
        heads[raw_id] = f.maintenance_head(raw_id, order_no=f"NO-{project}-{i}",
                                           project=project, sales_order=sales_order)
    lines = [f.maintenance_line(rid, f"{rid}-L1", "PN-BF-001") for rid in heads]
    loader.load(db, f.maintenance_result(heads, lines), batch.id,
                date(2026, 8, 21), mode="upsert")
    db.commit()
    orders = list(db.execute(
        select(FMaintenanceOrder).where(
            FMaintenanceOrder.raw_order_id.in_(list(heads)))).scalars())
    if salesperson is not None:
        for order in orders:
            order.salesperson = salesperson
        db.commit()
    return orders


def _assign(db, order: FMaintenanceOrder, project: MaintenanceProject) -> None:
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=f"as-{order.raw_order_id}", source_order_id=order.raw_order_id,
        project_id=project.project_id, is_active=True, version=1,
        created_by="tester"))
    db.commit()


def _contract_owner_project(
    db, *, project_id: str, code: str, name: str, xsdd: str
) -> MaintenanceProject:
    """显式人工建项目 + 销售合同建立 XSDD owner（合同是唯一事实来源）。"""
    project = MaintenanceProject(
        project_id=project_id, project_code=code,
        display_name=name, lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()  # 测试会话 autoflush=False；create_contract 先按 project_id 查主档
    operations.create_contract(
        db,
        project_id=project_id,
        contract_id=f"{project_id}-contract",
        contract_no=f"XSDD-{xsdd}",
        contract_amount=Decimal("100.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="bf-test-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source="test",
        reason="销售合同先建立 XSDD owner",
        operated_by="bf-contract-setup",
    )
    db.commit()
    return project


def _admin_ctx() -> UserContext:
    return UserContext(user_id="bf-admin", role="admin", is_authenticated=True)


def _active_assignment(db, project_id: str) -> MaintenanceProjectUserAssignment | None:
    return db.scalar(
        select(MaintenanceProjectUserAssignment).where(
            MaintenanceProjectUserAssignment.project_id == project_id,
            MaintenanceProjectUserAssignment.responsibility_type == "primary_manager",
            MaintenanceProjectUserAssignment.archived_at.is_(None)))


def test_salesperson_mode_picks_majority_and_breaks_ties_stably(db):
    project = MaintenanceProject(project_id="bf-p1", project_code="BF-P1",
                                 display_name="回填众数项目", lifecycle_status="ongoing")
    db.add(project)
    db.commit()
    orders = _load_orders(db, project="回填众数项目", n=3)
    for order in orders:
        _assign(db, order, project)
    # 2 票「阿销售」对 1 票「测试销售」（夹具默认）——众数取胜
    orders[0].salesperson = "阿销售"
    orders[1].salesperson = "阿销售"
    db.commit()
    assert sa.salesperson_modes_by_project(db, [project.project_id]) \
        == {project.project_id: "阿销售"}
    # 1:1:1 三方并列时按名字码点稳定排序——同一数据两次刷新结果一致
    # （"A销售" < "B销售" < "测试销售" 的 codepoint 序；中文姓按码点不按拼音）
    orders[0].salesperson = "A销售"
    orders[1].salesperson = "B销售"
    db.commit()
    assert sa.salesperson_modes_by_project(db, [project.project_id]) \
        == {project.project_id: "A销售"}


def test_auto_assign_backfills_salesperson_manager_and_assignment(db):
    db.add(SysUser(username="sales-t", role="readonly", display_name="测试销售",
                   salesperson_name="测试销售",
                   password_hash=hash_password(_PASSWORD), is_active=True))
    db.commit()
    # 显式建项目 + 销售合同建立 XSDD owner；WBDD 凭 XSDD 挂靠
    project = _contract_owner_project(
        db, project_id="bf-p-auto", code="BF-P-AUTO",
        name="回填测试项目甲", xsdd="20260821-0001",
    )
    _load_orders(db, project="回填测试项目甲-预交付", n=2,
                 sales_order="XSDD-20260821-0001")

    result = sa.auto_assign_unassigned(db, operated_by="bf-admin",
                                       user_ctx=_admin_ctx())
    db.commit()

    assert result["created_projects"] == 0
    assert result["assigned_orders"] == 2
    assert result["sales_filled_projects"] >= 1
    assert result["manager_filled_projects"] >= 1
    assert result["assignments_created"] == 1
    # 三字段：销售、负责人原文、账号级指派
    db.refresh(project)
    assert project.salesperson == "测试销售"
    assert project.project_manager_id == "测试销售"
    user = db.scalar(select(SysUser).where(SysUser.username == "sales-t"))
    assignment = _active_assignment(db, project.project_id)
    assert assignment is not None and assignment.user_id == user.id
    assert assignment.source_manager_text == "测试销售"

    # 幂等重跑：字段已满、指派已存在，全部零增量且不重复建
    again = sa.auto_assign_unassigned(db, operated_by="bf-admin",
                                      user_ctx=_admin_ctx())
    db.commit()
    assert again["sales_filled_projects"] == 0
    assert again["manager_filled_projects"] == 0
    assert again["assignments_created"] == 0
    assert _active_assignment(db, project.project_id).assignment_id \
        == assignment.assignment_id


def test_backfill_never_overwrites_manual_values(db):
    project = MaintenanceProject(
        project_id="bf-p2", project_code="BF-P2", display_name="回填保护项目",
        lifecycle_status="ongoing", salesperson="台账销售",
        project_manager_id="手工负责人")
    db.add(project)
    db.commit()
    orders = _load_orders(db, project="回填保护项目", n=1)
    _assign(db, orders[0], project)

    result = sa.auto_assign_unassigned(db, operated_by="bf-admin",
                                       user_ctx=_admin_ctx())
    db.commit()

    # 台账/人工编辑是事实源：一个字都不动，也不越权建指派
    db.refresh(project)
    assert project.salesperson == "台账销售"
    assert project.project_manager_id == "手工负责人"
    assert _active_assignment(db, project.project_id) is None
    assert result["assignments_created"] == 0


def test_manual_salesperson_clear_blocks_owner_backfill_and_access_restoration(db):
    project = MaintenanceProject(
        project_id="bf-salesperson-override-clear",
        project_code="BF-SALESPERSON-OVERRIDE-CLEAR",
        display_name="人工清空销售回填保护项目",
        lifecycle_status="ongoing",
        salesperson="测试销售",
        project_manager_id=None,
    )
    old_salesperson = SysUser(
        username="salesperson-override-manager",
        role="readonly",
        display_name="人工清空项目负责人",
        salesperson_name="测试销售",
        password_hash=hash_password(_PASSWORD),
        is_active=True,
    )
    db.add_all([project, old_salesperson])
    db.commit()
    orders = _load_orders(db, project=project.display_name, n=1)
    _assign(db, orders[0], project)
    old_salesperson_ctx = UserContext(
        user_id=old_salesperson.username,
        role=old_salesperson.role,
        salesperson_name=old_salesperson.salesperson_name,
        permissions={"own_maintenance_projects_only": True},
        is_authenticated=True,
    )
    assert project_assignments.can_access_project(
        db,
        project_id=project.project_id,
        user_ctx=old_salesperson_ctx,
    )

    cleared = project_catalog.update_project(
        db,
        project_id=project.project_id,
        version=project.version,
        updates={"salesperson": None},
        reason="人工确认项目暂无销售人员",
        operated_by="bf-admin",
    )
    db.commit()
    assert cleared is not None
    assert cleared["salesperson"] is None
    assert cleared["salesperson_override_active"] is True
    assert cleared["version"] == 2
    assert not project_assignments.can_access_project(
        db,
        project_id=project.project_id,
        user_ctx=old_salesperson_ctx,
    )

    result = sa.auto_assign_unassigned(
        db,
        operated_by="bf-admin",
        user_ctx=_admin_ctx(),
    )
    db.commit()

    db.refresh(project)
    assert project.salesperson is None
    assert project.salesperson_override_active is True
    assert project.project_manager_id is None
    assert result["sales_filled_projects"] == 0
    assert result["manager_filled_projects"] == 0
    assert result["assignments_created"] == 0
    assert _active_assignment(db, project.project_id) is None
    assert project.version == 2
    assert not project_assignments.can_access_project(
        db,
        project_id=project.project_id,
        user_ctx=old_salesperson_ctx,
    )
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    assert state is not None
    assert state.revision == 1


def test_complete_override_project_is_not_an_auto_backfill_candidate(db):
    project = MaintenanceProject(
        project_id="bf-salesperson-override-complete",
        project_code="BF-SALESPERSON-OVERRIDE-COMPLETE",
        display_name="人工清空销售且已有负责人项目",
        lifecycle_status="ongoing",
        salesperson=None,
        salesperson_override_active=True,
        project_manager_id="人工负责人",
    )
    db.add(project)
    db.commit()
    orders = _load_orders(db, project=project.display_name, n=1)
    _assign(db, orders[0], project)

    result = sa.auto_assign_unassigned(
        db,
        operated_by="bf-admin",
        user_ctx=_admin_ctx(),
    )
    db.commit()

    assert result["sales_filled_projects"] == 0
    assert result["manager_filled_projects"] == 0
    assert result["assignments_created"] == 0
    assert db.get(MaintenanceProjectWorkbookState, project.project_id) is None


def test_backfill_without_matching_account_keeps_text_only(db):
    # 有销售众数、但没有 salesperson_name 对齐的账号：只回填文本，不建指派
    project = _contract_owner_project(
        db, project_id="bf-p-noacct", code="BF-P-NOACCT",
        name="回填无账号项目", xsdd="20260821-0002",
    )
    _load_orders(db, project="回填无账号项目", n=1,
                 sales_order="XSDD-20260821-0002")
    result = sa.auto_assign_unassigned(db, operated_by="bf-admin",
                                       user_ctx=_admin_ctx())
    db.commit()
    db.refresh(project)
    assert result["assigned_orders"] == 1
    assert project.salesperson == "测试销售"
    assert project.project_manager_id == "测试销售"
    assert _active_assignment(db, project.project_id) is None
    assert result["assignments_created"] == 0


def test_manager_account_search_open_to_page_maintenance(db):
    """2026-08-21：负责人搜索端点从仅 admin 放宽到 page_maintenance（下拉数据源）。"""
    db.add(SysUser(username="bf-search-user", role="readonly",
                   display_name="回填搜索账号", password_hash=hash_password(_PASSWORD),
                   is_active=True, permissions={"page_maintenance": True}))
    db.add(SysUser(username="bf-search-nopage", role="readonly",
                   display_name="回填无权限账号", password_hash=hash_password(_PASSWORD),
                   is_active=True, permissions={}))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(auth_api.router, prefix="/api")
    test_app.include_router(directory_api.router, prefix="/api")
    client = TestClient(test_app)

    def login(username: str) -> TestClient:
        resp = client.post("/api/auth/login",
                           json={"username": username, "password": _PASSWORD})
        assert resp.status_code == 200, resp.text
        client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
        return client

    resp = login("bf-search-user").post(
        "/api/maintenance/project-manager-assignments/search",
        json={"q": "", "page": 1, "page_size": 20})
    assert resp.status_code == 200, resp.text
    assert any(r["username"] == "bf-search-user" for r in resp.json()["rows"])

    resp = login("bf-search-nopage").post(
        "/api/maintenance/project-manager-assignments/search",
        json={"q": "", "page": 1, "page_size": 20})
    assert resp.status_code == 403
