"""Project-manager account assignments through their public APIs (#205)."""

from datetime import UTC, date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app import auth
from app.api import maintenance_manager_directory
from app.api import maintenance_project_assignments, maintenance_project_operations
from app.api import maintenance_projects
from app.auth import hash_password
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysAccessLog, SysUser
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.services import maintenance_project_operations as operations_service


_PASSWORD = "synthetic-password-123"


def _admin_client(db, *, username: str) -> TestClient:
    db.add(
        SysUser(
            username=username,
            role="admin",
            display_name="合成维保管理员",
            password_hash=hash_password(_PASSWORD),
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_assignments.router, prefix="/api")
    app.include_router(maintenance_manager_directory.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
    app.include_router(maintenance_projects.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _role_client(db, *, username: str, role: str) -> TestClient:
    db.add(
        SysUser(
            username=username,
            role=role,
            display_name=f"合成{role}账号",
            password_hash=hash_password(_PASSWORD),
        )
    )
    db.commit()
    return _existing_user_client(db, username=username)


def _existing_user_client(db, *, username: str) -> TestClient:
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_assignments.router, prefix="/api")
    app.include_router(maintenance_manager_directory.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
    app.include_router(maintenance_projects.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def test_source_manager_text_never_auto_maps_and_explicit_assignment_reaches_card(db):
    project = MaintenanceProject(
        project_id="project-manager-explicit-map",
        project_code="PM-EXPLICIT-MAP",
        display_name="合成负责人映射项目",
        project_manager_id="同名来源负责人",
        lifecycle_status="missing",
    )
    first = SysUser(
        username="manager_candidate_first",
        role="purchaser",
        display_name="同名来源负责人",
        password_hash=hash_password(_PASSWORD),
    )
    second = SysUser(
        username="manager_candidate_second",
        role="purchaser",
        display_name="同名来源负责人",
        password_hash=hash_password(_PASSWORD),
    )
    db.add_all([project, first, second])
    db.commit()
    client = _admin_client(db, username="manager_assignment_admin")

    before = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={"q": "PM-EXPLICIT-MAP"},
    )
    assert before.status_code == 200, before.text
    assert before.json()["rows"][0]["manager_assignment"] is None
    assert {
        task["owner"]
        for task in before.json()["rows"][0]["task_summary"]["rows"]
    } == {None}

    assigned = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/manager-assignment",
        json={
            "user_id": first.id,
            "expected_assignment_id": None,
            "expected_assignment_version": None,
            "reason": "经项目主档确认后显式指定主负责人",
        },
    )

    assert assigned.status_code == 201, assigned.text
    assert assigned.json() == {
        "assignment_id": assigned.json()["assignment_id"],
        "project_id": project.project_id,
        "responsibility_type": "primary_manager",
        "user_id": first.id,
        "username": first.username,
        "display_name": first.display_name,
        "account_status": "active",
        "source_manager_text": "同名来源负责人",
        "version": 1,
        "assigned_at": assigned.json()["assigned_at"],
        "archived_at": None,
    }
    db.refresh(project)
    assert project.project_manager_id == "同名来源负责人"

    after = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={"q": "PM-EXPLICIT-MAP"},
    )
    assert after.status_code == 200, after.text
    assert after.json()["rows"][0]["manager_assignment"] == assigned.json()
    assert {
        task["owner"]
        for task in after.json()["rows"][0]["task_summary"]["rows"]
    } == {first.username}


def test_manager_account_search_is_post_only_and_keeps_same_name_candidates_distinct(db):
    first = SysUser(
        username="manager_search_first",
        role="purchaser",
        display_name="合成同名负责人",
        password_hash=hash_password(_PASSWORD),
    )
    second = SysUser(
        username="manager_search_second",
        role="purchaser",
        display_name="合成同名负责人",
        password_hash=hash_password(_PASSWORD),
    )
    inactive = SysUser(
        username="manager_search_inactive",
        role="purchaser",
        display_name="合成同名负责人",
        password_hash=hash_password(_PASSWORD),
        is_active=False,
    )
    db.add_all([first, second, inactive])
    db.commit()
    client = _admin_client(db, username="manager_search_admin")

    get_response = client.get(
        "/api/maintenance/project-manager-assignments/search",
        params={"q": "合成同名负责人"},
    )
    assert get_response.status_code == 405

    response = client.post(
        "/api/maintenance/project-manager-assignments/search",
        json={"q": "合成同名负责人", "page": 1, "page_size": 20},
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert {
        (row["user_id"], row["username"], row["display_name"], row["is_active"])
        for row in response.json()["rows"]
    } == {
        (first.id, first.username, first.display_name, True),
        (second.id, second.username, second.display_name, True),
    }


def test_reassign_and_archive_preserve_history_reasons_and_optimistic_lock(db):
    project = MaintenanceProject(
        project_id="project-manager-reassignment",
        project_code="PM-REASSIGNMENT",
        display_name="合成负责人改派项目",
        project_manager_id="来源负责人原文",
        lifecycle_status="missing",
    )
    first = SysUser(
        username="manager_reassignment_first",
        role="purchaser",
        display_name="第一负责人",
        password_hash=hash_password(_PASSWORD),
    )
    second = SysUser(
        username="manager_reassignment_second",
        role="purchaser",
        display_name="第二负责人",
        password_hash=hash_password(_PASSWORD),
    )
    db.add_all([project, first, second])
    db.commit()
    client = _admin_client(db, username="manager_reassignment_admin")

    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/manager-assignment",
        json={
            "user_id": first.id,
            "reason": "首次确认主负责人",
        },
    )
    assert created.status_code == 201, created.text

    stale = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/manager-assignment",
        json={
            "user_id": second.id,
            "expected_assignment_id": created.json()["assignment_id"],
            "expected_assignment_version": 99,
            "reason": "使用过期版本尝试改派",
        },
    )
    assert stale.status_code == 409

    reassigned = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/manager-assignment",
        json={
            "user_id": second.id,
            "expected_assignment_id": created.json()["assignment_id"],
            "expected_assignment_version": 1,
            "reason": "项目交接后改派主负责人",
        },
    )
    assert reassigned.status_code == 201, reassigned.text
    assert reassigned.json()["user_id"] == second.id
    assert reassigned.json()["assignment_id"] != created.json()["assignment_id"]

    archived = client.post(
        "/api/maintenance/project-manager-assignments/"
        f"{reassigned.json()['assignment_id']}/archive",
        json={
            "version": 1,
            "reason": "项目暂停，暂时归档负责人关系",
        },
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert archived.json()["version"] == 2

    history = list(
        db.scalars(
            select(MaintenanceProjectUserAssignment)
            .where(MaintenanceProjectUserAssignment.project_id == project.project_id)
            .order_by(MaintenanceProjectUserAssignment.assigned_at)
        )
    )
    assert len(history) == 2
    assert history[0].user_id == first.id
    assert history[0].archive_reason == "项目交接后改派主负责人"
    assert history[1].user_id == second.id
    assert history[1].archive_reason == "项目暂停，暂时归档负责人关系"
    assert all(row.archived_at is not None for row in history)

    audits = list(
        db.scalars(
            select(MaintenanceProjectAuditLog)
            .where(
                MaintenanceProjectAuditLog.project_id == project.project_id,
                MaintenanceProjectAuditLog.entity_type == "manager_assignment",
            )
            .order_by(MaintenanceProjectAuditLog.id)
        )
    )
    assert [row.action for row in audits] == ["assign", "reassign", "archive"]
    assert [row.reason for row in audits] == [
        "首次确认主负责人",
        "项目交接后改派主负责人",
        "项目暂停，暂时归档负责人关系",
    ]
    assert {row.operated_by for row in audits} == {"manager_reassignment_admin"}


def test_project_manager_scope_filters_cards_and_denies_guessed_project_ids(db):
    own_project = MaintenanceProject(
        project_id="project-manager-own",
        project_code="PM-OWN",
        display_name="合成本人维保项目",
        project_manager_id="来源负责人甲",
        lifecycle_status="missing",
    )
    other_project = MaintenanceProject(
        project_id="project-manager-other",
        project_code="PM-OTHER",
        display_name="合成他人维保项目",
        project_manager_id="来源负责人乙",
        lifecycle_status="missing",
    )
    manager = SysUser(
        username="scoped_project_manager",
        role="purchaser",
        display_name="合成项目经理",
        password_hash=hash_password(_PASSWORD),
    )
    db.add_all([own_project, other_project, manager])
    db.commit()
    admin = _admin_client(db, username="scope_assignment_admin")
    assigned = admin.post(
        f"/api/maintenance/projects/stable/{own_project.project_id}/manager-assignment",
        json={"user_id": manager.id, "reason": "确认本人项目范围"},
    )
    assert assigned.status_code == 201, assigned.text
    client = _existing_user_client(db, username=manager.username)

    directory = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "q": "",
            "owner_scope": "me",
            "lifecycle": "all",
            "page": 1,
            "page_size": 24,
        },
    )
    assert directory.status_code == 200, directory.text
    assert directory.json()["owner_scope"] == "me"
    assert [row["project_id"] for row in directory.json()["rows"]] == [
        own_project.project_id
    ]

    master_directory = client.post(
        "/api/maintenance/projects/stable/search",
        json={"q": "PM-", "page": 1, "page_size": 50},
    )
    assert master_directory.status_code == 200, master_directory.text
    assert [row["project_id"] for row in master_directory.json()["rows"]] == [
        own_project.project_id
    ]

    escalated = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={"q": "", "owner_scope": "all", "lifecycle": "all"},
    )
    assert escalated.status_code == 403

    own_workspace = client.get(
        f"/api/maintenance/projects/stable/{own_project.project_id}/workspace",
        params={"as_of": "2026-08-09"},
    )
    assert own_workspace.status_code == 200, own_workspace.text
    assert own_workspace.json()["project"]["project_id"] == own_project.project_id
    assert own_workspace.json()["project"]["manager_assignment"]["user_id"] == manager.id

    guessed = client.get(
        f"/api/maintenance/projects/stable/{other_project.project_id}/workspace",
        params={"as_of": "2026-08-09"},
    )
    assert guessed.status_code == 403

    guessed_overview = client.get(
        f"/api/maintenance/projects/stable/{other_project.project_id}",
        params={"as_of": "2026-08-09"},
    )
    assert guessed_overview.status_code == 403


