"""Public workflow tests for maintenance bad-part return obligations."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import auth
from app.api import maintenance_bad_returns, maintenance_project_operations
from app.auth import hash_password
from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.maintenance_bad_return import (
    MaintenanceBadReturnCommand,
    MaintenanceReturnObligation,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.master_data import ProductCategory
from app.models.system import SysUser
from app.services import maintenance_bad_returns as bad_return_service
from tests.test_site_issue_v2_api import _delivery_source, _project


def _client(
    db,
    *,
    username: str,
    role: str = "admin",
    permissions: dict | None = None,
) -> TestClient:
    db.add(
        SysUser(
            username=username,
            role=role,
            display_name="合成坏件返还操作人",
            password_hash=hash_password("synthetic-password-123"),
            permissions=permissions,
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
    app.include_router(maintenance_project_operations.site_issue_router, prefix="/api")
    app.include_router(maintenance_bad_returns.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _category(db, *, major: str, minor: str | None) -> ProductCategory:
    row = ProductCategory(category_major=major, category_minor=minor)
    db.add(row)
    db.flush()
    return row


def _confirm_issue(
    client: TestClient,
    *,
    project_id: str,
    lines: list[dict],
    suffix: str,
) -> dict:
    created = client.post(
        f"/api/maintenance/projects/stable/{project_id}/site-issues",
        json={
            "idempotency_key": f"synthetic-bad-return-create-{suffix}",
            "issue_date": "2026-08-09",
            "receiver": "合成接收人",
            "issued_by": "合成发出人",
            "site_location": "合成现场",
            "lines": lines,
            "reason": "建立返还义务来源领用",
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    confirmed = client.post(
        f"/api/maintenance/site-issues/{draft['issue_id']}/confirm",
        json={
            "project_id": project_id,
            "version": draft["version"],
            "idempotency_key": f"synthetic-bad-return-confirm-{suffix}",
            "reason": "确认并生成稳定返还义务",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _one_required_obligation(
    db,
    client: TestClient,
    *,
    project_id: str,
    quantity: str = "5",
    suffix: str,
) -> tuple[dict, MaintenanceReturnObligation]:
    project = _project(db, project_id=project_id)
    category = _category(db, major="服务器配件", minor="电源")
    delivery = _delivery_source(
        db,
        project=project,
        delivery_line_id=f"synthetic-return-source-{suffix}",
        quantity=quantity,
    )
    part = db.get(DimPart, delivery.part_id)
    part.category_id = category.id
    db.commit()
    confirmed = _confirm_issue(
        client,
        project_id=project_id,
        lines=[{"delivery_line_id": delivery.delivery_line_id, "quantity": quantity}],
        suffix=suffix,
    )
    obligation = db.query(MaintenanceReturnObligation).filter_by(
        issue_id=confirmed["issue_id"]
    ).one()
    return confirmed, obligation


def test_confirm_projects_exact_category_evidence_and_incomplete_rate(db):
    project = _project(db, project_id="project-bad-return-category")
    client = _client(db, username="bad_return_category_admin")
    normal_category = _category(db, major="服务器配件", minor="电源")
    disk_category = _category(db, major="硬盘", minor="SAS-HDD")
    normal = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-normal",
        quantity="3",
    )
    disk = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-disk",
        quantity="2",
    )
    pending = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-pending",
        quantity="1",
    )
    db.get(DimPart, normal.part_id).category_id = normal_category.id
    db.get(DimPart, disk.part_id).category_id = disk_category.id
    # A legacy text field that says 硬盘 is not standard-category evidence.
    db.get(DimPart, pending.part_id).category_major = "硬盘"
    db.commit()

    confirmed = _confirm_issue(
        client,
        project_id=project.project_id,
        lines=[
            {"delivery_line_id": normal.delivery_line_id, "quantity": "3"},
            {"delivery_line_id": disk.delivery_line_id, "quantity": "2"},
            {"delivery_line_id": pending.delivery_line_id, "quantity": "1"},
        ],
        suffix="category",
    )
    directory = client.post(
        "/api/maintenance/return-obligations/search",
        json={"project_id": project.project_id, "page": 1, "page_size": 20},
    )
    assert directory.status_code == 200, directory.text
    payload = directory.json()
    rows = {row["delivery_line_id"]: row for row in payload["rows"]}
    assert rows[normal.delivery_line_id]["classification"] == "required"
    assert rows[normal.delivery_line_id]["required_quantity"] == "3.000"
    assert rows[disk.delivery_line_id]["classification"] == "exempt"
    assert rows[disk.delivery_line_id]["category_major_snapshot"] == "硬盘"
    assert rows[disk.delivery_line_id]["required_quantity"] == "0.000"
    assert rows[pending.delivery_line_id]["classification"] == "pending_category"
    assert rows[pending.delivery_line_id]["category_id_snapshot"] is None
    assert payload["return_rate"]["status"] == "basis_incomplete"
    assert payload["return_rate"]["official_rate_pct"] is None
    assert payload["return_rate"]["required_quantity"] == "3.000"
    assert payload["return_rate"]["exempt_quantity"] == "2.000"
    assert payload["return_rate"]["pending_quantity"] == "1.000"

    event = db.query(MaintenanceSiteIssueReturnEvent).filter_by(
        issue_id=confirmed["issue_id"]
    ).one()
    assert event.downstream_reference.startswith("maintenance-return-obligations:")
    assert event.consumed_at is not None

    # Later master-data changes do not rewrite the frozen classification.
    db.get(DimPart, disk.part_id).category_id = normal_category.id
    db.commit()
    unchanged = client.post(
        "/api/maintenance/return-obligations/search",
        json={"project_id": project.project_id, "page": 1, "page_size": 20},
    ).json()
    unchanged_rows = {row["delivery_line_id"]: row for row in unchanged["rows"]}
    assert unchanged_rows[disk.delivery_line_id]["classification"] == "exempt"
    assert unchanged_rows[disk.delivery_line_id]["category_major_snapshot"] == "硬盘"


def test_correction_deactivates_replaced_obligation_without_double_counting(db):
    project = _project(db, project_id="project-bad-return-correction")
    client = _client(db, username="bad_return_correction_admin")
    category = _category(db, major="服务器配件", minor="电源")
    original = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-correction-original",
        quantity="3",
    )
    replacement = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-correction-replacement",
        quantity="4",
    )
    db.get(DimPart, original.part_id).category_id = category.id
    db.get(DimPart, replacement.part_id).category_id = category.id
    db.commit()
    confirmed = _confirm_issue(
        client,
        project_id=project.project_id,
        lines=[{"delivery_line_id": original.delivery_line_id, "quantity": "3"}],
        suffix="correction",
    )

    corrected_response = client.patch(
        f"/api/maintenance/site-issues/{confirmed['issue_id']}",
        json={
            "project_id": project.project_id,
            "version": confirmed["version"],
            "idempotency_key": "synthetic-return-correction-replace",
            "lines": [
                {
                    "delivery_line_id": replacement.delivery_line_id,
                    "quantity": "2",
                }
            ],
            "reason": "更正为实际领用的稳定发货明细",
        },
    )
    assert corrected_response.status_code == 200, corrected_response.text
    corrected = corrected_response.json()

    metadata_response = client.patch(
        f"/api/maintenance/site-issues/{confirmed['issue_id']}",
        json={
            "project_id": project.project_id,
            "version": corrected["version"],
            "idempotency_key": "synthetic-return-correction-metadata",
            "receiver": "更正后的合成接收人",
            "reason": "只更正接收人，不制造重复返还义务",
        },
    )
    assert metadata_response.status_code == 200, metadata_response.text

    all_obligations = (
        db.query(MaintenanceReturnObligation)
        .filter_by(issue_id=confirmed["issue_id"])
        .order_by(MaintenanceReturnObligation.delivery_line_id)
        .all()
    )
    assert len(all_obligations) == 2
    by_delivery = {row.delivery_line_id: row for row in all_obligations}
    assert by_delivery[original.delivery_line_id].is_active is False
    assert by_delivery[replacement.delivery_line_id].is_active is True
    assert by_delivery[replacement.delivery_line_id].source_quantity == 2

    active_directory = client.post(
        "/api/maintenance/return-obligations/search",
        json={"project_id": project.project_id, "page": 1, "page_size": 20},
    ).json()
    assert active_directory["total"] == 1
    assert active_directory["rows"][0]["delivery_line_id"] == replacement.delivery_line_id
    assert active_directory["return_rate"]["required_quantity"] == "2.000"
    assert active_directory["return_rate"]["required_count"] == 1

    full_directory = client.post(
        "/api/maintenance/return-obligations/search",
        json={
            "project_id": project.project_id,
            "active_only": False,
            "page": 1,
            "page_size": 20,
        },
    ).json()
    assert full_directory["total"] == 2
    assert full_directory["return_rate"]["required_quantity"] == "2.000"


def test_correction_drains_legacy_pending_creation_before_newer_projection(db):
    project = _project(db, project_id="project-bad-return-legacy-pending")
    client = _client(db, username="bad_return_legacy_pending_admin")
    category = _category(db, major="服务器配件", minor="电源")
    delivery = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-legacy-pending",
        quantity="5",
    )
    db.get(DimPart, delivery.part_id).category_id = category.id
    db.commit()
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/site-issues",
        json={
            "idempotency_key": "synthetic-return-create-legacy-pending",
            "issue_date": "2026-08-09",
            "receiver": "合成接收人",
            "issued_by": "合成发出人",
            "site_location": "合成现场",
            "lines": [
                {"delivery_line_id": delivery.delivery_line_id, "quantity": "3"}
            ],
            "reason": "建立升级边界前的现场领用草稿",
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()

    # Simulate the supported upgrade boundary without violating #207's
    # append-only event trigger: confirmation and its pending event already
    # exist, while #208's obligation projection has not run yet.
    issue = db.get(MaintenanceSiteIssue, draft["issue_id"])
    line = db.query(MaintenanceSiteIssueLine).filter_by(issue_id=issue.issue_id).one()
    issue.raw_status = "confirmed"
    issue.status_mapping_state = "mapped"
    issue.normalized_status = "confirmed"
    issue.status_mapping_version = "site-issue-v2-workflow-v1"
    issue.confirmed_at = datetime.now(UTC)
    issue.version += 1
    source_event = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()),
        project_id=project.project_id,
        issue_id=issue.issue_id,
        event_type="return_obligation_created",
        issue_version=issue.version,
        payload={
            "schema_version": "maintenance-return-obligation-interface-v1",
            "project_id": project.project_id,
            "issue_id": issue.issue_id,
            "issue_no": issue.issue_no,
            "issue_date": issue.issue_date.isoformat(),
            "receiver": issue.receiver,
            "issued_by": issue.issued_by,
            "site_location": issue.site_location,
            "lines": [
                {
                    "issue_line_id": line.issue_line_id,
                    "delivery_line_id": line.delivery_line_id,
                    "source_order_id": line.source_order_id,
                    "source_line_id": line.source_line_id,
                    "part_id": line.part_id,
                    "pn": line.pn,
                    "serial_number": line.serial_number,
                    "quantity": "3.000",
                }
            ],
        },
    )
    db.add(source_event)
    db.commit()
    confirmed_version = issue.version

    corrected = client.patch(
        f"/api/maintenance/site-issues/{issue.issue_id}",
        json={
            "project_id": project.project_id,
            "version": confirmed_version,
            "idempotency_key": "synthetic-return-legacy-pending-correct",
            "lines": [
                {"delivery_line_id": delivery.delivery_line_id, "quantity": "2"}
            ],
            "reason": "更正旧版本尚未投影的现场领用",
        },
    )
    assert corrected.status_code == 200, corrected.text

    directory = client.post(
        "/api/maintenance/return-obligations/search",
        json={"project_id": project.project_id, "page": 1, "page_size": 20},
    )
    assert directory.status_code == 200, directory.text
    row = directory.json()["rows"][0]
    assert row["source_quantity"] == "2.000"
    assert row["required_quantity"] == "2.000"
    assert row["source_issue_version"] == corrected.json()["version"]
    db.refresh(source_event)
    assert source_event.downstream_reference.startswith(
        "maintenance-return-obligations:"
    )
    assert source_event.consumed_at is not None


def test_late_stale_return_event_is_consumed_without_rolling_back_projection(db):
    project = _project(db, project_id="project-bad-return-stale-event")
    client = _client(db, username="bad_return_stale_event_admin")
    category = _category(db, major="服务器配件", minor="电源")
    delivery = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-stale-event",
        quantity="5",
    )
    db.get(DimPart, delivery.part_id).category_id = category.id
    db.commit()
    confirmed = _confirm_issue(
        client,
        project_id=project.project_id,
        lines=[{"delivery_line_id": delivery.delivery_line_id, "quantity": "2"}],
        suffix="stale-event",
    )
    issue = db.get(MaintenanceSiteIssue, confirmed["issue_id"])
    line = db.query(MaintenanceSiteIssueLine).filter_by(issue_id=issue.issue_id).one()

    # A delayed legacy writer publishes an older source version after the
    # current confirmation was already projected. It must be acknowledged but
    # must never rewrite the authoritative v2 quantity back to 4.
    stale_event = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()),
        project_id=project.project_id,
        issue_id=issue.issue_id,
        event_type="return_obligation_created",
        issue_version=1,
        payload={
            "schema_version": "maintenance-return-obligation-interface-v1",
            "project_id": project.project_id,
            "issue_id": issue.issue_id,
            "issue_no": issue.issue_no,
            "issue_date": issue.issue_date.isoformat(),
            "receiver": issue.receiver,
            "issued_by": issue.issued_by,
            "site_location": issue.site_location,
            "lines": [
                {
                    "issue_line_id": line.issue_line_id,
                    "delivery_line_id": line.delivery_line_id,
                    "source_order_id": line.source_order_id,
                    "source_line_id": line.source_line_id,
                    "part_id": line.part_id,
                    "pn": line.pn,
                    "serial_number": line.serial_number,
                    "quantity": "4.000",
                }
            ],
        },
    )
    db.add(stale_event)
    db.commit()

    directory = client.post(
        "/api/maintenance/return-obligations/search",
        json={"project_id": project.project_id, "page": 1, "page_size": 20},
    )
    assert directory.status_code == 200, directory.text
    row = directory.json()["rows"][0]
    assert row["source_quantity"] == "2.000"
    assert row["required_quantity"] == "2.000"
    assert row["source_issue_version"] == confirmed["version"]
    db.refresh(stale_event)
    assert stale_event.downstream_reference.startswith(
        "maintenance-return-obligations:"
    )
    assert stale_event.consumed_at is not None


def test_stale_creation_cannot_resurrect_empty_newer_void_projection(db):
    project = _project(db, project_id="project-bad-return-empty-void")
    client = _client(db, username="bad_return_empty_void_watermark_admin")
    category = _category(db, major="服务器配件", minor="电源")
    delivery = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-empty-void-watermark",
        quantity="3",
    )
    db.get(DimPart, delivery.part_id).category_id = category.id
    db.commit()
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/site-issues",
        json={
            "idempotency_key": "synthetic-return-empty-void-create",
            "issue_date": "2026-08-09",
            "receiver": "合成接收人",
            "issued_by": "合成发出人",
            "site_location": "合成现场",
            "lines": [
                {"delivery_line_id": delivery.delivery_line_id, "quantity": "3"}
            ],
            "reason": "建立空投影水位测试草稿",
        },
    )
    assert created.status_code == 201, created.text
    issue = db.get(MaintenanceSiteIssue, created.json()["issue_id"])
    line = db.query(MaintenanceSiteIssueLine).filter_by(issue_id=issue.issue_id).one()
    issue.raw_status = "void"
    issue.status_mapping_state = "mapped"
    issue.normalized_status = "void"
    issue.status_mapping_version = "site-issue-v2-workflow-v1"
    issue.voided_at = datetime.now(UTC)
    issue.version = 2
    payload = {
        "schema_version": "maintenance-return-obligation-interface-v1",
        "project_id": project.project_id,
        "issue_id": issue.issue_id,
        "issue_no": issue.issue_no,
        "issue_date": issue.issue_date.isoformat(),
        "receiver": issue.receiver,
        "issued_by": issue.issued_by,
        "site_location": issue.site_location,
        "lines": [
            {
                "issue_line_id": line.issue_line_id,
                "delivery_line_id": line.delivery_line_id,
                "source_order_id": line.source_order_id,
                "source_line_id": line.source_line_id,
                "part_id": line.part_id,
                "pn": line.pn,
                "serial_number": line.serial_number,
                "quantity": "3.000",
            }
        ],
    }
    void_event = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()),
        project_id=project.project_id,
        issue_id=issue.issue_id,
        event_type="return_obligation_voided",
        issue_version=2,
        payload=payload,
    )
    db.add(void_event)
    db.commit()

    empty = client.post(
        "/api/maintenance/return-obligations/search",
        json={"project_id": project.project_id, "page": 1, "page_size": 20},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["total"] == 0
    db.refresh(void_event)
    assert void_event.consumed_at is not None

    stale_event = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()),
        project_id=project.project_id,
        issue_id=issue.issue_id,
        event_type="return_obligation_created",
        issue_version=1,
        payload=payload,
    )
    db.add(stale_event)
    db.commit()
    still_empty = client.post(
        "/api/maintenance/return-obligations/search",
        json={"project_id": project.project_id, "page": 1, "page_size": 20},
    )
    assert still_empty.status_code == 200, still_empty.text
    assert still_empty.json()["total"] == 0
    db.refresh(stale_event)
    assert stale_event.downstream_reference.startswith(
        "maintenance-return-obligations:"
    )
    assert stale_event.consumed_at is not None


def test_confirmed_site_issue_can_be_fully_voided_before_any_return_registration(db):
    client = _client(db, username="bad_return_source_void_admin")
    confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="project-bad-return-source-void",
        quantity="3",
        suffix="source-void",
    )
    before = client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()
    assert before["required_quantity"] == "3.000"

    voided = client.post(
        f"/api/maintenance/site-issues/{confirmed['issue_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": confirmed["version"],
            "idempotency_key": "synthetic-source-full-void",
            "reason": "整张现场领用误确认且尚未登记返还，原子撤回",
        },
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["workflow_status"] == "void"
    directory = client.post(
        "/api/maintenance/return-obligations/search",
        json={"project_id": obligation.project_id, "page": 1, "page_size": 20},
    ).json()
    assert directory["rows"] == []
    assert directory["return_rate"]["status"] == "no_return_required"
    assert directory["return_rate"]["required_quantity"] == "0.000"


def test_confirmed_site_issue_void_fails_closed_after_return_registration(db):
    client = _client(db, username="bad_return_source_void_blocked_admin")
    confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="proj-return-source-void-blocked",
        quantity="3",
        suffix="source-void-blocked",
    )
    created = client.post(
        "/api/maintenance/bad-returns",
        json={
            "project_id": obligation.project_id,
            "idempotency_key": "synthetic-source-void-blocked-create",
            "lines": [{"obligation_id": obligation.obligation_id, "quantity": "1"}],
            "reason": "建立不可被上游整单撤回的返还登记",
        },
    ).json()
    submitted = client.post(
        f"/api/maintenance/bad-returns/{created['return_id']}/submit",
        json={
            "project_id": obligation.project_id,
            "version": created["version"],
            "idempotency_key": "synthetic-source-void-blocked-submit",
            "reason": "提交返还登记后锁定上游义务",
        },
    )
    assert submitted.status_code == 200, submitted.text

    rejected = client.post(
        f"/api/maintenance/site-issues/{confirmed['issue_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": confirmed["version"],
            "idempotency_key": "synthetic-source-full-void-blocked",
            "reason": "已有返还登记时必须拒绝整单撤回",
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert "已有返还登记" in rejected.json()["detail"]
    db.expire_all()
    assert db.get(MaintenanceReturnObligation, obligation.obligation_id).is_active is True
    rate = client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()
    assert rate["required_quantity"] == "3.000"
    assert rate["registered_quantity"] == "1.000"


def test_workspace_rate_synchronously_consumes_pending_project_event(db):
    client = _client(db, username="bad_return_pending_projection_admin")
    confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="proj-return-pending-projection",
        quantity="2",
        suffix="pending-projection",
    )
    source_event = db.query(MaintenanceSiteIssueReturnEvent).filter_by(
        issue_id=confirmed["issue_id"],
        event_type="return_obligation_created",
    ).one()
    pending_event = MaintenanceSiteIssueReturnEvent(
        event_id="00000000-0000-0000-0000-000000000999",
        project_id=obligation.project_id,
        issue_id=confirmed["issue_id"],
        event_type="return_obligation_voided",
        issue_version=confirmed["version"] + 1,
        payload=source_event.payload,
    )
    db.add(pending_event)
    db.commit()

    workspace = client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/workspace",
        params={"as_of": "2026-08-09"},
    )
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["return_rate"]["status"] == "no_return_required"
    assert workspace.json()["return_rate"]["required_quantity"] == "0.000"
    db.expire_all()
    projected = db.get(MaintenanceSiteIssueReturnEvent, pending_event.event_id)
    assert projected.downstream_reference.startswith("maintenance-return-obligations:")
    assert projected.consumed_at is not None
    assert db.get(MaintenanceReturnObligation, obligation.obligation_id).is_active is False


def test_admin_resolves_pending_category_by_linking_standard_category_only(db):
    project = _project(db, project_id="project-bad-return-resolve")
    admin = _client(db, username="bad_return_resolve_admin")
    category = _category(db, major="服务器配件", minor="电源")
    delivery = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-resolve",
        quantity="4",
    )
    _confirm_issue(
        admin,
        project_id=project.project_id,
        lines=[{"delivery_line_id": delivery.delivery_line_id, "quantity": "4"}],
        suffix="resolve",
    )
    obligation = db.query(MaintenanceReturnObligation).filter_by(
        delivery_line_id=delivery.delivery_line_id
    ).one()
    assert obligation.classification == "pending_category"

    options = admin.get("/api/maintenance/return-categories")
    assert options.status_code == 200, options.text
    assert options.json() == {
        "categories": [
            {
                "category_id": category.id,
                "category_major": "服务器配件",
                "category_minor": "电源",
            }
        ]
    }

    resolved = admin.post(
        f"/api/maintenance/return-obligations/{obligation.obligation_id}/resolve-category",
        json={
            "project_id": project.project_id,
            "version": obligation.version,
            "category_id": category.id,
            "idempotency_key": "synthetic-return-category-resolution",
            "reason": "实名管理员关联标准品类",
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["classification"] == "required"
    assert resolved.json()["required_quantity"] == "4.000"
    rate = admin.get(
        f"/api/maintenance/projects/stable/{project.project_id}/return-rate"
    ).json()
    assert rate["status"] == "available"
    assert rate["official_rate_pct"] == "0.00"

    non_admin = _client(
        db,
        username="bad_return_resolve_non_admin",
        role="boss",
        permissions={
            "page_maintenance": True,
            "action_maintenance_bad_return_manage": True,
        },
    )
    assert non_admin.get("/api/maintenance/return-categories").status_code == 403
    denied = non_admin.post(
        f"/api/maintenance/return-obligations/{obligation.obligation_id}/resolve-category",
        json={
            "project_id": project.project_id,
            "version": resolved.json()["version"],
            "category_id": category.id,
            "idempotency_key": "synthetic-return-category-non-admin",
            "reason": "非管理员不能直接判定豁免",
        },
    )
    assert denied.status_code == 403, denied.text


def test_partial_return_lifecycle_is_recoverable_and_never_mutates_cost_or_inventory(db):
    client = _client(db, username="bad_return_lifecycle_admin")
    confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="project-bad-return-lifecycle",
        suffix="lifecycle",
    )
    issue_line = db.get(MaintenanceSiteIssueLine, confirmed["lines"][0]["issue_line_id"])
    inventory = Inventory(
        raw_inventory_id="synthetic-return-inventory",
        part_id=obligation.part_id,
        pn_std=obligation.pn,
        warehouse="合成公司库",
        source_qty="9",
        manual_qty="8",
        is_qty_overridden=True,
    )
    db.add(inventory)
    db.commit()
    cost_before = (
        issue_line.cost_source,
        issue_line.cost_amount_ex_tax,
        issue_line.cost_amount_inc_tax,
        issue_line.version,
    )
    inventory_before = (
        inventory.source_qty,
        inventory.manual_qty,
        inventory.is_qty_overridden,
    )
    create_body = {
        "project_id": obligation.project_id,
        "idempotency_key": "synthetic-return-document-create",
        "lines": [{"obligation_id": obligation.obligation_id, "quantity": "2"}],
        "note": "合成部分返还",
        "reason": "登记部分坏件返还",
    }
    created = client.post("/api/maintenance/bad-returns", json=create_body)
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["return_no"].startswith("BHR-")
    assert draft["status"] == "draft"
    assert draft["inventory_effect"] == draft["cost_effect"] == "none"
    replayed = client.post("/api/maintenance/bad-returns", json=create_body)
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["return_id"] == draft["return_id"]
    assert replayed.json()["idempotent_replay"] is True
    assert client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()["registered_quantity"] == "0.000"

    submitted_body = {
        "project_id": obligation.project_id,
        "version": draft["version"],
        "idempotency_key": "synthetic-return-document-submit",
        "reason": "提交返还登记",
    }
    submitted = client.post(
        f"/api/maintenance/bad-returns/{draft['return_id']}/submit",
        json=submitted_body,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"
    assert client.post(
        f"/api/maintenance/bad-returns/{draft['return_id']}/submit",
        json=submitted_body,
    ).json()["idempotent_replay"] is True
    registered_rate = client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()
    assert registered_rate["registered_quantity"] == "2.000"
    assert registered_rate["registered_rate_pct"] == "40.00"
    assert registered_rate["warehouse_confirmed_quantity"] == "0.000"
    assert registered_rate["official_rate_pct"] == "0.00"

    in_transit = client.post(
        f"/api/maintenance/bad-returns/{draft['return_id']}/in-transit",
        json={
            "project_id": obligation.project_id,
            "version": submitted.json()["version"],
            "idempotency_key": "synthetic-return-document-transit",
            "logistics_reference": "SYNTH-LOGISTICS-001",
            "reason": "登记合成在途引用",
        },
    )
    assert in_transit.status_code == 200, in_transit.text
    assert in_transit.json()["status"] == "in_transit"

    confirmed_return = client.post(
        f"/api/maintenance/bad-returns/{draft['return_id']}/warehouse-confirm",
        json={
            "project_id": obligation.project_id,
            "version": in_transit.json()["version"],
            "idempotency_key": "synthetic-return-document-warehouse",
            "warehouse_reference": "SYNTH-WAREHOUSE-001",
            "inbound_reference": "SYNTH-INBOUND-001",
            "reason": "仓库确认合成返还",
        },
    )
    assert confirmed_return.status_code == 200, confirmed_return.text
    assert confirmed_return.json()["status"] == "warehouse_confirmed"
    assert confirmed_return.json()["inbound_reference"] == "SYNTH-INBOUND-001"
    official = client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()
    assert official["warehouse_confirmed_quantity"] == "2.000"
    assert official["official_basis"] == "warehouse_confirmed_v1"
    assert official["official_rate_pct"] == "40.00"

    project = db.get(MaintenanceProject, obligation.project_id)
    assert project is not None
    # The rate is part of both the project workspace and directory card, not a
    # hidden download-only fact.
    workspace = client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/workspace",
        params={"as_of": "2026-08-09"},
    )
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["return_rate"] == official
    assert workspace.json()["project"]["return_rate"] == official
    directory = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "as_of": "2026-08-09",
            "q": project.project_code,
            "lifecycle": "ongoing",
        },
    )
    assert directory.status_code == 200, directory.text
    assert directory.json()["rows"][0]["return_rate"] == official

    recovered = client.post(
        "/api/maintenance/bad-returns/search",
        json={"project_id": obligation.project_id, "page": 1, "page_size": 20},
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["rows"][0]["return_id"] == draft["return_id"]
    assert recovered.json()["rows"][0]["status"] == "warehouse_confirmed"

    db.expire_all()
    unchanged_line = db.get(MaintenanceSiteIssueLine, issue_line.issue_line_id)
    unchanged_inventory = db.get(Inventory, inventory.id)
    assert (
        unchanged_line.cost_source,
        unchanged_line.cost_amount_ex_tax,
        unchanged_line.cost_amount_inc_tax,
        unchanged_line.version,
    ) == cost_before
    assert (
        unchanged_inventory.source_qty,
        unchanged_inventory.manual_qty,
        unchanged_inventory.is_qty_overridden,
    ) == inventory_before


def test_concurrent_draft_submission_allows_only_one_overlapping_return(db):
    client = _client(db, username="bad_return_concurrency_admin")
    _confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="project-bad-return-concurrency",
        quantity="5",
        suffix="concurrency",
    )

    def create(key: str) -> dict:
        response = client.post(
            "/api/maintenance/bad-returns",
            json={
                "project_id": obligation.project_id,
                "idempotency_key": key,
                "lines": [{"obligation_id": obligation.obligation_id, "quantity": "3"}],
                "reason": "建立并发提交草稿",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    first = create("synthetic-return-concurrent-create-a")
    second = create("synthetic-return-concurrent-create-b")

    def submit(document: dict, key: str):
        return client.post(
            f"/api/maintenance/bad-returns/{document['return_id']}/submit",
            json={
                "project_id": obligation.project_id,
                "version": document["version"],
                "idempotency_key": key,
                "reason": "并发提交只能成功一张",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda item: submit(*item),
                [
                    (first, "synthetic-return-concurrent-submit-a"),
                    (second, "synthetic-return-concurrent-submit-b"),
                ],
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    rate = client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()
    assert rate["registered_quantity"] == "3.000"


def test_bad_return_write_lock_wait_is_bounded_and_retryable(db, monkeypatch):
    client = _client(db, username="bad_return_lock_timeout_admin")
    _confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="project-bad-return-lock-timeout",
        quantity="5",
        suffix="lock-timeout",
    )
    created = client.post(
        "/api/maintenance/bad-returns",
        json={
            "project_id": obligation.project_id,
            "idempotency_key": "synthetic-return-lock-timeout-create",
            "lines": [{"obligation_id": obligation.obligation_id, "quantity": "2"}],
            "reason": "建立有限锁等待测试草稿",
        },
    ).json()
    body = {
        "project_id": obligation.project_id,
        "version": created["version"],
        "idempotency_key": "synthetic-return-lock-timeout-submit",
        "reason": "验证锁等待可重试且不会无限挂起",
    }
    monkeypatch.setattr(bad_return_service, "_WRITE_LOCK_TIMEOUT", "250ms", raising=False)
    blocker = SessionLocal()
    try:
        blocker.scalar(
            select(MaintenanceProject)
            .where(MaintenanceProject.project_id == obligation.project_id)
            .with_for_update()
        )
        started = monotonic()
        blocked = client.post(
            f"/api/maintenance/bad-returns/{created['return_id']}/submit",
            json=body,
        )
        elapsed = monotonic() - started
        assert blocked.status_code == 409, blocked.text
        assert "正在被其他操作处理" in blocked.json()["detail"]
        assert elapsed < 2
    finally:
        blocker.rollback()
        blocker.close()

    retried = client.post(
        f"/api/maintenance/bad-returns/{created['return_id']}/submit",
        json=body,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "submitted"


def test_submitted_return_can_be_voided_and_replaced_without_counting_twice(db):
    client = _client(db, username="bad_return_void_replace_admin")
    _confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="project-bad-return-void-replace",
        quantity="5",
        suffix="void-replace",
    )
    created = client.post(
        "/api/maintenance/bad-returns",
        json={
            "project_id": obligation.project_id,
            "idempotency_key": "synthetic-return-void-create",
            "lines": [{"obligation_id": obligation.obligation_id, "quantity": "2"}],
            "reason": "建立待更正返还单",
        },
    )
    assert created.status_code == 201, created.text
    submitted = client.post(
        f"/api/maintenance/bad-returns/{created.json()['return_id']}/submit",
        json={
            "project_id": obligation.project_id,
            "version": created.json()["version"],
            "idempotency_key": "synthetic-return-void-submit",
            "reason": "提交后发现业务内容有误",
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()["registered_quantity"] == "2.000"

    voided = client.post(
        f"/api/maintenance/bad-returns/{created.json()['return_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": submitted.json()["version"],
            "idempotency_key": "synthetic-return-void-command",
            "reason": "原返还单登记错误，追加式作废后重建",
        },
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "void"
    assert voided.json()["voided_at"] is not None
    replayed_void = client.post(
        f"/api/maintenance/bad-returns/{created.json()['return_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": submitted.json()["version"],
            "idempotency_key": "synthetic-return-void-command",
            "reason": "原返还单登记错误，追加式作废后重建",
        },
    )
    assert replayed_void.status_code == 200, replayed_void.text
    assert replayed_void.json()["idempotent_replay"] is True
    assert client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()["registered_quantity"] == "0.000"

    replacement = client.post(
        "/api/maintenance/bad-returns",
        json={
            "project_id": obligation.project_id,
            "idempotency_key": "synthetic-return-replacement-create",
            "replaces_return_id": created.json()["return_id"],
            "lines": [{"obligation_id": obligation.obligation_id, "quantity": "1"}],
            "reason": "按正确数量建立替代返还单",
        },
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["replaces_return_id"] == created.json()["return_id"]
    assert replacement.json()["status"] == "draft"
    replacement_submit = client.post(
        f"/api/maintenance/bad-returns/{replacement.json()['return_id']}/submit",
        json={
            "project_id": obligation.project_id,
            "version": replacement.json()["version"],
            "idempotency_key": "synthetic-return-replacement-submit",
            "reason": "提交正确替代返还单",
        },
    )
    assert replacement_submit.status_code == 200, replacement_submit.text
    assert client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()["registered_quantity"] == "1.000"

    duplicate_replacement = client.post(
        "/api/maintenance/bad-returns",
        json={
            "project_id": obligation.project_id,
            "idempotency_key": "synthetic-return-replacement-duplicate",
            "replaces_return_id": created.json()["return_id"],
            "lines": [{"obligation_id": obligation.obligation_id, "quantity": "1"}],
            "reason": "同一原单不得存在两个替代单",
        },
    )
    assert duplicate_replacement.status_code == 409, duplicate_replacement.text
    assert db.query(MaintenanceBadReturnCommand).filter_by(
        entity_id=created.json()["return_id"],
        action="void",
    ).count() == 1
    assert db.query(MaintenanceProjectOperationAudit).filter_by(
        entity_type="bad_return",
        entity_id=created.json()["return_id"],
        action="void",
    ).count() == 1


def test_draft_and_in_transit_returns_are_voidable_without_rate_residue(db):
    client = _client(db, username="bad_return_other_void_states_admin")
    _confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="project-return-other-void-states",
        quantity="5",
        suffix="other-void-states",
    )

    def create(*, suffix: str, quantity: str) -> dict:
        response = client.post(
            "/api/maintenance/bad-returns",
            json={
                "project_id": obligation.project_id,
                "idempotency_key": f"synthetic-other-void-create-{suffix}",
                "lines": [
                    {"obligation_id": obligation.obligation_id, "quantity": quantity}
                ],
                "reason": "建立其他状态作废边界测试单",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    draft = create(suffix="draft", quantity="1")
    draft_void = client.post(
        f"/api/maintenance/bad-returns/{draft['return_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": draft["version"],
            "idempotency_key": "synthetic-other-void-draft",
            "reason": "草稿建立错误",
        },
    )
    assert draft_void.status_code == 200, draft_void.text
    assert draft_void.json()["status"] == "void"

    transit_source = create(suffix="transit", quantity="2")
    submitted = client.post(
        f"/api/maintenance/bad-returns/{transit_source['return_id']}/submit",
        json={
            "project_id": obligation.project_id,
            "version": transit_source["version"],
            "idempotency_key": "synthetic-other-void-submit",
            "reason": "提交在途作废边界测试单",
        },
    ).json()
    in_transit = client.post(
        f"/api/maintenance/bad-returns/{transit_source['return_id']}/in-transit",
        json={
            "project_id": obligation.project_id,
            "version": submitted["version"],
            "idempotency_key": "synthetic-other-void-transit",
            "logistics_reference": "SYNTH-VOID-TRANSIT",
            "reason": "登记测试物流事实",
        },
    ).json()
    transit_void = client.post(
        f"/api/maintenance/bad-returns/{transit_source['return_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": in_transit["version"],
            "idempotency_key": "synthetic-other-void-transit-command",
            "reason": "物流登记后发现整单错误",
        },
    )
    assert transit_void.status_code == 200, transit_void.text
    assert transit_void.json()["status"] == "void"
    assert transit_void.json()["logistics_reference"] == "SYNTH-VOID-TRANSIT"
    rate = client.get(
        f"/api/maintenance/projects/stable/{obligation.project_id}/return-rate"
    ).json()
    assert rate["registered_quantity"] == "0.000"
    assert rate["warehouse_confirmed_quantity"] == "0.000"


def test_warehouse_confirmed_return_void_requires_no_formal_inbound_link(db):
    client = _client(db, username="bad_return_warehouse_void_admin")
    _confirmed, obligation = _one_required_obligation(
        db,
        client,
        project_id="project-bad-return-warehouse-void",
        quantity="5",
        suffix="warehouse-void",
    )

    def warehouse_confirm(*, suffix: str, inbound_reference: str | None) -> dict:
        created = client.post(
            "/api/maintenance/bad-returns",
            json={
                "project_id": obligation.project_id,
                "idempotency_key": f"synthetic-warehouse-void-create-{suffix}",
                "lines": [{"obligation_id": obligation.obligation_id, "quantity": "1"}],
                "reason": "建立仓库确认边界测试单",
            },
        ).json()
        submitted = client.post(
            f"/api/maintenance/bad-returns/{created['return_id']}/submit",
            json={
                "project_id": obligation.project_id,
                "version": created["version"],
                "idempotency_key": f"synthetic-warehouse-void-submit-{suffix}",
                "reason": "提交仓库确认边界测试单",
            },
        ).json()
        body = {
            "project_id": obligation.project_id,
            "version": submitted["version"],
            "idempotency_key": f"synthetic-warehouse-void-confirm-{suffix}",
            "warehouse_reference": f"SYNTH-WH-{suffix}",
            "reason": "确认仓库收件边界",
        }
        if inbound_reference is not None:
            body["inbound_reference"] = inbound_reference
        response = client.post(
            f"/api/maintenance/bad-returns/{created['return_id']}/warehouse-confirm",
            json=body,
        )
        assert response.status_code == 200, response.text
        return response.json()

    reversible = warehouse_confirm(suffix="NO-INBOUND", inbound_reference=None)
    voided = client.post(
        f"/api/maintenance/bad-returns/{reversible['return_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": reversible["version"],
            "idempotency_key": "synthetic-warehouse-void-allowed",
            "reason": "尚未关联正式入库，撤销错误仓库确认",
        },
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "void"

    protected = warehouse_confirm(
        suffix="WITH-INBOUND",
        inbound_reference="SYNTH-INBOUND-PROTECTED",
    )
    blocked = client.post(
        f"/api/maintenance/bad-returns/{protected['return_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": protected["version"],
            "idempotency_key": "synthetic-warehouse-void-blocked",
            "reason": "正式入库关联后不得直接撤销",
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert "正式入库" in blocked.json()["detail"]


def test_return_commands_and_audits_are_append_only_and_permission_hidden(db):
    admin = _client(db, username="bad_return_append_admin")
    _confirmed, obligation = _one_required_obligation(
        db,
        admin,
        project_id="project-bad-return-append",
        suffix="append",
    )
    denied = _client(
        db,
        username="bad_return_action_denied",
        role="boss",
        permissions={
            "page_maintenance": True,
            "action_maintenance_bad_return_manage": False,
        },
    ).post(
        "/api/maintenance/bad-returns",
        json={
            "project_id": obligation.project_id,
            "idempotency_key": "synthetic-return-permission-denied",
            "lines": [{"obligation_id": obligation.obligation_id, "quantity": "1"}],
            "reason": "无权限不得登记",
        },
    )
    assert denied.status_code == 403, denied.text
    assert db.query(MaintenanceBadReturnCommand).count() == 0

    created = admin.post(
        "/api/maintenance/bad-returns",
        json={
            "project_id": obligation.project_id,
            "idempotency_key": "synthetic-return-append-create",
            "lines": [{"obligation_id": obligation.obligation_id, "quantity": "1"}],
            "reason": "建立追加式审计事实",
        },
    )
    assert created.status_code == 201, created.text
    assert db.query(MaintenanceBadReturnCommand).count() == 1
    assert db.query(MaintenanceProjectOperationAudit).filter_by(
        entity_type="bad_return",
        entity_id=created.json()["return_id"],
        action="create",
    ).count() == 1
    command = db.query(MaintenanceBadReturnCommand).one()
    audit = db.query(MaintenanceProjectOperationAudit).filter_by(
        entity_type="bad_return",
        entity_id=created.json()["return_id"],
        action="create",
    ).one()
    with pytest.raises(DBAPIError, match="append-only"):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE maintenance_bad_return_command "
                    "SET response_json = CAST(:payload AS jsonb) "
                    "WHERE command_id = :command_id"
                ),
                {"payload": '{"tampered": true}', "command_id": command.command_id},
            )
    with pytest.raises(DBAPIError, match="append-only"):
        with db.begin_nested():
            db.execute(
                text(
                    "DELETE FROM maintenance_project_operation_audit WHERE id = :audit_id"
                ),
                {"audit_id": audit.id},
            )

    voided = admin.post(
        f"/api/maintenance/bad-returns/{created.json()['return_id']}/void",
        json={
            "project_id": obligation.project_id,
            "version": created.json()["version"],
            "idempotency_key": "synthetic-return-append-void",
            "reason": "验证新增作废命令同样追加式留痕",
        },
    )
    assert voided.status_code == 200, voided.text
    void_command = db.query(MaintenanceBadReturnCommand).filter_by(
        action="void"
    ).one()
    assert db.query(MaintenanceProjectOperationAudit).filter_by(
        entity_type="bad_return",
        entity_id=created.json()["return_id"],
        action="void",
    ).count() == 1
    with pytest.raises(DBAPIError, match="append-only"):
        with db.begin_nested():
            db.execute(
                text(
                    "DELETE FROM maintenance_bad_return_command "
                    "WHERE command_id = :command_id"
                ),
                {"command_id": void_command.command_id},
            )


def test_all_hard_drive_obligations_report_no_return_required_not_one_hundred(db):
    project = _project(db, project_id="project-bad-return-all-disk")
    client = _client(db, username="bad_return_all_disk_admin")
    category = _category(db, major="硬盘", minor="NVMe-SSD")
    delivery = _delivery_source(
        db,
        project=project,
        delivery_line_id="synthetic-return-all-disk",
        quantity="2",
    )
    db.get(DimPart, delivery.part_id).category_id = category.id
    db.commit()
    _confirm_issue(
        client,
        project_id=project.project_id,
        lines=[{"delivery_line_id": delivery.delivery_line_id, "quantity": "2"}],
        suffix="all-disk",
    )
    rate = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/return-rate"
    ).json()
    assert rate["status"] == "no_return_required"
    assert rate["required_quantity"] == "0.000"
    assert rate["exempt_quantity"] == "2.000"
    assert rate["official_rate_pct"] is None
