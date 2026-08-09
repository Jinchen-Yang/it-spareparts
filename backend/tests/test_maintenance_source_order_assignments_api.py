"""Public API behavior for manual source-maintenance-order assignments (#201)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import hash_password
from app.etl import loader
from app.main import app
from app.models.maintenance import FMaintenanceOrder
from app.config import get_settings
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectAuditLog
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysUser
from tests import factories as f


def _admin_client(db, username: str = "source_assignment_admin") -> TestClient:
    db.add(
        SysUser(
            username=username,
            role="admin",
            display_name="合成维保归属管理员",
            password_hash=hash_password("synthetic-password-123"),
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
            display_name="合成维保归属授权账号",
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


def _source_order(db, *, raw_order_id: str, order_no: str, project_raw: str):
    batch = SysImportBatch(
        filename=f"{raw_order_id}.xlsx",
        file_type="maintenance",
        file_hash=f"hash-{raw_order_id}",
        status="success",
    )
    db.add(batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id=raw_order_id,
        order_no=order_no,
        order_date=date(2026, 1, 15),
        project_raw=project_raw,
        project_std=project_raw,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.commit()
    return order


def _project(db, *, project_id: str, project_code: str, display_name: str):
    project = MaintenanceProject(
        project_id=project_id,
        project_code=project_code,
        display_name=display_name,
        lifecycle_status="missing",
        is_active=True,
    )
    db.add(project)
    db.commit()
    return project


def test_real_admin_assigns_unassigned_source_order_and_directory_reads_it(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-RAW-001",
        order_no="WBDD-SYNTH-001",
        project_raw="原始项目文字甲",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000201",
        project_code="MAINT-SYNTH-201-A",
        display_name="稳定项目甲",
    )
    client = _admin_client(db)

    assigned = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [
                {
                    "source_order_id": source.raw_order_id,
                    "expected_assignment_id": None,
                    "expected_version": None,
                }
            ],
            "reason": "业务人员核对原始单据后确认归属",
        },
    )

    assert assigned.status_code == 200, assigned.text
    assignment = assigned.json()["assignments"][0]
    assert assignment["source_order_id"] == source.raw_order_id
    assert assignment["project_id"] == project.project_id
    assert assignment["version"] == 1
    assert assignment["is_active"] is True

    directory = client.get(
        "/api/maintenance/project-assignments/orders",
        params={"assignment_status": "assigned"},
    )
    assert directory.status_code == 200, directory.text
    assert directory.json()["rows"] == [
        {
            "raw_order_id": source.raw_order_id,
            "order_no": source.order_no,
            "order_date": "2026-01-15",
            "project_raw": "原始项目文字甲",
            "project_std": "原始项目文字甲",
            "assignment_id": assignment["assignment_id"],
            "assignment_version": 1,
            "assigned_project": {
                "project_id": project.project_id,
                "project_code": project.project_code,
                "display_name": project.display_name,
                "is_active": True,
            },
        }
    ]
    assert directory.json()["total"] == 1


def test_directory_filters_by_repeated_source_order_ids(db):
    included = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-FILTER-001",
        order_no="WBDD-SYNTH-FILTER-001",
        project_raw="需要精确刷新的来源单",
    )
    _source_order(
        db,
        raw_order_id="WBDD-SYNTH-FILTER-002",
        order_no="WBDD-SYNTH-FILTER-002",
        project_raw="不应出现在精确刷新结果",
    )
    client = _admin_client(db, "source_assignment_filter_admin")

    response = client.get(
        "/api/maintenance/project-assignments/orders",
        params=[
            ("source_order_id", included.raw_order_id),
            ("source_order_id", "WBDD-SYNTH-NOT-PRESENT"),
            ("assignment_status", "all"),
        ],
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert [row["raw_order_id"] for row in response.json()["rows"]] == [
        included.raw_order_id
    ]


def test_same_source_project_name_can_be_assigned_to_different_stable_projects(db):
    first = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-SAME-001",
        order_no="WBDD-SAME-001",
        project_raw="完全相同的历史项目名",
    )
    second = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-SAME-002",
        order_no="WBDD-SAME-002",
        project_raw="完全相同的历史项目名",
    )
    first_project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000202",
        project_code="MAINT-SYNTH-201-B",
        display_name="稳定项目乙",
    )
    second_project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000203",
        project_code="MAINT-SYNTH-201-C",
        display_name="稳定项目丙",
    )
    client = _admin_client(db, "same_name_assignment_admin")

    for source, project in (
        (first, first_project),
        (second, second_project),
    ):
        response = client.post(
            "/api/maintenance/project-assignments/orders/assign",
            json={
                "project_id": project.project_id,
                "items": [{"source_order_id": source.raw_order_id}],
                "reason": "按业务单据核实，不根据历史名称猜测",
            },
        )
        assert response.status_code == 200, response.text

    directory = client.get(
        "/api/maintenance/project-assignments/orders",
        params={"assignment_status": "assigned", "page_size": 10},
    ).json()
    assert {
        row["raw_order_id"]: row["assigned_project"]["project_id"]
        for row in directory["rows"]
    } == {
        first.raw_order_id: first_project.project_id,
        second.raw_order_id: second_project.project_id,
    }
    assert {row["project_raw"] for row in directory["rows"]} == {
        "完全相同的历史项目名"
    }


def test_batch_stale_expectation_rolls_back_every_source_order(db):
    first = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-ATOMIC-001",
        order_no="WBDD-ATOMIC-001",
        project_raw="原始项目文字丁",
    )
    stale = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-ATOMIC-002",
        order_no="WBDD-ATOMIC-002",
        project_raw="原始项目文字戊",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000204",
        project_code="MAINT-SYNTH-201-D",
        display_name="稳定项目丁",
    )
    client = _admin_client(db, "atomic_assignment_admin")

    response = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [
                {"source_order_id": first.raw_order_id},
                {
                    "source_order_id": stale.raw_order_id,
                    "expected_assignment_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                    "expected_version": 7,
                },
            ],
            "reason": "验证过期批次不会部分写入",
        },
    )

    assert response.status_code == 409, response.text
    directory = client.get(
        "/api/maintenance/project-assignments/orders",
        params={"assignment_status": "unassigned", "page_size": 10},
    )
    assert directory.status_code == 200, directory.text
    assert {row["raw_order_id"] for row in directory.json()["rows"]} == {
        first.raw_order_id,
        stale.raw_order_id,
    }


def test_batch_rejects_duplicate_source_order_ids_without_writing(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-DUPLICATE-001",
        order_no="WBDD-DUPLICATE-001",
        project_raw="原始项目文字己",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000205",
        project_code="MAINT-SYNTH-201-E",
        display_name="稳定项目戊",
    )
    client = _admin_client(db, "duplicate_assignment_admin")

    response = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [
                {"source_order_id": source.raw_order_id},
                {"source_order_id": source.raw_order_id},
            ],
            "reason": "重复勾选必须失败关闭",
        },
    )

    assert response.status_code == 400, response.text
    directory = client.get(
        "/api/maintenance/project-assignments/orders",
        params={"assignment_status": "unassigned"},
    )
    assert directory.status_code == 200
    assert [row["raw_order_id"] for row in directory.json()["rows"]] == [
        source.raw_order_id
    ]


def test_assignment_can_be_reassigned_with_current_assignment_version(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-REASSIGN-001",
        order_no="WBDD-REASSIGN-001",
        project_raw="原始项目文字庚",
    )
    first_project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000206",
        project_code="MAINT-SYNTH-201-F",
        display_name="改派前稳定项目",
    )
    next_project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000207",
        project_code="MAINT-SYNTH-201-G",
        display_name="改派后稳定项目",
    )
    client = _admin_client(db, "reassign_assignment_admin")
    initial = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": first_project.project_id,
            "items": [{"source_order_id": source.raw_order_id}],
            "reason": "首次人工归属",
        },
    ).json()["assignments"][0]

    reassigned = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": next_project.project_id,
            "items": [
                {
                    "source_order_id": source.raw_order_id,
                    "expected_assignment_id": initial["assignment_id"],
                    "expected_version": initial["version"],
                }
            ],
            "reason": "业务复核确认应改派到另一稳定项目",
        },
    )

    assert reassigned.status_code == 200, reassigned.text
    latest = reassigned.json()["assignments"][0]
    assert latest["assignment_id"] != initial["assignment_id"]
    assert latest["source_order_id"] == source.raw_order_id
    assert latest["project_id"] == next_project.project_id
    assert latest["version"] == 1

    directory = client.get(
        "/api/maintenance/project-assignments/orders",
        params={"assignment_status": "assigned"},
    ).json()
    assert directory["rows"][0]["assigned_project"]["project_id"] == (
        next_project.project_id
    )
    assert directory["rows"][0]["assignment_id"] == latest["assignment_id"]


def test_current_assignment_can_be_unassigned_without_deleting_source_order(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-UNASSIGN-001",
        order_no="WBDD-UNASSIGN-001",
        project_raw="原始项目文字辛",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000208",
        project_code="MAINT-SYNTH-201-H",
        display_name="待撤销归属的稳定项目",
    )
    client = _admin_client(db, "unassign_assignment_admin")
    initial = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [{"source_order_id": source.raw_order_id}],
            "reason": "首次人工归属",
        },
    ).json()["assignments"][0]

    unassigned = client.post(
        "/api/maintenance/project-assignments/orders/unassign",
        json={
            "items": [
                {
                    "assignment_id": initial["assignment_id"],
                    "expected_version": initial["version"],
                }
            ],
            "reason": "复核后确认当前不应归属任何项目",
        },
    )

    assert unassigned.status_code == 200, unassigned.text
    assert unassigned.json()["assignments"] == [
        {
            **initial,
            "is_active": False,
            "version": 2,
        }
    ]
    directory = client.get(
        "/api/maintenance/project-assignments/orders",
        params={"assignment_status": "unassigned"},
    ).json()
    assert directory["rows"][0]["raw_order_id"] == source.raw_order_id
    assert directory["rows"][0]["assignment_id"] is None
    assert directory["rows"][0]["assigned_project"] is None


def test_same_target_assignment_replay_returns_existing_without_new_audit(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-REPLAY-001",
        order_no="WBDD-REPLAY-001",
        project_raw="原始项目文字壬",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000209",
        project_code="MAINT-SYNTH-201-I",
        display_name="幂等重放稳定项目",
    )
    client = _admin_client(db, "replay_assignment_admin")
    request = {
        "project_id": project.project_id,
        "items": [{"source_order_id": source.raw_order_id}],
        "reason": "确认来源单属于此稳定项目",
    }

    first = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json=request,
    )
    replay = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json=request,
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    audits = list(
        db.scalars(
            select(MaintenanceProjectAuditLog).where(
                MaintenanceProjectAuditLog.entity_type
                == "source_order_assignment"
            )
        )
    )
    assert [(row.action, row.operated_by) for row in audits] == [
        ("assign", "replay_assignment_admin")
    ]


def test_same_target_replay_remains_idempotent_after_project_is_archived(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-ARCHIVED-REPLAY-001",
        order_no="WBDD-ARCHIVED-REPLAY-001",
        project_raw="归档后重放来源单",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000223",
        project_code="MAINT-SYNTH-201-W",
        display_name="归档后幂等重放项目",
    )
    client = _admin_client(db, "archived_replay_assignment_admin")
    request = {
        "project_id": project.project_id,
        "items": [{"source_order_id": source.raw_order_id}],
        "reason": "首次确认后发生网络重放",
    }
    first = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json=request,
    )
    assert first.status_code == 200, first.text
    project.is_active = False
    db.commit()

    replay = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json=request,
    )

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert db.scalar(
        select(func.count())
        .select_from(MaintenanceProjectAuditLog)
        .where(MaintenanceProjectAuditLog.entity_type == "source_order_assignment")
    ) == 1


def test_same_target_item_is_idempotent_while_mixed_batch_assigns_new_rows(db):
    assigned_source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-SAME-TARGET-001",
        order_no="WBDD-SAME-TARGET-001",
        project_raw="已归属来源单",
    )
    pending_source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-SAME-TARGET-002",
        order_no="WBDD-SAME-TARGET-002",
        project_raw="同批未归属来源单",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000222",
        project_code="MAINT-SYNTH-201-V",
        display_name="同目标原子拒绝项目",
    )
    client = _admin_client(db, "same_target_atomic_admin")
    first = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [{"source_order_id": assigned_source.raw_order_id}],
            "reason": "首次确认来源单归属",
        },
    )
    assert first.status_code == 200, first.text
    current = first.json()["assignments"][0]

    replayed = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [
                {"source_order_id": pending_source.raw_order_id},
                {
                    "source_order_id": assigned_source.raw_order_id,
                    "expected_assignment_id": current["assignment_id"],
                    "expected_version": current["version"],
                },
            ],
            "reason": "同目标项保持幂等并处理同批新归属",
        },
    )

    assert replayed.status_code == 200, replayed.text
    replayed_rows = {
        row["source_order_id"]: row for row in replayed.json()["assignments"]
    }
    assert replayed_rows[assigned_source.raw_order_id] == current
    assert replayed_rows[pending_source.raw_order_id]["project_id"] == project.project_id
    directory = client.get(
        "/api/maintenance/project-assignments/orders",
        params=[
            ("source_order_id", assigned_source.raw_order_id),
            ("source_order_id", pending_source.raw_order_id),
            ("assignment_status", "all"),
        ],
    )
    assert directory.status_code == 200, directory.text
    rows = {row["raw_order_id"]: row for row in directory.json()["rows"]}
    assert rows[assigned_source.raw_order_id]["assignment_id"] == current[
        "assignment_id"
    ]
    assert rows[assigned_source.raw_order_id]["assignment_version"] == current[
        "version"
    ]
    assert rows[pending_source.raw_order_id]["assignment_id"] == replayed_rows[
        pending_source.raw_order_id
    ]["assignment_id"]
    audits = list(
        db.scalars(
            select(MaintenanceProjectAuditLog).where(
                MaintenanceProjectAuditLog.entity_type
                == "source_order_assignment"
            )
        )
    )
    assert [(row.action, row.operated_by) for row in audits] == [
        ("assign", "same_target_atomic_admin"),
        ("assign", "same_target_atomic_admin"),
    ]


def test_assignment_only_changes_project_detail_manual_order_count(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-COUNT-001",
        order_no="WBDD-COUNT-001",
        project_raw="原始项目文字癸",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000210",
        project_code="MAINT-SYNTH-201-J",
        display_name="只增加人工归属计数的项目",
    )
    client = _admin_client(db, "assignment_count_admin")
    workspace_path = (
        f"/api/maintenance/projects/stable/{project.project_id}/workspace"
    )
    before = client.get(workspace_path, params={"as_of": "2026-08-09"})
    assert before.status_code == 200, before.text

    assigned = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [{"source_order_id": source.raw_order_id}],
            "reason": "确认历史来源单归属",
        },
    )
    assert assigned.status_code == 200, assigned.text
    after = client.get(workspace_path, params={"as_of": "2026-08-09"})
    assert after.status_code == 200, after.text

    before_payload = before.json()
    after_payload = after.json()
    assert before_payload["project"]["manual_source_order_count"] == 0
    assert after_payload["project"]["manual_source_order_count"] == 1
    assert after_payload["project"]["metrics"] == before_payload["project"]["metrics"]
    assert after_payload["requisitions"] == before_payload["requisitions"]
    assert after_payload["approved_expenses"] == before_payload["approved_expenses"]


def test_directory_and_write_permission_matrix_fail_closed(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-PERMISSION-001",
        order_no="WBDD-PERMISSION-001",
        project_raw="权限边界合成项目文字",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000211",
        project_code="MAINT-SYNTH-201-K",
        display_name="权限矩阵稳定项目",
    )
    directory_path = "/api/maintenance/project-assignments/orders"
    assign_path = f"{directory_path}/assign"
    payload = {
        "project_id": project.project_id,
        "items": [{"source_order_id": source.raw_order_id}],
        "reason": "验证权限失败关闭",
    }

    anonymous = TestClient(app)
    assert anonymous.get(directory_path).status_code == 401
    assert anonymous.post(assign_path, json=payload).status_code == 401

    no_page = _permission_client(
        db,
        username="source_assignment_no_page",
        permissions={
            "page_maintenance": False,
            "data_profit": True,
            "action_maintenance_project_manage": True,
        },
    )
    assert no_page.get(directory_path).status_code == 403
    assert no_page.post(assign_path, json=payload).status_code == 403

    read_only = _permission_client(
        db,
        username="source_assignment_readonly",
        permissions={
            "page_maintenance": True,
            "data_profit": False,
            "action_maintenance_project_manage": False,
        },
    )
    directory = read_only.get(directory_path)
    assert directory.status_code == 200, directory.text
    assert set(directory.json()["rows"][0]) == {
        "raw_order_id",
        "order_no",
        "order_date",
        "project_raw",
        "project_std",
        "assignment_id",
        "assignment_version",
        "assigned_project",
    }
    assert read_only.post(assign_path, json=payload).status_code == 403

    no_profit = _permission_client(
        db,
        username="source_assignment_no_profit",
        permissions={
            "page_maintenance": True,
            "data_profit": False,
            "action_maintenance_project_manage": True,
        },
    )
    assert no_profit.post(assign_path, json=payload).status_code == 403

    no_action = _permission_client(
        db,
        username="source_assignment_no_action",
        permissions={
            "page_maintenance": True,
            "data_profit": True,
            "action_maintenance_project_manage": False,
        },
    )
    assert no_action.post(assign_path, json=payload).status_code == 403
    assert db.scalar(select(MaintenanceSourceOrderAssignment)) is None


def test_shared_password_admin_cannot_write_source_order_assignment(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-SHARED-001",
        order_no="WBDD-SHARED-001",
        project_raw="共享账号禁止写入",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000212",
        project_code="MAINT-SYNTH-201-L",
        display_name="共享账号门禁稳定项目",
    )
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": get_settings().admin_password},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    response = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [{"source_order_id": source.raw_order_id}],
            "reason": "共享账号必须被拒绝",
        },
    )

    assert response.status_code == 403
    assert "实名系统账号" in response.json()["detail"]
    assert db.scalar(select(MaintenanceSourceOrderAssignment)) is None


def test_reassign_and_unassign_keep_immutable_history_and_audit_snapshots(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-HISTORY-001",
        order_no="WBDD-HISTORY-001",
        project_raw="历史链不可覆盖",
    )
    first_project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000213",
        project_code="MAINT-SYNTH-201-M",
        display_name="历史链项目甲",
    )
    second_project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000214",
        project_code="MAINT-SYNTH-201-N",
        display_name="历史链项目乙",
    )
    client = _admin_client(db, "source_assignment_history_admin")
    first = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": first_project.project_id,
            "items": [{"source_order_id": source.raw_order_id}],
            "reason": "首次人工核对",
        },
    ).json()["assignments"][0]
    second = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": second_project.project_id,
            "items": [{
                "source_order_id": source.raw_order_id,
                "expected_assignment_id": first["assignment_id"],
                "expected_version": first["version"],
            }],
            "reason": "复核后改派",
        },
    ).json()["assignments"][0]
    unassigned = client.post(
        "/api/maintenance/project-assignments/orders/unassign",
        json={
            "items": [{
                "assignment_id": second["assignment_id"],
                "expected_version": second["version"],
            }],
            "reason": "再次复核后撤销",
        },
    )
    assert unassigned.status_code == 200, unassigned.text

    history = list(
        db.scalars(
            select(MaintenanceSourceOrderAssignment).order_by(
                MaintenanceSourceOrderAssignment.created_at,
                MaintenanceSourceOrderAssignment.assignment_id,
            )
        )
    )
    assert len(history) == 2
    assert [(row.project_id, row.is_active, row.version) for row in history] == [
        (first_project.project_id, False, 2),
        (second_project.project_id, False, 2),
    ]
    audits = list(
        db.scalars(
            select(MaintenanceProjectAuditLog)
            .where(MaintenanceProjectAuditLog.entity_type == "source_order_assignment")
            .order_by(MaintenanceProjectAuditLog.id)
        )
    )
    assert [row.action for row in audits] == [
        "assign",
        "reassign_out",
        "assign",
        "unassign",
    ]
    assert [row.reason for row in audits] == [
        "首次人工核对",
        "复核后改派",
        "复核后改派",
        "再次复核后撤销",
    ]
    assert audits[1].before_json["is_active"] is True
    assert audits[1].after_json["is_active"] is False
    assert audits[3].before_json["is_active"] is True
    assert audits[3].after_json["is_active"] is False
    assert source.project_raw == "历史链不可覆盖"


def test_competing_initial_assignments_serialize_to_one_winner(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-CONCURRENT-001",
        order_no="WBDD-CONCURRENT-001",
        project_raw="并发归属合成来源单",
    )
    projects = [
        _project(
            db,
            project_id="00000000-0000-4000-8000-000000000215",
            project_code="MAINT-SYNTH-201-O",
            display_name="并发候选项目甲",
        ),
        _project(
            db,
            project_id="00000000-0000-4000-8000-000000000216",
            project_code="MAINT-SYNTH-201-P",
            display_name="并发候选项目乙",
        ),
    ]
    clients = [
        _admin_client(db, "source_assignment_concurrent_a"),
        _admin_client(db, "source_assignment_concurrent_b"),
    ]
    project_ids = [project.project_id for project in projects]
    source_id = source.raw_order_id

    def assign(index: int):
        return clients[index].post(
            "/api/maintenance/project-assignments/orders/assign",
            json={
                "project_id": project_ids[index],
                "items": [{"source_order_id": source_id}],
                "reason": f"并发候选 {index} 的人工确认",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(assign, (0, 1)))

    assert sorted(response.status_code for response in responses) == [200, 409]
    db.expire_all()
    active = list(
        db.scalars(
            select(MaintenanceSourceOrderAssignment).where(
                MaintenanceSourceOrderAssignment.source_order_id == source_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
        )
    )
    assert len(active) == 1
    assert active[0].project_id in set(project_ids)


def test_upsert_reimport_same_raw_order_id_preserves_manual_assignment(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-REIMPORT-001",
        order_no="WBDD-REIMPORT-001",
        project_raw="重导前原始项目名",
    )
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000217",
        project_code="MAINT-SYNTH-201-Q",
        display_name="重导保持归属稳定项目",
    )
    client = _admin_client(db, "source_assignment_reimport_admin")
    assigned = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [{"source_order_id": source.raw_order_id}],
            "reason": "重导前确认归属",
        },
    ).json()["assignments"][0]
    reimport_batch = SysImportBatch(
        filename="synthetic-reimport.xlsx",
        file_type="maintenance",
        file_hash="synthetic-reimport-hash",
        status="success",
    )
    db.add(reimport_batch)
    db.flush()

    loader.load(
        db,
        f.maintenance_result(
            {
                source.raw_order_id: f.maintenance_head(
                    source.raw_order_id,
                    order_no=source.order_no,
                    project="重导后的原始项目名",
                )
            },
            [
                f.maintenance_line(
                    source.raw_order_id,
                    "WBDD-SYNTH-REIMPORT-LINE-001",
                    "PN-SYNTH-REIMPORT",
                )
            ],
        ),
        reimport_batch.id,
        date(2026, 8, 9),
        mode="upsert",
    )
    db.commit()

    directory = client.get(
        "/api/maintenance/project-assignments/orders",
        params={"q": source.order_no, "assignment_status": "assigned"},
    )
    assert directory.status_code == 200, directory.text
    row = directory.json()["rows"][0]
    assert row["project_raw"] == "重导后的原始项目名"
    assert row["project_std"] == "重导后的原始项目名"
    assert row["assignment_id"] == assigned["assignment_id"]
    assert row["assignment_version"] == assigned["version"]
    assert row["assigned_project"]["project_id"] == project.project_id


def test_assignment_request_requires_complete_expectation_pair_and_limits_batch(db):
    project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000218",
        project_code="MAINT-SYNTH-201-R",
        display_name="输入边界稳定项目",
    )
    client = _admin_client(db, "source_assignment_input_admin")
    incomplete = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [{
                "source_order_id": "WBDD-SYNTH-PAIR-001",
                "expected_assignment_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            }],
            "reason": "不完整期望值必须被拒绝",
        },
    )
    too_many = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project.project_id,
            "items": [
                {"source_order_id": f"WBDD-SYNTH-BATCH-{index:03d}"}
                for index in range(101)
            ],
            "reason": "超过批次边界",
        },
    )

    assert incomplete.status_code == 422
    assert too_many.status_code == 422
    assert db.scalar(select(MaintenanceSourceOrderAssignment)) is None


def test_exactly_one_hundred_source_orders_assign_atomically(db):
    batch = SysImportBatch(
        filename="synthetic-100-source-orders.xlsx",
        file_type="maintenance",
        file_hash="synthetic-100-source-orders-hash",
        status="success",
    )
    db.add(batch)
    db.flush()
    source_ids = [f"WBDD-SYNTH-HUNDRED-{index:03d}" for index in range(100)]
    db.add_all(
        [
            FMaintenanceOrder(
                raw_order_id=source_id,
                order_no=f"WBDD-HUNDRED-{index:03d}",
                order_date=date(2026, 1, 15),
                project_raw=f"合成批量原始文字 {index:03d}",
                project_std=f"合成批量原始文字 {index:03d}",
                import_batch_id=batch.id,
            )
            for index, source_id in enumerate(source_ids)
        ]
    )
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000219",
        project_code="MAINT-SYNTH-201-S",
        display_name="一百张批量稳定项目",
        lifecycle_status="missing",
        is_active=True,
    )
    db.add(project)
    db.commit()
    project_id = project.project_id
    client = _admin_client(db, "source_assignment_hundred_admin")

    response = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": project_id,
            "items": [{"source_order_id": source_id} for source_id in source_ids],
            "reason": "逐张核对后明确勾选的一百张合成来源单",
        },
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["assignments"]) == 100
    assert {
        assignment["source_order_id"]
        for assignment in response.json()["assignments"]
    } == set(source_ids)
    assert db.scalar(
        select(func.count())
        .select_from(MaintenanceSourceOrderAssignment)
        .where(MaintenanceSourceOrderAssignment.is_active.is_(True))
    ) == 100
    assert db.scalar(
        select(func.count())
        .select_from(MaintenanceProjectAuditLog)
        .where(MaintenanceProjectAuditLog.entity_type == "source_order_assignment")
    ) == 100


def test_missing_source_or_archived_target_rolls_back_whole_batch(db):
    source = _source_order(
        db,
        raw_order_id="WBDD-SYNTH-INVALID-BATCH-001",
        order_no="WBDD-INVALID-BATCH-001",
        project_raw="非法批次中的合法来源单",
    )
    active_project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000220",
        project_code="MAINT-SYNTH-201-T",
        display_name="非法批次有效目标",
    )
    archived_project = _project(
        db,
        project_id="00000000-0000-4000-8000-000000000221",
        project_code="MAINT-SYNTH-201-U",
        display_name="已归档目标",
    )
    archived_project.is_active = False
    db.commit()
    source_id = source.raw_order_id
    active_project_id = active_project.project_id
    archived_project_id = archived_project.project_id
    client = _admin_client(db, "source_assignment_invalid_batch_admin")

    missing = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": active_project_id,
            "items": [
                {"source_order_id": source_id},
                {"source_order_id": "WBDD-SYNTH-NOT-EXIST"},
            ],
            "reason": "不存在来源单必须整批回滚",
        },
    )
    archived = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={
            "project_id": archived_project_id,
            "items": [{"source_order_id": source_id}],
            "reason": "归档目标必须拒绝",
        },
    )

    assert missing.status_code == 400, missing.text
    assert archived.status_code == 400, archived.text
    assert db.scalar(select(MaintenanceSourceOrderAssignment)) is None
    assert db.scalar(
        select(MaintenanceProjectAuditLog).where(
            MaintenanceProjectAuditLog.entity_type == "source_order_assignment"
        )
    ) is None