def test_task_board_keeps_incomplete_project_and_filters_generated_tasks_in_post_body(db):
    project = MaintenanceProject(
        project_id="project-task-card-incomplete",
        project_code="PM-TASK-INCOMPLETE",
        display_name="合成缺数据任务卡",
        project_manager_id="来源负责人原文",
        lifecycle_status="missing",
    )
    db.add(project)
    db.commit()
    client = _admin_client(db, username="task_card_admin")

    # 2026-08-25：「项目经理月度更新」任务退役（工作簿入口不存在，死胡同
    # 提示）——类型筛选为空；无过滤时项目仍在，卡片标签与任务列表不再
    # 出现月度/验收截止日/业务配置项。
    response = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "q": "PM-TASK-INCOMPLETE",
            "task_type": "项目经理月度更新",
            "as_of": "2026-08-09",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0

    unfiltered = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={"q": "PM-TASK-INCOMPLETE", "as_of": "2026-08-09"},
    )
    assert unfiltered.status_code == 200, unfiltered.text
    assert unfiltered.json()["total"] == 1
    card = unfiltered.json()["rows"][0]
    assert card["project_id"] == project.project_id
    assert card["manager_assignment"] is None
    assert card["missing_data_labels"] == [
        "负责人待映射",
        "维保期限待补",
        "合同额待补",
        "成本待补",
        "验收附件待上传",
    ]
    assert card["attachment_status"] == "missing"
    assert not [
        row for row in card["task_summary"]["rows"]
        if row["task_type"] == "项目经理月度更新"
    ]

    task_rules = {row["rule_key"] for row in card["task_summary"]["rows"]}
    # 2026-08-25：截止日类（missing_due）与审批类（pending_review，审批入口
    # 已删）任务退役——不再生成；未提交验收的提醒保留（新口径下唯一保留
    # 的验收提醒），标题已无「期」概念。
    assert "acceptance:missing_due" not in task_rules
    assert "acceptance:pending_review" not in task_rules
    report_due = next(
        row for row in card["task_summary"]["rows"]
        if row["rule_key"] == "acceptance:report_due"
    )
    assert report_due["title"] == "提交验收报告"

    invalid_range = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "q": "",
            "due_from": "2026-09-01",
            "due_to": "2026-08-31",
        },
    )
    assert invalid_range.status_code == 422


