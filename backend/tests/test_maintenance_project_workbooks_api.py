"""Public API contract for the stable-project four-sheet workbook loop."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.utils import range_boundaries
import pytest
from sqlalchemy import func, select

from app import auth
from app.api import maintenance_project_operations, maintenance_project_workbooks
from app.auth import hash_password
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectWorkbookOperation,
    MaintenanceProjectWorkbookState,
    MaintenanceProjectWorkbookValidation,
)
from app.models.system import SysUser
from app.services import maintenance_project_workbook_adapter
from app.services.maintenance_project_workbook_v2 import COLLECTION_TABLE, PROTOCOL_ID


def _client(db, *, username: str) -> TestClient:
    db.add(
        SysUser(
            username=username,
            role="admin",
            display_name="合成工作簿管理员",
            password_hash=hash_password("synthetic-password-123"),
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
    app.include_router(maintenance_project_workbooks.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _project_and_contract(client: TestClient, db, *, suffix: str) -> tuple[str, dict]:
    project_id = f"project-workbook-{suffix}"
    db.add(
        MaintenanceProject(
            project_id=project_id,
            project_code=f"WB-{suffix}",
            display_name=f"合成工作簿项目 {suffix}",
            project_manager_id="manager-synthetic",
            lifecycle_status="ongoing",
        )
    )
    db.commit()
    response = client.post(
        f"/api/maintenance/projects/stable/{project_id}/contracts",
        json={
            "contract_id": f"contract-{suffix}",
            "contract_no": f"XS-{suffix}",
            "contract_amount": "1000.00",
            "contract_status": "active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立工作簿接口测试合同",
        },
    )
    assert response.status_code == 201, response.text
    return project_id, response.json()


def _append_collection(
    source: bytes,
    *,
    project_contract_id: str,
    contract_no: str,
    report_month: str = "2026-08",
    amount: str = "320.00",
) -> bytes:
    book = load_workbook(BytesIO(source))
    try:
        sheet = book["01_总览"]
        table = sheet.tables[COLLECTION_TABLE]
        min_col, min_row, max_col, max_row = range_boundaries(table.ref)
        assert min_col == 1 and max_col == 12
        target = next(
            row
            for row in range(min_row + 1, max_row + 1)
            if sheet.cell(row, 9).value in (None, "")
            and all(
                sheet.cell(row, column).value in (None, "") for column in range(1, 9)
            )
        )
        sheet.cell(target, 1, "CREATE")
        sheet.cell(target, 2, project_contract_id)
        sheet.cell(target, 3, contract_no)
        sheet.cell(target, 4, report_month)
        sheet.cell(target, 5, amount)
        sheet.cell(target, 6, "SYNTHETIC-VOUCHER-001")
        sheet.cell(target, 7, "已确认")
        sheet.cell(target, 8, "合成月度回填")
        output = BytesIO()
        book.save(output)
        return output.getvalue()
    finally:
        book.close()


def _download(client: TestClient, project_id: str) -> bytes:
    response = client.get(f"/api/maintenance/projects/stable/{project_id}/workbook")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    return response.content


def _validate(client: TestClient, project_id: str, content: bytes, name="update.xlsx"):
    return client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/validate",
        files={
            "file": (
                name,
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


def test_export_validate_apply_is_a_server_owned_atomic_loop(db):
    client = _client(db, username="workbook_loop_admin")
    project_id, contract = _project_and_contract(client, db, suffix="loop")
    workbook = _download(client, project_id)

    export_state = db.get(MaintenanceProjectWorkbookState, project_id)
    db.refresh(export_state)
    assert export_state.last_export_id
    assert export_state.last_exported_at
    export_ledgers = db.scalar(
        select(func.count())
        .select_from(MaintenanceProjectWorkbookOperation)
        .where(MaintenanceProjectWorkbookOperation.operation_type == "file_export")
    )
    assert export_ledgers == 1

    edited = _append_collection(
        workbook,
        project_contract_id=contract["project_contract_id"],
        contract_no=contract["contract_no"],
    )
    validated = _validate(client, project_id, edited)
    assert validated.status_code == 200, validated.text
    plan = validated.json()
    assert set(plan) == {
        "validation_token",
        "project_id",
        "data_version",
        "filename",
        "preview",
        "changes",
        "warnings",
        "errors",
        "can_apply",
    }
    assert plan["project_id"] == project_id
    assert plan["filename"] == "update.xlsx"
    assert plan["changes"] == {"collection_append": 1}
    assert plan["errors"] == []
    assert plan["can_apply"] is True
    assert plan["preview"]["protocol_version"] == PROTOCOL_ID
    assert plan["preview"]["latest_tracking_month"] == "2026-08"
    assert validated.headers["cache-control"] == "no-store"
    assert validated.headers["x-content-type-options"] == "nosniff"
    assert [sheet["code"] for sheet in plan["preview"]["sheets"]] == [
        "overview",
        "site_requisitions",
        "approved_expenses",
        "manager_tracking",
    ]
    assert [sheet["name"] for sheet in plan["preview"]["sheets"]] == [
        "01_总览",
        "02_备件消耗",
        "03_报销单",
        "04_项目经理追踪与提醒",
    ]

    validation_row = db.get(
        MaintenanceProjectWorkbookValidation, plan["validation_token"]
    )
    db.refresh(validation_row)
    assert validation_row.status == "valid"
    assert validation_row.plan_json["creates"][0]["cumulative_amount"] == "320.00"

    applied = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json={
            "validation_token": plan["validation_token"],
            "data_version": plan["data_version"],
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert applied.json()["changed_rows"] == 1
    assert applied.json()["data_version"] != plan["data_version"]
    assert applied.headers["cache-control"] == "no-store"
    assert applied.headers["x-content-type-options"] == "nosniff"

    db.expire_all()
    collection = db.scalar(
        select(MaintenanceCollectionSnapshot).where(
            MaintenanceCollectionSnapshot.project_id == project_id
        )
    )
    assert collection is not None
    assert collection.report_month == date(2026, 8, 1)
    assert str(collection.cumulative_amount) == "320.00"
    assert collection.status == "confirmed"
    assert db.get(MaintenanceProjectWorkbookState, project_id).revision == 2
    assert (
        db.get(MaintenanceProjectWorkbookValidation, plan["validation_token"]).status
        == "applied"
    )
    operation_types = list(
        db.scalars(
            select(MaintenanceProjectWorkbookOperation.operation_type)
            .where(MaintenanceProjectWorkbookOperation.project_id == project_id)
            .order_by(MaintenanceProjectWorkbookOperation.id)
        )
    )
    assert operation_types == ["file_export", "collection_create", "file_apply"]


def test_invalid_file_returns_persisted_error_workbook_without_writes(db):
    client = _client(db, username="workbook_error_admin")
    project_id, _contract = _project_and_contract(client, db, suffix="error")
    before_revision = db.get(MaintenanceProjectWorkbookState, project_id).revision

    response = _validate(client, project_id, b"not-an-xlsx")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["can_apply"] is False
    assert result["errors"]
    assert result["changes"] == {"collection_append": 0}

    error_file = client.get(
        f"/api/maintenance/workbook-validations/{result['validation_token']}/errors.xlsx"
    )
    assert error_file.status_code == 200, error_file.text
    assert error_file.content.startswith(b"PK")
    assert error_file.headers["cache-control"] == "no-store"
    assert error_file.headers["x-content-type-options"] == "nosniff"
    other_client = _client(db, username="workbook_error_other_admin")
    wrong_user = other_client.get(
        f"/api/maintenance/workbook-validations/{result['validation_token']}/errors.xlsx"
    )
    assert wrong_user.status_code == 404
    row = db.get(MaintenanceProjectWorkbookValidation, result["validation_token"])
    db.refresh(row)
    assert row.status == "error"
    assert row.plan_json is None
    assert (
        db.get(MaintenanceProjectWorkbookState, project_id).revision == before_revision
    )
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 0
    )


def test_unchanged_monthly_workbook_can_confirm_zero_row_update(db):
    client = _client(db, username="workbook_unchanged_admin")
    project_id, _contract = _project_and_contract(client, db, suffix="unchanged")
    workbook = _download(client, project_id)

    validated = _validate(client, project_id, workbook)
    assert validated.status_code == 200, validated.text
    plan = validated.json()
    assert plan["changes"] == {"collection_append": 0}
    assert plan["can_apply"] is True
    assert plan["warnings"] == ["未检测到新增回款；确认后将记录本月已更新"]
    before = db.get(MaintenanceProjectWorkbookState, project_id)
    db.refresh(before)
    before_revision = before.revision

    applied = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json={
            "validation_token": plan["validation_token"],
            "data_version": plan["data_version"],
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert applied.json()["changed_rows"] == 0
    assert applied.json()["data_version"] != plan["data_version"]

    db.expire_all()
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    assert state.revision == before_revision + 1
    assert state.last_applied_at is not None
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 0
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(MaintenanceProjectWorkbookOperation)
            .where(MaintenanceProjectWorkbookOperation.operation_type == "file_apply")
        )
        == 1
    )
    assert (
        db.get(MaintenanceProjectWorkbookValidation, plan["validation_token"]).status
        == "applied"
    )

    replay = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json={
            "validation_token": plan["validation_token"],
            "data_version": applied.json()["data_version"],
        },
    )
    assert replay.status_code == 409


def test_export_preserves_approved_expense_business_fields(db):
    client = _client(db, username="workbook_expense_admin")
    project_id, contract = _project_and_contract(client, db, suffix="expense")
    created = client.post(
        f"/api/maintenance/projects/stable/{project_id}/expenses",
        json={
            "expense_id": "expense-workbook-001",
            "project_contract_id": contract["project_contract_id"],
            "expense_ref": "BX-WORKBOOK-001",
            "expense_date": "2026-08-01",
            "applicant": "合成报销人",
            "category": "差旅费",
            "expense_reason": "项目现场支持",
            "amount_ex_tax": "50.00",
            "raw_status": "synthetic-finished",
            "status_mapping_state": "mapped",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "验证报销字段进入工作簿",
        },
    )
    assert created.status_code == 201, created.text

    book = load_workbook(BytesIO(_download(client, project_id)), data_only=False)
    try:
        sheet = book["03_报销单"]
        assert [sheet.cell(2, column).value for column in range(1, 9)] == [
            "expense-workbook-001",
            "BX-WORKBOOK-001",
            datetime(2026, 8, 1, 0, 0),
            "合成报销人",
            "差旅费",
            50,
            "已审批",
            "项目现场支持",
        ]
    finally:
        book.close()


def test_apply_rejects_expired_wrong_project_replay_and_client_plan(db):
    client = _client(db, username="workbook_fail_close_admin")
    project_id, contract = _project_and_contract(client, db, suffix="source")
    other_project_id, _ = _project_and_contract(client, db, suffix="other")
    content = _append_collection(
        _download(client, project_id),
        project_contract_id=contract["project_contract_id"],
        contract_no=contract["contract_no"],
    )
    plan = _validate(client, project_id, content).json()
    body = {
        "validation_token": plan["validation_token"],
        "data_version": plan["data_version"],
    }

    wrong_project = client.post(
        f"/api/maintenance/projects/stable/{other_project_id}/workbook/apply",
        json=body,
    )
    assert wrong_project.status_code == 409
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 0
    )

    injected = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json={**body, "plan": {"creates": [{"cumulative_amount": "0.01"}]}},
    )
    assert injected.status_code == 422
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 0
    )

    row = db.get(MaintenanceProjectWorkbookValidation, plan["validation_token"])
    db.refresh(row)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    expired = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json=body,
    )
    assert expired.status_code == 409
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 0
    )

    row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.commit()
    applied = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json=body,
    )
    assert applied.status_code == 200, applied.text
    replay = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json=body,
    )
    assert replay.status_code == 409
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 1
    )


def test_validate_requires_exactly_one_xlsx_file(db):
    client = _client(db, username="workbook_upload_admin")
    project_id, _contract = _project_and_contract(client, db, suffix="upload")

    wrong_extension = _validate(client, project_id, b"content", name="update.xls")
    assert wrong_extension.status_code == 400
    multiple = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/validate",
        files=[
            ("file", ("one.xlsx", b"one", "application/octet-stream")),
            ("file", ("two.xlsx", b"two", "application/octet-stream")),
        ],
    )
    assert multiple.status_code == 400 or multiple.status_code == 422


def test_stale_plan_and_same_file_replay_fail_closed(db):
    client = _client(db, username="workbook_stale_admin")
    project_id, contract = _project_and_contract(client, db, suffix="stale")
    content = _append_collection(
        _download(client, project_id),
        project_contract_id=contract["project_contract_id"],
        contract_no=contract["contract_no"],
    )
    first_plan = _validate(client, project_id, content).json()
    cloned_plan = _validate(client, project_id, content).json()

    changed = client.post(
        f"/api/maintenance/projects/stable/{project_id}/collections",
        json={
            "project_contract_id": contract["project_contract_id"],
            "report_month": "2026-07-01",
            "cumulative_amount": "100.00",
            "status": "confirmed",
            "reason": "制造服务端 revision 变化",
        },
    )
    assert changed.status_code == 201, changed.text
    stale = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json={
            "validation_token": first_plan["validation_token"],
            "data_version": first_plan["data_version"],
        },
    )
    assert stale.status_code == 409
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 1
    )

    # A fresh export is required after any project revision change.
    fresh_content = _append_collection(
        _download(client, project_id),
        project_contract_id=contract["project_contract_id"],
        contract_no=contract["contract_no"],
    )
    applied_plan = _validate(client, project_id, fresh_content).json()
    duplicate_plan = _validate(client, project_id, fresh_content).json()
    applied = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json={
            "validation_token": applied_plan["validation_token"],
            "data_version": applied_plan["data_version"],
        },
    )
    assert applied.status_code == 200, applied.text
    replay = client.post(
        f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
        json={
            "validation_token": duplicate_plan["validation_token"],
            "data_version": applied.json()["data_version"],
        },
    )
    assert replay.status_code == 409
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 2
    )
    assert cloned_plan["validation_token"] != first_plan["validation_token"]


def test_unexpected_second_row_failure_rolls_back_first_row_and_ledgers(
    db, monkeypatch
):
    client = _client(db, username="workbook_atomic_admin")
    project_id, contract = _project_and_contract(client, db, suffix="atomic")
    content = _append_collection(
        _download(client, project_id),
        project_contract_id=contract["project_contract_id"],
        contract_no=contract["contract_no"],
        report_month="2026-07",
        amount="100.00",
    )
    content = _append_collection(
        content,
        project_contract_id=contract["project_contract_id"],
        contract_no=contract["contract_no"],
        report_month="2026-08",
        amount="200.00",
    )
    plan = _validate(client, project_id, content).json()
    before_revision = db.get(MaintenanceProjectWorkbookState, project_id).revision
    original_create = maintenance_project_workbook_adapter.operations.create_collection
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second-row failure")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(
        maintenance_project_workbook_adapter.operations,
        "create_collection",
        fail_on_second,
    )
    with pytest.raises(RuntimeError, match="second-row failure"):
        client.post(
            f"/api/maintenance/projects/stable/{project_id}/workbook/apply",
            json={
                "validation_token": plan["validation_token"],
                "data_version": plan["data_version"],
            },
        )

    db.expire_all()
    assert (
        db.scalar(select(func.count()).select_from(MaintenanceCollectionSnapshot)) == 0
    )
    assert (
        db.get(MaintenanceProjectWorkbookState, project_id).revision == before_revision
    )
    assert (
        db.get(MaintenanceProjectWorkbookValidation, plan["validation_token"]).status
        == "valid"
    )
    applied_ledgers = db.scalar(
        select(func.count())
        .select_from(MaintenanceProjectWorkbookOperation)
        .where(
            MaintenanceProjectWorkbookOperation.operation_type.in_(
                ["collection_create", "file_apply"]
            )
        )
    )
    assert applied_ledgers == 0
