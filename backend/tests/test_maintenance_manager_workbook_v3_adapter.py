"""Database adapter contract for manager monthly workbook v3 (#206)."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta

import pytest
from openpyxl import load_workbook
from openpyxl.utils import range_boundaries
from sqlalchemy import select

from app.auth import hash_password
from app.models.maintenance_manager import (
    MaintenanceAcceptanceDeliverable,
    MaintenanceCollectionMilestone,
    MaintenanceManagerUploadBatch,
    MaintenanceManagerUploadBatchProject,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from app.models.system import SysUser
from app.security import UserContext
from app.services.maintenance_manager_workbook_adapter import (
    MaintenanceManagerWorkbookAdapter,
    ManagerWorkbookConflict,
)
from app.services.maintenance_manager_workbook_v3 import (
    OVERVIEW_SHEET,
    OVERVIEW_TABLE,
    PLAN_SHEET,
    PLAN_TABLE,
    ManagerWorkbookV3Error,
)
from app.services.maintenance_project_operations import project_operations


HMAC_KEY = b"synthetic-manager-workbook-adapter-key"


def _seed(db, *, username: str = "synthetic_manager") -> tuple[SysUser, str, str]:
    user = SysUser(
        username=username,
        role="purchaser",
        display_name="合成项目经理",
        password_hash=hash_password("synthetic-password-123"),
    )
    project = MaintenanceProject(
        project_id=f"project-{username}",
        project_code=f"PM-{username}",
        display_name="合成项目经理工作簿项目",
        lifecycle_status="ongoing",
    )
    db.add_all([user, project])
    db.flush()
    assignment = MaintenanceProjectUserAssignment(
        assignment_id=f"assignment-{username}",
        project_id=project.project_id,
        responsibility_type="primary_manager",
        user_id=user.id,
        version=1,
        assigned_at=datetime.now(UTC),
        assigned_by="synthetic-admin",
        assignment_reason="合成测试负责人映射",
    )
    contract = MaintenanceProjectContract(
        project_contract_id=f"pc-{username}",
        project_id=project.project_id,
        contract_id=f"contract-{username}",
        contract_no=f"XS-{username}",
        contract_amount=100000,
        contract_status="active",
        status_mapping_state="mapped",
        status_mapping_version="synthetic-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        source="synthetic-test",
    )
    actual = MaintenanceCollectionSnapshot(
        collection_id=f"collection-{username}",
        project_id=project.project_id,
        project_contract_id=contract.project_contract_id,
        report_month=date(2026, 8, 1),
        cumulative_amount=30000,
        status="confirmed",
    )
    db.add_all([assignment, contract, actual])
    db.commit()
    return user, project.project_id, contract.project_contract_id


def _adapter(db, user: SysUser) -> MaintenanceManagerWorkbookAdapter:
    return MaintenanceManagerWorkbookAdapter(
        db,
        user_ctx=_ctx(user),
        operator=user.username,
        as_of=date(2026, 8, 9),
    )


def _ctx(user: SysUser) -> UserContext:
    return UserContext(
        role=user.role,
        user_id=user.username,
        is_authenticated=True,
        permissions={
            "page_maintenance": True,
            "data_profit": True,
            "data_purchase_cost": True,
        },
    )


def _set_plan(content: bytes, relation_id: str) -> bytes:
    book = load_workbook(io.BytesIO(content), data_only=False)
    try:
        sheet = book[PLAN_SHEET]
        table = sheet.tables[PLAN_TABLE]
        min_col, min_row, max_col, max_row = range_boundaries(table.ref)
        headers = [sheet.cell(min_row, column).value for column in range(min_col, max_col + 1)]
        target = next(
            row
            for row in range(min_row + 1, max_row + 1)
            if sheet.cell(row, headers.index("项目合同关系ID") + 1).value == relation_id
            and sheet.cell(row, headers.index("计划期次") + 1).value == 1
        )
        sheet.cell(target, headers.index("计划回款日期") + 1, date(2026, 9, 15))
        sheet.cell(target, headers.index("计划回款金额（含税）") + 1, 25000)
        output = io.BytesIO()
        book.save(output)
        return output.getvalue()
    finally:
        book.close()


def _set_acceptance_due(content: bytes, value: date) -> bytes:
    book = load_workbook(io.BytesIO(content), data_only=False)
    try:
        sheet = book[OVERVIEW_SHEET]
        table = sheet.tables[OVERVIEW_TABLE]
        min_col, min_row, max_col, _max_row = range_boundaries(table.ref)
        headers = [sheet.cell(min_row, column).value for column in range(min_col, max_col + 1)]
        sheet.cell(min_row + 1, headers.index("验收报告截止日") + 1, value)
        output = io.BytesIO()
        book.save(output)
        return output.getvalue()
    finally:
        book.close()


def test_validate_then_apply_is_atomic_and_replay_safe(db):
    user, project_id, relation_id = _seed(db)
    adapter = _adapter(db, user)
    artifact, _snapshot = adapter.export(date(2026, 8, 1), hmac_key=HMAC_KEY)
    validation, batch = adapter.validate(
        date(2026, 8, 1),
        _set_plan(artifact.content, relation_id),
        hmac_key=HMAC_KEY,
    )
    assert validation.can_apply is True
    assert len(validation.milestone_changes) == 1
    db.commit()

    result = adapter.apply(batch.batch_id)
    db.commit()
    milestone = db.scalar(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.project_contract_id == relation_id,
            MaintenanceCollectionMilestone.sequence == 1,
        )
    )
    assert milestone is not None
    assert milestone.planned_amount == 25000
    assert result["changed_rows"] == 1
    assert db.get(MaintenanceManagerUploadBatch, batch.batch_id).status == "applied"
    assert db.get(MaintenanceManagerUploadBatchProject, (batch.batch_id, project_id)) is not None

    replay = adapter.apply(batch.batch_id)
    db.commit()
    assert replay == result
    assert db.scalar(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.project_contract_id == relation_id,
            MaintenanceCollectionMilestone.sequence == 1,
        )
    ).version == 1

    directory = project_operations(
        db,
        as_of=date(2026, 8, 9),
        user_ctx=_ctx(user),
        owner_scope="me",
    )
    monthly = next(
        task
        for task in directory["rows"][0]["task_summary"]["rows"]
        if task["task_type"] == "项目经理月度更新"
    )
    assert monthly["status"] == "completed"
    assert monthly["title"] == "已完成2026年08月月度全量工作簿"
    completed = project_operations(
        db,
        as_of=date(2026, 8, 9),
        user_ctx=_ctx(user),
        owner_scope="me",
        task_type="项目经理月度更新",
        task_status="completed",
    )
    assert completed["total"] == 1
    pending = project_operations(
        db,
        as_of=date(2026, 8, 9),
        user_ctx=_ctx(user),
        owner_scope="me",
        task_type="项目经理月度更新",
        task_status="pending",
    )
    assert pending["total"] == 0


def test_apply_creates_configured_acceptance_due_date_and_preview(db):
    user, project_id, _relation_id = _seed(
        db,
        username="acceptance_due_manager",
    )
    adapter = _adapter(db, user)
    artifact, _snapshot = adapter.export(date(2026, 8, 1), hmac_key=HMAC_KEY)
    validation, batch = adapter.validate(
        date(2026, 8, 1),
        _set_acceptance_due(artifact.content, date(2026, 10, 31)),
        hmac_key=HMAC_KEY,
    )
    assert len(validation.acceptance_due_date_changes) == 1
    assert batch.plan_json["preview_changes"] == [{
        "kind": "acceptance_due_date",
        "project_id": project_id,
        "project_code": "PM-acceptance_due_manager",
        "project_name": "合成项目经理工作簿项目",
        "contract_no": None,
        "sequence": None,
        "before": {
            "due_date": None,
            "configuration_state": "pending_business_configuration",
        },
        "after": {
            "due_date": "2026-10-31",
            "configuration_state": "configured",
        },
    }]
    db.commit()

    result = adapter.apply(batch.batch_id)
    db.commit()
    assert result["changed_rows"] == 1
    deliverable = db.scalar(
        select(MaintenanceAcceptanceDeliverable).where(
            MaintenanceAcceptanceDeliverable.project_id == project_id,
            MaintenanceAcceptanceDeliverable.deliverable_type == "acceptance_report",
        )
    )
    assert deliverable is not None
    assert deliverable.due_date == date(2026, 10, 31)
    assert deliverable.configuration_state == "configured"
    assert deliverable.version == 1


def test_scope_change_after_validation_fails_before_any_write(db):
    user, _project_id, relation_id = _seed(db, username="scope_conflict_manager")
    adapter = _adapter(db, user)
    artifact, _snapshot = adapter.export(date(2026, 8, 1), hmac_key=HMAC_KEY)
    _validation, batch = adapter.validate(
        date(2026, 8, 1),
        _set_plan(artifact.content, relation_id),
        hmac_key=HMAC_KEY,
    )
    db.commit()
    assignment = db.scalar(
        select(MaintenanceProjectUserAssignment).where(
            MaintenanceProjectUserAssignment.user_id == user.id,
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
    )
    assignment.archived_at = datetime.now(UTC)
    assignment.archived_by = "synthetic-admin"
    assignment.archive_reason = "合成范围冲突"
    assignment.version += 1
    db.commit()

    with pytest.raises(ManagerWorkbookConflict, match="范围"):
        adapter.apply(batch.batch_id)
    db.rollback()
    assert db.scalar(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.project_contract_id == relation_id
        )
    ) is None


def test_expired_preview_revalidates_into_same_idempotency_ledger(db):
    user, project_id, relation_id = _seed(
        db,
        username="expired_preview_manager",
    )
    adapter = _adapter(db, user)
    artifact, _snapshot = adapter.export(date(2026, 8, 1), hmac_key=HMAC_KEY)
    edited = _set_plan(artifact.content, relation_id)
    first_validation, batch = adapter.validate(
        date(2026, 8, 1),
        edited,
        hmac_key=HMAC_KEY,
    )
    batch.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    second_validation, refreshed = adapter.validate(
        date(2026, 8, 1),
        edited,
        hmac_key=HMAC_KEY,
    )
    db.commit()

    assert second_validation.validation_id == first_validation.validation_id
    assert refreshed.batch_id == batch.batch_id
    assert refreshed.status == "valid"
    assert refreshed.expires_at > datetime.now(UTC)
    assert len(second_validation.milestone_changes) == 1

    refreshed.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    project = db.get(MaintenanceProject, project_id)
    project.version += 1
    db.commit()

    with pytest.raises(ManagerWorkbookV3Error, match="数据版本已变化"):
        adapter.validate(
            date(2026, 8, 1),
            edited,
            hmac_key=HMAC_KEY,
        )
    db.rollback()
    unchanged_batch = db.get(MaintenanceManagerUploadBatch, batch.batch_id)
    assert unchanged_batch.expires_at <= datetime.now(UTC)


def test_applied_status_becomes_stale_when_current_scope_changes(db):
    user, _project_id, _relation_id = _seed(
        db,
        username="status_scope_manager",
    )
    adapter = _adapter(db, user)
    artifact, _snapshot = adapter.export(date(2026, 8, 1), hmac_key=HMAC_KEY)
    _validation, batch = adapter.validate(
        date(2026, 8, 1),
        artifact.content,
        hmac_key=HMAC_KEY,
    )
    db.commit()
    adapter.apply(batch.batch_id)
    db.commit()
    assert adapter.status(date(2026, 8, 1))["latest_batch"]["status"] == "applied"

    project = MaintenanceProject(
        project_id="status-scope-new-project",
        project_code="PM-STATUS-SCOPE-NEW",
        display_name="新增本人范围项目",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add_all([
        MaintenanceProjectUserAssignment(
            assignment_id="status-scope-new-assignment",
            project_id=project.project_id,
            responsibility_type="primary_manager",
            user_id=user.id,
            assigned_at=datetime.now(UTC),
            assigned_by="synthetic-admin",
            assignment_reason="新增范围验证",
        ),
        MaintenanceProjectContract(
            project_contract_id="status-scope-new-relation",
            project_id=project.project_id,
            contract_id="status-scope-new-contract",
            contract_no="XS-STATUS-SCOPE-NEW",
            contract_amount=1000,
            contract_status="active",
            status_mapping_state="mapped",
            status_mapping_version="synthetic-v1",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            source="synthetic-test",
        ),
    ])
    db.commit()

    current = adapter.status(date(2026, 8, 1))

    assert current["latest_batch"]["status"] == "stale_scope"
    assert current["latest_batch"]["scope_matches_current"] is False
    assert current["project_count"] == 2

    directory = project_operations(
        db,
        as_of=date(2026, 8, 9),
        user_ctx=_ctx(user),
        owner_scope="me",
        task_type="项目经理月度更新",
        task_status="pending",
    )
    assert directory["total"] == 2
    assert all(
        next(
            task
            for task in row["task_summary"]["rows"]
            if task["task_type"] == "项目经理月度更新"
        )["status"] == "pending"
        for row in directory["rows"]
    )