def test_inactive_assigned_account_is_labeled_and_loses_own_project_scope(db):
    project = MaintenanceProject(
        project_id="project-inactive-manager",
        project_code="PM-INACTIVE-MANAGER",
        display_name="合成停用负责人项目",
        project_manager_id="原始负责人",
        lifecycle_status="ongoing",
    )
    manager = SysUser(
        username="inactive_scoped_manager",
        role="purchaser",
        display_name="合成待停用负责人",
        password_hash=hash_password(_PASSWORD),
    )
    db.add_all([project, manager])
    db.commit()
    admin = _admin_client(db, username="inactive_assignment_admin")
    assigned = admin.post(
        f"/api/maintenance/projects/stable/{project.project_id}/manager-assignment",
        json={"user_id": manager.id, "reason": "先建立合成负责关系"},
    )
    assert assigned.status_code == 201, assigned.text

    manager.is_active = False
    db.commit()
    card_response = admin.post(
        "/api/maintenance/projects/stable/operations/search",
        json={"q": project.project_code},
    )
    assert card_response.status_code == 200, card_response.text
    assignment = card_response.json()["rows"][0]["manager_assignment"]
    assert assignment["username"] == manager.username
    assert assignment["account_status"] == "inactive"


def test_boss_defaults_to_all_projects_without_explicit_assignment(db):
    project = MaintenanceProject(
        project_id="project-boss-all-scope",
        project_code="PM-BOSS-ALL",
        display_name="合成老板全量范围项目",
        lifecycle_status="ongoing",
    )
    target = SysUser(
        username="boss_scope_mapping_target",
        role="purchaser",
        display_name="合成老板不可映射候选人",
        password_hash=hash_password(_PASSWORD),
    )
    db.add_all([project, target])
    db.commit()
    client = _role_client(db, username="boss_all_scope", role="boss")

    response = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={"q": project.project_code},
    )

    assert response.status_code == 200, response.text
    assert response.json()["owner_scope"] == "all"
    assert [row["project_id"] for row in response.json()["rows"]] == [
        project.project_id
    ]
    # 目录搜索 2026-08-21 起对维保页面权限开放（boss 恒有 page_maintenance）
    assert client.post(
        "/api/maintenance/project-manager-assignments/search",
        json={"q": target.username},
    ).status_code == 200
    assert client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/manager-assignment",
        json={"user_id": target.id, "reason": "老板只读全量不代表可改授权关系"},
    ).status_code == 403


