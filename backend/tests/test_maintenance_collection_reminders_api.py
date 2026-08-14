"""车道 A 目录/详情/人工操作 API 红测（Task 2 Step 2.3）。

覆盖：
- 目录：本人/全部范围、q 搜索、reminder_state 筛选、金额脱敏、服务端计数与排序。
- 详情：行字段、摘要计数、last_operation、404/403 失败关闭。
- 写操作：handle/reschedule/reopen 规则、版本冲突 409、幂等重放与 409、
  IDOR/撤权/inactive fail-closed、canary 403、admin 无显式 action 403、
  operation 账本 append-only。
- 请求 DTO：extra="forbid" 与判别字段 422。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app import auth
from app import permissions as _perms
from app.api import maintenance_collection_reminders
from app.auth import hash_password
from app.models.maintenance_manager import (
    MaintenanceCollectionMilestone,
    MaintenanceCollectionMilestoneOperation,
    MaintenanceServicePeriod,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser
from app.services import maintenance_collection_reminders as reminders_service


# ---------- 种子 ----------

@pytest.fixture(autouse=True)
def _fixed_business_today(monkeypatch):
    """API 层 as_of 来自 business_today()；固定为合成业务日保证确定性。"""
    monkeypatch.setattr(
        maintenance_collection_reminders,
        "business_today",
        lambda: date(2026, 8, 14),
    )


def _sys_user(
    db,
    *,
    username: str,
    role: str = "admin",
    follow_up_action: bool = False,
) -> SysUser:
    graph = _perms.effective(role, None)
    template = dict(graph)
    overrides = {}
    if follow_up_action:
        template["action_maintenance_collection_follow_up"] = False
        overrides["action_maintenance_collection_follow_up"] = True
    user = SysUser(
        username=username,
        role=role,
        display_name=f"合成{username}",
        password_hash=hash_password("synthetic-password-123"),
        template_perms=template,
        perm_overrides=overrides or None,
    )
    db.add(user)
    db.commit()
    return user


def _client(db, *, username: str, role: str = "admin", follow_up_action: bool = False):
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    if user is None:
        user = _sys_user(
            db,
            username=username,
            role=role,
            follow_up_action=follow_up_action,
        )
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_collection_reminders.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client, user


def _project(
    db,
    *,
    suffix: str,
    manager: SysUser | None = None,
    contract_no: str | None = None,
    lifecycle_status: str = "ongoing",
    active: bool = True,
) -> tuple[str, str]:
    project = MaintenanceProject(
        project_id=f"reminder-project-{suffix}",
        project_code=f"REM-{suffix}",
        display_name=f"合成回款项目 {suffix}",
        lifecycle_status=lifecycle_status,
        is_active=active,
    )
    db.add(project)
    db.flush()
    if manager is not None:
        db.add(
            MaintenanceProjectUserAssignment(
                assignment_id=f"reminder-assignment-{suffix}",
                project_id=project.project_id,
                responsibility_type="primary_manager",
                user_id=manager.id,
                assigned_at=datetime.now(UTC),
                assigned_by="synthetic-admin",
                assignment_reason="合成回款提醒负责人映射",
            )
        )
    contract = MaintenanceProjectContract(
        project_contract_id=f"reminder-pc-{suffix}",
        project_id=project.project_id,
        contract_id=f"reminder-contract-{suffix}",
        contract_no=contract_no or f"XS-REM-{suffix}",
        contract_amount=Decimal("100000.00"),
        contract_status="active",
        status_mapping_state="mapped",
        status_mapping_version="synthetic-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        source="synthetic-test",
    )
    db.add(contract)
    db.commit()
    return project.project_id, contract.project_contract_id


def _milestone(
    db,
    *,
    project_id: str,
    project_contract_id: str,
    milestone_id: str,
    sequence: int = 1,
    planned_date: date | None = date(2026, 9, 1),
    planned_amount: Decimal | None = Decimal("18000.00"),
    completeness_state: str = "complete",
    date_precision: str = "month",
    follow_up_status: str = "pending",
    follow_up_review_required: bool = False,
    follow_up_note: str | None = None,
    followed_up_by: int | None = None,
    followed_up_at: datetime | None = None,
    version: int = 1,
) -> MaintenanceCollectionMilestone:
    milestone = MaintenanceCollectionMilestone(
        milestone_id=milestone_id,
        project_id=project_id,
        project_contract_id=project_contract_id,
        sequence=sequence,
        planned_date=planned_date,
        planned_amount=planned_amount,
        completeness_state=completeness_state,
        source="direct_api",
        date_precision=date_precision,
        follow_up_status=follow_up_status,
        follow_up_review_required=follow_up_review_required,
        follow_up_note=follow_up_note,
        followed_up_by=followed_up_by,
        followed_up_at=followed_up_at,
        version=version,
    )
    db.add(milestone)
    db.commit()
    return milestone


def _service_period(db, *, project_id: str) -> None:
    db.add(
        MaintenanceServicePeriod(
            project_id=project_id,
            service_start=date(2026, 1, 1),
            service_end=date(2027, 1, 1),
            completeness_state="complete",
            source="direct_api",
        )
    )
    db.commit()


def _reload(db, model, pk):
    db.expire_all()
    return db.get(model, pk)


# ---------- 目录 ----------

def test_search_directory_shape_sort_counts_and_amounts(db):
    admin = _sys_user(db, username="reminder_admin", role="admin")
    project_a, pc_a = _project(db, suffix="a", manager=admin)
    project_b, pc_b = _project(db, suffix="b", manager=admin)
    _service_period(db, project_id=project_a)
    # a：一条需要复核（handled + review_required）→ 应排最前
    _milestone(
        db,
        project_id=project_a,
        project_contract_id=pc_a,
        milestone_id="rem-m-a1",
        sequence=1,
        planned_date=date(2026, 7, 1),
        follow_up_status="handled",
        follow_up_review_required=True,
        followed_up_by=admin.id,
        followed_up_at=datetime.now(UTC),
    )
    # a：一条逾期
    _milestone(
        db,
        project_id=project_a,
        project_contract_id=pc_a,
        milestone_id="rem-m-a2",
        sequence=2,
        planned_date=date(2026, 6, 1),
    )
    # b：一条本月
    _milestone(
        db,
        project_id=project_b,
        project_contract_id=pc_b,
        milestone_id="rem-m-b1",
        sequence=1,
        planned_date=date(2026, 8, 20),
    )

    client, _ = _client(db, username="reminder_admin", role="admin")
    # 默认 owner_scope=me：admin 只看到本人负责的项目
    default_response = client.post(
        "/api/maintenance/collection-reminders/search",
        json={},
    )
    assert default_response.status_code == 200, default_response.text
    default_payload = default_response.json()
    assert default_payload["owner_scope"] == "me"
    assert default_payload["allowed_owner_scopes"] == ["me", "all"]
    assert [r["project"]["project_id"] for r in default_payload["rows"]] == [
        "reminder-project-a",
        "reminder-project-b",
    ]

    response = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"owner_scope": "all"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 24
    assert payload["owner_scope"] == "all"
    assert payload["allowed_owner_scopes"] == ["me", "all"]
    assert payload["amount_visibility"] == "visible"
    assert payload["as_of"]
    assert payload["data_version"]
    assert payload["total"] == 2
    rows = payload["rows"]
    # 排序：needs_review(a) 先于 due_this_month(b)
    assert rows[0]["project"]["project_id"] == project_a
    assert rows[1]["project"]["project_id"] == project_b

    row_a = rows[0]
    assert row_a["project"]["project_code"] == "REM-a"
    assert row_a["project"]["display_name"] == "合成回款项目 a"
    assert row_a["project"]["lifecycle_status"] == "ongoing"
    assert row_a["project"]["manager_assignment"] == {
        "username": "reminder_admin",
        "display_name": "合成reminder_admin",
    }
    assert row_a["project"]["service_period"] == {
        "service_start": "2026-01-01",
        "service_end": "2027-01-01",
        "completeness_state": "complete",
    }
    assert row_a["project"]["contracts"] == [
        {
            "project_contract_id": pc_a,
            "contract_no": "XS-REM-a",
            "relation_status": "active",
            "lifecycle_status": "active",
            "version": 1,
        }
    ]
    assert row_a["reminder_counts"] == {
        "total": 2,
        "needs_review": 1,
        "handled": 0,
        "incomplete": 0,
        "overdue": 1,
        "due_this_month": 0,
        "upcoming": 0,
    }
    actionable = row_a["next_actionable_milestone"]
    assert actionable["milestone_id"] == "rem-m-a1"
    assert actionable["reminder_state"] == "needs_review"
    assert actionable["planned_month"] == "2026-07"
    assert actionable["planned_amount"] == "18000.00"
    assert actionable["version"] == 1

    row_b = rows[1]
    assert row_b["reminder_counts"]["due_this_month"] == 1
    assert row_b["next_actionable_milestone"]["planned_month"] == "2026-08"


def test_search_q_matches_project_code_display_name_and_contract_no(db):
    admin = _sys_user(db, username="reminder_search_admin", role="admin")
    _project(db, suffix="alpha", manager=admin, contract_no="XS-ALPHA-001")
    _project(db, suffix="beta", manager=admin)
    client, _ = _client(db, username="reminder_search_admin", role="admin")

    code_hit = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"q": "REM-al"},
    )
    assert code_hit.status_code == 200
    assert [r["project"]["project_id"] for r in code_hit.json()["rows"]] == [
        "reminder-project-alpha"
    ]

    name_hit = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"q": "合成回款项目 beta"},
    )
    assert name_hit.status_code == 200
    assert [r["project"]["project_id"] for r in name_hit.json()["rows"]] == [
        "reminder-project-beta"
    ]

    contract_hit = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"q": "XS-ALPHA"},
    )
    assert contract_hit.status_code == 200
    assert [r["project"]["project_id"] for r in contract_hit.json()["rows"]] == [
        "reminder-project-alpha"
    ]


def test_search_reminder_state_filter_and_pagination(db):
    admin = _sys_user(db, username="reminder_filter_admin", role="admin")
    project_x, pc_x = _project(db, suffix="x", manager=admin)
    project_y, pc_y = _project(db, suffix="y", manager=admin)
    _milestone(
        db,
        project_id=project_x,
        project_contract_id=pc_x,
        milestone_id="rem-m-x1",
        planned_date=date(2026, 6, 1),
    )
    _milestone(
        db,
        project_id=project_y,
        project_contract_id=pc_y,
        milestone_id="rem-m-y1",
        planned_date=date(2026, 10, 1),
    )
    client, _ = _client(db, username="reminder_filter_admin", role="admin")
    overdue = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"reminder_state": "overdue"},
    )
    assert overdue.status_code == 200
    assert [r["project"]["project_id"] for r in overdue.json()["rows"]] == [
        "reminder-project-x"
    ]
    upcoming = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"reminder_state": "upcoming"},
    )
    assert [r["project"]["project_id"] for r in upcoming.json()["rows"]] == [
        "reminder-project-y"
    ]

    paged = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"page": 1, "page_size": 1},
    )
    assert paged.status_code == 200
    assert paged.json()["total"] == 2
    assert len(paged.json()["rows"]) == 1

    bad_state = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"reminder_state": "not-a-state"},
    )
    assert bad_state.status_code == 422

    bad_page_size = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"page_size": 201},
    )
    assert bad_page_size.status_code == 422


def test_search_owner_scope_me_and_all_permission(db):
    manager = _sys_user(
        db,
        username="reminder_scope_manager",
        role="purchaser",
    )
    _project(db, suffix="owned", manager=manager)
    _project(db, suffix="foreign")
    client, _ = _client(
        db,
        username="reminder_scope_manager",
        role="purchaser",
    )
    mine = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"owner_scope": "me"},
    )
    assert mine.status_code == 200, mine.text
    assert mine.json()["owner_scope"] == "me"
    assert mine.json()["allowed_owner_scopes"] == ["me"]
    assert [r["project"]["project_id"] for r in mine.json()["rows"]] == [
        "reminder-project-owned"
    ]
    denied = client.post(
        "/api/maintenance/collection-reminders/search",
        json={"owner_scope": "all"},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"


def test_search_amount_masking_without_data_profit(db):
    manager = _sys_user(
        db,
        username="reminder_masked_manager",
        role="purchaser",
    )
    project, pc = _project(db, suffix="masked", manager=manager)
    _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-masked",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_masked_manager",
        role="purchaser",
    )
    payload = client.post(
        "/api/maintenance/collection-reminders/search",
        json={},
    ).json()
    assert payload["amount_visibility"] == "restricted"
    row = payload["rows"][0]
    assert row["next_actionable_milestone"]["planned_amount"] is None
    assert row["next_actionable_milestone"]["planned_month"] == "2026-08"


# ---------- 详情 ----------

def test_detail_rows_summary_and_last_operation(db):
    admin = _sys_user(db, username="reminder_detail_admin", role="admin")
    project, pc = _project(db, suffix="detail", manager=admin)
    _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-detail-1",
        sequence=1,
        planned_date=date(2026, 6, 1),
    )
    _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-detail-2",
        sequence=2,
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(db, username="reminder_detail_api", role="admin")
    response = client.get(
        f"/api/maintenance/projects/stable/{project}/collection-milestones"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["project"]["project_id"] == project
    assert payload["amount_visibility"] == "visible"
    assert payload["as_of"]
    assert payload["data_version"]
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["overdue"] == 1
    assert payload["summary"]["due_this_month"] == 1
    assert len(payload["rows"]) == 2
    row = payload["rows"][0]
    assert row["milestone_id"] == "rem-m-detail-1"
    assert row["project_contract_id"] == pc
    assert row["contract_no"] == "XS-REM-detail"
    assert row["sequence"] == 1
    assert row["planned_date"] == "2026-06-01"
    assert row["date_precision"] == "month"
    assert row["planned_month"] == "2026-06"
    assert row["planned_amount"] == "18000.00"
    assert row["completeness_state"] == "complete"
    assert row["follow_up_status"] == "pending"
    assert row["reminder_state"] == "overdue"
    assert row["follow_up_review_required"] is False
    assert row["followed_up_by"] is None
    assert row["followed_up_at"] is None
    assert row["follow_up_note"] is None
    assert row["last_operation"] is None
    assert row["version"] == 1


def test_detail_not_found_and_invisible_403(db):
    _sys_user(db, username="reminder_detail_owner", role="purchaser")
    _project(db, suffix="hidden")
    client, _ = _client(
        db,
        username="reminder_detail_foreign_api",
        role="purchaser",
    )
    missing = client.get(
        "/api/maintenance/projects/stable/reminder-project-does-not-exist/collection-milestones"
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"
    hidden = client.get(
        "/api/maintenance/projects/stable/reminder-project-hidden/collection-milestones"
    )
    assert hidden.status_code == 403
    assert hidden.json()["detail"]["code"] == "permission_denied"


def test_detail_amount_masked_for_purchaser(db):
    manager = _sys_user(
        db,
        username="reminder_detail_masked_manager",
        role="purchaser",
    )
    project, pc = _project(db, suffix="detail-masked", manager=manager)
    _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-detail-masked",
    )
    client, _ = _client(
        db,
        username="reminder_detail_masked_manager",
        role="purchaser",
    )
    payload = client.get(
        f"/api/maintenance/projects/stable/{project}/collection-milestones"
    ).json()
    assert payload["amount_visibility"] == "restricted"
    assert all(row["planned_amount"] is None for row in payload["rows"])


# ---------- 写操作：handle ----------

def _follow_up_url(milestone_id: str) -> str:
    return f"/api/maintenance/collection-milestones/{milestone_id}/follow-ups"


def test_follow_up_handle_success(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="handle", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-handle",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api",
        role="admin",
        follow_up_action=True,
    )
    response = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "handle-key-0001",
            "action": "handle",
            "note": "已电话跟进客户",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["idempotent_replay"] is False
    assert payload["data_version"]
    row = payload["row"]
    assert row["follow_up_status"] == "handled"
    assert row["reminder_state"] == "handled"
    assert row["follow_up_review_required"] is False
    assert row["follow_up_note"] == "已电话跟进客户"
    assert row["followed_up_by"] == "合成reminder_writer_api"
    assert row["followed_up_at"]
    assert row["version"] == 2

    reloaded = _reload(db, MaintenanceCollectionMilestone, milestone.milestone_id)
    assert reloaded.follow_up_status == "handled"
    assert reloaded.version == 2
    operation_count = db.scalar(
        select(func.count()).select_from(MaintenanceCollectionMilestoneOperation)
    )
    assert operation_count == 1


def test_follow_up_handle_on_handled_node_422(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin2",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="handle-twice", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-handle-twice",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api2",
        role="admin",
        follow_up_action=True,
    )
    first = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "handle-twice-key-1",
            "action": "handle",
        },
    )
    assert first.status_code == 200
    second = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 2,
            "idempotency_key": "handle-twice-key-2",
            "action": "handle",
        },
    )
    assert second.status_code == 422
    assert second.json()["detail"]["code"] == "invalid_request"


# ---------- 写操作：reschedule ----------

def test_follow_up_reschedule_changes_month_and_keeps_amount(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin3",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="reschedule", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-reschedule",
        planned_date=date(2026, 8, 20),
        planned_amount=Decimal("25000.00"),
        date_precision="day",
    )
    client, _ = _client(
        db,
        username="reminder_writer_api3",
        role="admin",
        follow_up_action=True,
    )
    response = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "reschedule-key-0001",
            "action": "reschedule",
            "planned_month": "2026-11",
            "reason": "客户确认下月回款",
        },
    )
    assert response.status_code == 200, response.text
    row = response.json()["row"]
    assert row["planned_date"] == "2026-11-01"
    assert row["date_precision"] == "month"
    assert row["planned_month"] == "2026-11"
    assert row["planned_amount"] == "25000.00"
    assert row["follow_up_status"] == "pending"
    assert row["reminder_state"] == "upcoming"
    assert row["version"] == 2


def test_follow_up_reschedule_requires_reason_422(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin4",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="reschedule-bad", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-reschedule-bad",
    )
    client, _ = _client(
        db,
        username="reminder_writer_api4",
        role="admin",
        follow_up_action=True,
    )
    no_reason = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "reschedule-bad-key-1",
            "action": "reschedule",
            "planned_month": "2026-11",
        },
    )
    assert no_reason.status_code == 422
    no_month = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "reschedule-bad-key-2",
            "action": "reschedule",
            "reason": "客户确认",
        },
    )
    assert no_month.status_code == 422


# ---------- 写操作：reopen ----------

def test_follow_up_reopen_clears_handled_state(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin5",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="reopen", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-reopen",
        planned_date=date(2026, 8, 20),
        follow_up_status="handled",
        follow_up_note="误处理",
        followed_up_by=admin.id,
        followed_up_at=datetime.now(UTC),
        version=2,
    )
    client, _ = _client(
        db,
        username="reminder_writer_api5",
        role="admin",
        follow_up_action=True,
    )
    response = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 2,
            "idempotency_key": "reopen-key-0001",
            "action": "reopen",
            "reason": "客户实际未回款，重新进入提醒",
        },
    )
    assert response.status_code == 200, response.text
    row = response.json()["row"]
    assert row["follow_up_status"] == "pending"
    assert row["follow_up_review_required"] is False
    assert row["follow_up_note"] is None
    assert row["followed_up_by"] is None
    assert row["followed_up_at"] is None
    assert row["reminder_state"] == "due_this_month"
    assert row["version"] == 3


def test_follow_up_needs_review_only_reopen(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin6",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="needs-review", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-needs-review",
        planned_date=date(2026, 8, 20),
        follow_up_status="handled",
        follow_up_review_required=True,
        followed_up_by=admin.id,
        followed_up_at=datetime.now(UTC),
        version=2,
    )
    client, _ = _client(
        db,
        username="reminder_writer_api6",
        role="admin",
        follow_up_action=True,
    )
    handle_denied = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 2,
            "idempotency_key": "needs-review-handle",
            "action": "handle",
        },
    )
    assert handle_denied.status_code == 422
    reschedule_denied = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 2,
            "idempotency_key": "needs-review-reschedule",
            "action": "reschedule",
            "planned_month": "2026-10",
            "reason": "计划变更",
        },
    )
    assert reschedule_denied.status_code == 422
    reopened = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 2,
            "idempotency_key": "needs-review-reopen",
            "action": "reopen",
            "reason": "计划已更新，重新跟进",
        },
    )
    assert reopened.status_code == 200
    assert reopened.json()["row"]["follow_up_review_required"] is False


def test_follow_up_incomplete_read_only(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin7",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="incomplete", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-incomplete",
        planned_date=date(2026, 8, 20),
        planned_amount=None,
        completeness_state="date_only",
    )
    client, _ = _client(
        db,
        username="reminder_writer_api7",
        role="admin",
        follow_up_action=True,
    )
    for action, extra in (
        ("handle", {}),
        ("reschedule", {"planned_month": "2026-10", "reason": "补全计划"}),
    ):
        response = client.post(
            _follow_up_url(milestone.milestone_id),
            json={
                "expected_version": 1,
                "idempotency_key": f"incomplete-{action}",
                "action": action,
                **extra,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "invalid_request"


# ---------- 版本冲突 ----------

def test_follow_up_expected_version_conflict_409(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin8",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="version", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-version",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api8",
        role="admin",
        follow_up_action=True,
    )
    response = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 99,
            "idempotency_key": "version-conflict-key",
            "action": "handle",
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "version_conflict"
    assert detail["current_version"] == 1
    assert detail["message"] == "数据已变化，请刷新后重试"


# ---------- 幂等 ----------

def test_follow_up_idempotent_replay_same_key_same_body(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin9",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="idem", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-idem",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api9",
        role="admin",
        follow_up_action=True,
    )
    body = {
        "expected_version": 1,
        "idempotency_key": "idem-key-0000001",
        "action": "handle",
        "note": "重试安全的备注",
    }
    first = client.post(_follow_up_url(milestone.milestone_id), json=body)
    second = client.post(_follow_up_url(milestone.milestone_id), json=body)
    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert second.json()["idempotent_replay"] is True
    assert second.json()["row"] == first.json()["row"]
    assert second.json()["data_version"] == first.json()["data_version"]
    operation_count = db.scalar(
        select(func.count()).select_from(MaintenanceCollectionMilestoneOperation)
    )
    assert operation_count == 1


def test_follow_up_idempotency_conflict_same_key_different_body(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin10",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="idem-conflict", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-idem-conflict",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api10",
        role="admin",
        follow_up_action=True,
    )
    first = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "idem-conflict-key-1",
            "action": "handle",
            "note": "第一次备注",
        },
    )
    assert first.status_code == 200
    second = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "idem-conflict-key-1",
            "action": "handle",
            "note": "第二次备注",
        },
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "version_conflict"


def test_follow_up_idempotency_conflict_same_key_different_actor(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin11",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="idem-actor", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-idem-actor",
        planned_date=date(2026, 8, 20),
    )
    client_a, _ = _client(
        db,
        username="reminder_writer_api11a",
        role="admin",
        follow_up_action=True,
    )
    client_b, _ = _client(
        db,
        username="reminder_writer_api11b",
        role="admin",
        follow_up_action=True,
    )
    body = {
        "expected_version": 1,
        "idempotency_key": "idem-actor-key-1",
        "action": "handle",
    }
    first = client_a.post(_follow_up_url(milestone.milestone_id), json=body)
    assert first.status_code == 200
    second = client_b.post(_follow_up_url(milestone.milestone_id), json=body)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "version_conflict"


def test_follow_up_idempotency_conflict_same_key_different_milestone(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin12",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="idem-milestone", manager=admin)
    first_m = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-idem-m1",
        sequence=1,
        planned_date=date(2026, 8, 20),
    )
    second_m = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-idem-m2",
        sequence=2,
        planned_date=date(2026, 9, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api12",
        role="admin",
        follow_up_action=True,
    )
    body = {
        "expected_version": 1,
        "idempotency_key": "idem-milestone-key-1",
        "action": "handle",
    }
    first = client.post(_follow_up_url(first_m.milestone_id), json=body)
    assert first.status_code == 200
    second = client.post(_follow_up_url(second_m.milestone_id), json=body)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "version_conflict"


# ---------- 失败关闭 ----------

def test_follow_up_idor_403_for_invisible_project(db):
    _sys_user(db, username="reminder_foreign_owner", role="purchaser")
    project, pc = _project(db, suffix="idor")
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-idor",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_foreign_api",
        role="purchaser",
        follow_up_action=True,
    )
    response = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "idor-key-000001",
            "action": "handle",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"
    reloaded = _reload(db, MaintenanceCollectionMilestone, milestone.milestone_id)
    assert reloaded.follow_up_status == "pending"
    assert reloaded.version == 1


def test_follow_up_inactive_project_404(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin13",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(
        db,
        suffix="inactive",
        manager=admin,
        active=False,
    )
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-inactive",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api13",
        role="admin",
        follow_up_action=True,
    )
    response = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "inactive-key-0001",
            "action": "handle",
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_follow_up_unknown_milestone_404(db):
    client, _ = _client(
        db,
        username="reminder_writer_api14",
        role="admin",
        follow_up_action=True,
    )
    response = client.post(
        _follow_up_url("rem-m-does-not-exist"),
        json={
            "expected_version": 1,
            "idempotency_key": "unknown-key-0001",
            "action": "handle",
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_follow_up_admin_without_explicit_action_403(db):
    admin = _sys_user(db, username="reminder_admin_no_action", role="admin")
    project, pc = _project(db, suffix="no-action", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-no-action",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_no_action_api",
        role="admin",
        follow_up_action=False,
    )
    response = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "no-action-key-01",
            "action": "handle",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"
    reloaded = _reload(db, MaintenanceCollectionMilestone, milestone.milestone_id)
    assert reloaded.version == 1


def test_follow_up_canary_scope_denied_for_other_projects(
    db, monkeypatch
):
    admin = _sys_user(
        db,
        username="reminder_writer_admin15",
        role="admin",
        follow_up_action=True,
    )
    settings = SimpleNamespace(
        maintenance_collection_canary_project_id="reminder-project-canary",
        maintenance_collection_plan_apply_enabled=False,
    )
    monkeypatch.setattr(
        reminders_service,
        "get_settings",
        lambda: settings,
    )
    # canary 项目本身允许
    canary_project, canary_pc = _project(db, suffix="canary", manager=admin)
    canary_m = _milestone(
        db,
        project_id=canary_project,
        project_contract_id=canary_pc,
        milestone_id="rem-m-canary",
        planned_date=date(2026, 8, 20),
    )
    # 其他项目固定 403
    other_project, other_pc = _project(db, suffix="other", manager=admin)
    other_m = _milestone(
        db,
        project_id=other_project,
        project_contract_id=other_pc,
        milestone_id="rem-m-other",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api15",
        role="admin",
        follow_up_action=True,
    )
    denied = client.post(
        _follow_up_url(other_m.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "canary-denied-key",
            "action": "handle",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "canary_scope_denied"
    reloaded = _reload(db, MaintenanceCollectionMilestone, other_m.milestone_id)
    assert reloaded.version == 1
    assert reloaded.follow_up_status == "pending"

    allowed = client.post(
        _follow_up_url(canary_m.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "canary-allowed-key",
            "action": "handle",
        },
    )
    assert allowed.status_code == 200, allowed.text


def test_operation_ledger_rejects_update_and_delete(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin16",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="append-only", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-append-only",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api16",
        role="admin",
        follow_up_action=True,
    )
    response = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "append-only-key-1",
            "action": "handle",
            "note": "留痕",
        },
    )
    assert response.status_code == 200
    db.expire_all()
    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "UPDATE maintenance_collection_milestone_operation "
                "SET action = 'reopen' WHERE idempotency_key = :key"
            ),
            {"key": "append-only-key-1"},
        )
    db.rollback()
    db.expire_all()
    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "DELETE FROM maintenance_collection_milestone_operation "
                "WHERE idempotency_key = :key"
            ),
            {"key": "append-only-key-1"},
        )
    db.rollback()
    db.expire_all()
    remaining = db.scalar(
        select(func.count()).select_from(MaintenanceCollectionMilestoneOperation)
    )
    assert remaining == 1


# ---------- DTO 校验 ----------

def test_follow_up_extra_field_422_and_discriminated_fields(db):
    admin = _sys_user(
        db,
        username="reminder_writer_admin17",
        role="admin",
        follow_up_action=True,
    )
    project, pc = _project(db, suffix="dto", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-dto",
        planned_date=date(2026, 8, 20),
    )
    client, _ = _client(
        db,
        username="reminder_writer_api17",
        role="admin",
        follow_up_action=True,
    )
    extra = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "dto-extra-key-01",
            "action": "handle",
            "mystery_field": "forbidden",
        },
    )
    assert extra.status_code == 422
    handle_with_reason = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "dto-reason-key-01",
            "action": "handle",
            "reason": "不允许的理由",
        },
    )
    assert handle_with_reason.status_code == 422
    short_key = client.post(
        _follow_up_url(milestone.milestone_id),
        json={
            "expected_version": 1,
            "idempotency_key": "short",
            "action": "handle",
        },
    )
    assert short_key.status_code == 422