def test_custom_action_permission_cannot_delegate_manager_mapping_outside_admin(db):
    project = MaintenanceProject(
        project_id="project-mapping-admin-only",
        project_code="PM-ADMIN-ONLY",
        display_name="合成仅管理员映射项目",
        lifecycle_status="ongoing",
    )
    target = SysUser(
        username="mapping_admin_only_target",
        role="purchaser",
        display_name="合成候选负责人",
        password_hash=hash_password(_PASSWORD),
    )
    delegated = SysUser(
        username="delegated_mapping_operator",
        role="purchaser",
        display_name="合成被授权非管理员",
        password_hash=hash_password(_PASSWORD),
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "data_profit": True,
            "action_maintenance_project_manage": True,
        },
    )
    db.add_all([project, target, delegated])
    db.commit()
    client = _existing_user_client(db, username=delegated.username)

    account_search = client.post(
        "/api/maintenance/project-manager-assignments/search",
        json={"q": target.display_name},
    )
    assignment = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/manager-assignment",
        json={"user_id": target.id, "reason": "尝试通过自定义 action 扩权"},
    )

    # 2026-08-21 客户反馈：负责人目录搜索迁稳定版（page_maintenance 门）——
    # 有维保页面权限的非管理员可搜索候选账号（只读目录：账号/显示名/在职状态）。
    # 改派/归档仍是 admin 专属，本测试的护栏就是下面这条。
    assert account_search.status_code == 200
    assert assignment.status_code == 403
    assert db.scalar(
        select(MaintenanceProjectUserAssignment.assignment_id).where(
            MaintenanceProjectUserAssignment.project_id == project.project_id
        )
    ) is None


def test_manager_account_overlong_search_is_generic_and_not_audited(db):
    client = _admin_client(db, username="manager_search_privacy_admin")
    sentinel = "MANAGER-PRIVATE-SENTINEL-" + "x" * 256

    response = client.post(
        "/api/maintenance/project-manager-assignments/search",
        json={"q": sentinel},
    )

    assert response.status_code == 422
    assert sentinel not in response.text
    assert "PRIVATE-SENTINEL" not in response.text
    db.expire_all()
    assert db.scalar(
        select(SysAccessLog.id).where(
            SysAccessLog.username == "manager_search_privacy_admin",
            SysAccessLog.action
            == "maintenance_project_manager_account_search",
        )
    ) is None


def test_task_summary_prioritizes_overdue_before_higher_severity_without_due_date():
    overdue = operations_service._task(
        project_id="project-task-order",
        rule_key="collection:overdue-synthetic",
        severity="info",
        title="合成逾期任务",
        detail="用于验证排序",
        due_date=date(2026, 8, 8),
    )
    critical_without_due = operations_service._task(
        project_id="project-task-order",
        rule_key="cost_ratio:red-synthetic",
        severity="critical",
        title="合成无期限紧急任务",
        detail="用于验证排序",
    )

    summary = operations_service._task_summary(
        [critical_without_due, overdue],
        as_of=date(2026, 8, 9),
    )

    assert summary["primary"]["task_id"] == overdue["task_id"]
    assert summary["primary"]["is_overdue"] is True
    assert summary["overdue_count"] == 1


def test_task_filter_paginates_in_sql_and_only_builds_the_requested_page(db):
    client = _admin_client(db, username="task_filter_paging_admin")
    db.add_all(
        MaintenanceProject(
            project_id=f"project-task-page-{index:03d}",
            project_code=f"TASK-PAGE-{index:03d}",
            display_name=f"合成任务分页项目 {index:03d}",
            lifecycle_status="ongoing",
        )
        for index in range(32)
    )
    db.commit()
    db.expunge_all()
    loaded_project_ids: list[str] = []

    def record_project_load(project, _context) -> None:
        loaded_project_ids.append(project.project_id)

    event.listen(MaintenanceProject, "load", record_project_load)
    try:
        response = client.post(
            "/api/maintenance/projects/stable/operations/search",
            json={
                "q": "",
                "task_status": "pending",
                "as_of": "2026-08-09",
                "page": 1,
                "page_size": 1,
            },
        )
    finally:
        event.remove(MaintenanceProject, "load", record_project_load)

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 32
    assert [row["project_id"] for row in response.json()["rows"]] == [
        "project-task-page-000"
    ]
    assert loaded_project_ids == ["project-task-page-000"]


def test_project_viewer_sync_and_visibility(db):
    """2026-08-25：项目级可见账号（基础信息编辑·多选）——viewer 挂靠计入
    行级隔离可见集；PATCH visible_usernames 整组同步（差量+审计+账号校验）。"""
    target = MaintenanceProject(
        project_id="project-viewer-target",
        project_code="PM-VIEW",
        display_name="合成可见性项目",
        project_manager_id="来源负责人",
        lifecycle_status="missing",
    )
    viewer = SysUser(
        username="scoped_project_viewer",
        role="purchaser",
        display_name="合成可见账号",
        password_hash=hash_password(_PASSWORD),
    )
    db.add_all([target, viewer])
    db.commit()

    admin = _admin_client(db, username="viewer_sync_admin")
    # PATCH 仅带 visible_usernames（不改主档字段）也应可用
    patched = admin.patch(
        f"/api/maintenance/projects/stable/{target.project_id}",
        json={
            "version": 1,
            "visible_usernames": [viewer.username],
            "reason": "基础信息编辑·多选可见账号",
        },
    )
    assert patched.status_code == 200, patched.text
    assert [row["username"] for row in patched.json()["visible_usernames"]] == [
        viewer.username
    ]

    # overview 回显
    overview = admin.get(
        f"/api/maintenance/projects/stable/{target.project_id}",
        params={"as_of": "2026-08-09"},
    )
    assert overview.status_code == 200, overview.text
    assert overview.json()["project"]["visible_usernames"] == [viewer.username]

    # viewer 账号（无负责人挂靠、行级隔离口径下）现在能看到该项目
    client = _existing_user_client(db, username=viewer.username)
    directory = client.post(
        "/api/maintenance/projects/stable/search",
        json={"q": "PM-VIEW", "page": 1, "page_size": 50},
    )
    assert directory.status_code == 200, directory.text
    assert [row["project_id"] for row in directory.json()["rows"]] == [
        target.project_id
    ]
    workspace = client.get(
        f"/api/maintenance/projects/stable/{target.project_id}/workspace",
        params={"as_of": "2026-08-09"},
    )
    assert workspace.status_code == 200, workspace.text

    # 整组同步：清空 → 可见性随之消失
    cleared = admin.patch(
        f"/api/maintenance/projects/stable/{target.project_id}",
        json={"version": 1, "visible_usernames": [], "reason": "清空可见账号"},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["visible_usernames"] == []

    # 账号不存在 → 400 整组拒绝
    bad = admin.patch(
        f"/api/maintenance/projects/stable/{target.project_id}",
        json={"version": 1, "visible_usernames": ["ghost-user"],
              "reason": "校验失败路径"},
    )
    assert bad.status_code == 400
    assert "ghost-user" in bad.json()["detail"]
