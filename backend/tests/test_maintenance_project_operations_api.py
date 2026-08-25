"""Stable maintenance-project operating facts through their public API."""

import uuid
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

from app import auth
from app.auth import hash_password
from app.api import maintenance_project_operations
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysAccessLog, SysImportBatch, SysUser
from app.security import UserContext
from app.services import maintenance_consumption_cost as cost_service
from app.services import maintenance_project_operations as operations_service


def _client(db, *, username: str = "maintenance_facts_admin") -> TestClient:
    db.add(
        SysUser(
            username=username,
            role="admin",
            display_name="合成维保管理员",
            password_hash=hash_password("synthetic-password-123"),
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
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
            display_name="合成维保权限账号",
            password_hash=hash_password("synthetic-password-123"),
            permissions=permissions,
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _project(db, *, project_id: str = "project-synthetic-facts") -> MaintenanceProject:
    project = MaintenanceProject(
        project_id=project_id,
        project_code=f"FACTS-{project_id}",
        display_name="合成经营事实项目",
        lifecycle_status="missing",
    )
    db.add(project)
    db.commit()
    return project


def _count_endpoint_queries(db, client: TestClient, *, params: dict) -> tuple[dict, int]:
    engine = db.get_bind()
    query_count = 0

    def count_query(*_args) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        response = client.get(
            "/api/maintenance/projects/stable/operations",
            params=params,
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert response.status_code == 200, response.text
    return response.json(), query_count


def _count_get_queries(
    db,
    client: TestClient,
    *,
    path: str,
    params: dict,
) -> tuple[dict, int]:
    engine = db.get_bind()
    query_count = 0

    def count_query(*_args) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        response = client.get(path, params=params)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert response.status_code == 200, response.text
    return response.json(), query_count


def _count_write_queries(
    db,
    client: TestClient,
    *,
    method: str,
    path: str,
    payload: dict,
) -> tuple[object, int]:
    engine = db.get_bind()
    query_count = 0

    def count_query(*_args) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        response = client.request(method, path, json=payload)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    return response, query_count


def _batch(db, suffix: str) -> SysImportBatch:
    batch = SysImportBatch(
        filename=f"synthetic-{suffix}.xlsx",
        file_type="purchase",
        file_hash=f"synthetic-{suffix}",
        status="success",
    )
    db.add(batch)
    db.flush()
    return batch


def _site_issue_lines(part: DimPart, *, count: int, prefix: str) -> list[dict]:
    return [
        {
            "issue_line_id": f"{prefix}-{line_no}",
            "line_no": line_no,
            "part_id": part.id,
            "pn": part.pn_std,
            "quantity": "1",
        }
        for line_no in range(1, count + 1)
    ]


def _create_legacy_site_issue_fixture(
    db,
    *,
    project_id: str,
    body: dict,
    commit: bool = True,
) -> dict:
    """Seed one historical/import-style fact without reopening the public v1 API."""

    values = dict(body)
    raw_issue_date = values.pop("issue_date")
    payload = operations_service.create_site_issue(
        db,
        project_id=project_id,
        issue_date=(
            date.fromisoformat(raw_issue_date)
            if isinstance(raw_issue_date, str)
            else raw_issue_date
        ),
        operated_by="legacy-service-test-fixture",
        source="direct_api",
        import_batch_id=None,
        **values,
    )
    assert payload is not None
    if commit:
        db.commit()
    return payload


def _count_service_write_queries(db, action) -> tuple[dict, int]:
    engine = db.get_bind()
    query_count = 0

    def count_query(*_args) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        payload = action()
        db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    return payload, query_count


def test_public_site_issue_create_preserves_legacy_contract_and_line_limit(db):
    project = _project(db, project_id="project-site-issue-api-limit")
    client = _client(db, username="site_issue_api_limit_admin")
    part = DimPart(pn_std="PN-SITE-ISSUE-API-LIMIT")
    db.add(part)
    db.commit()

    old_public_payload = {
        "issue_no": "ISSUE-API-LIMIT-200",
        "issue_date": "2026-08-01",
        "raw_status": "synthetic-confirmed",
        "status_mapping_state": "mapped",
        "normalized_status": "confirmed",
        "status_mapping_version": "synthetic-map-v1",
        "lines": _site_issue_lines(part, count=200, prefix="issue-api-accepted"),
        "reason": "验证旧客户端契约继续可用",
    }
    accepted_public = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/site-issues",
        json=old_public_payload,
    )
    assert accepted_public.status_code == 201, accepted_public.text
    assert len(accepted_public.json()["lines"]) == 200
    assert db.get(MaintenanceSiteIssueLine, "issue-api-accepted-1") is not None

    with pytest.raises(
        operations_service.MaintenanceOperationError,
        match="最多允许 200 条明细",
    ):
        _create_legacy_site_issue_fixture(
            db,
            project_id=project.project_id,
            body={
                "issue_no": "ISSUE-API-LIMIT-201",
                "issue_date": "2026-08-01",
                "raw_status": "synthetic-confirmed",
                "status_mapping_state": "mapped",
                "normalized_status": "confirmed",
                "status_mapping_version": "synthetic-map-v1",
                "lines": _site_issue_lines(part, count=201, prefix="issue-api-limit"),
                "reason": "验证历史服务明细上限",
            },
        )
    db.rollback()
    assert db.get(MaintenanceSiteIssueLine, "issue-api-limit-1") is None


def test_site_issue_create_service_rejects_more_than_200_lines(db):
    project = _project(db, project_id="project-site-issue-service-limit")
    part = DimPart(pn_std="PN-SITE-ISSUE-SERVICE-LIMIT")
    db.add(part)
    db.commit()

    with pytest.raises(
        operations_service.MaintenanceOperationError,
        match="最多允许 200 条明细",
    ):
        operations_service.create_site_issue(
            db,
            project_id=project.project_id,
            issue_no="ISSUE-SERVICE-LIMIT-201",
            issue_date=date(2026, 8, 1),
            raw_status="synthetic-confirmed",
            status_mapping_state="mapped",
            normalized_status="confirmed",
            status_mapping_version="synthetic-map-v1",
            lines=_site_issue_lines(
                part,
                count=201,
                prefix="issue-service-limit",
            ),
            reason="验证服务层现场领用单明细上限",
            operated_by="service-limit-test",
        )


def test_legacy_service_site_issue_exposes_history_provenance(db):
    project = _project(db, project_id="project-direct-site-issue-provenance")
    client = _client(db, username="direct_site_issue_provenance_admin")
    part = DimPart(pn_std="PN-DIRECT-SITE-ISSUE-PROVENANCE")
    db.add(part)
    db.commit()

    created = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-DIRECT-PROVENANCE",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-direct-provenance",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                }
            ],
            "reason": "通过历史兼容服务建立现场领用",
        },
    )

    assert created["source"] == "direct_api"
    assert created["import_batch_id"] is None
    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-08-31"},
    )
    assert workspace.status_code == 200, workspace.text
    requisition = workspace.json()["requisitions"]["rows"][0]
    assert requisition["source"] == "direct_api"
    assert requisition["import_batch_id"] is None

    derived_write = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/site-issues",
        json={
            "issue_no": "ISSUE-DERIVED-COST-WRITE",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [{
                "issue_line_id": "issue-line-derived-cost-write",
                "line_no": 1,
                "part_id": part.id,
                "pn": part.pn_std,
                "quantity": "1",
                "unit_cost_inc_tax": "999.00",
            }],
            "reason": "验证客户端不能写服务器派生成本",
        },
    )
    assert derived_write.status_code == 422, derived_write.text
    assert db.get(MaintenanceSiteIssueLine, "issue-line-derived-cost-write") is None


def test_create_rejects_mapped_unknown_status_pairs_without_writes(db):
    project = _project(db, project_id="project-mapped-unknown-rejected")
    client = _client(db, username="mapped_unknown_rejected_admin")
    part = DimPart(pn_std="PN-MAPPED-UNKNOWN-REJECTED")
    db.add(part)
    db.commit()

    site_body = {
        "issue_no": "ISSUE-MAPPED-UNKNOWN-REJECTED",
        "issue_date": "2026-08-01",
        "raw_status": "synthetic-unknown",
        "status_mapping_state": "mapped",
        "normalized_status": "unknown",
        "status_mapping_version": "synthetic-map-v1",
        "lines": [{
            "issue_line_id": "issue-line-mapped-unknown-rejected",
            "line_no": 1,
            "part_id": part.id,
            "pn": part.pn_std,
            "quantity": "1",
        }],
        "reason": "验证 mapped 不能伪装成 unknown",
    }
    site = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/site-issues",
        json=site_body,
    )
    assert site.status_code == 400, site.text
    assert "mapped" in site.json()["detail"]
    assert db.scalar(
        select(MaintenanceSiteIssue).where(
            MaintenanceSiteIssue.project_id == project.project_id
        )
    ) is None

    expense_body = {
        "expense_id": "expense-mapped-unknown-rejected",
        "expense_ref": "BX-MAPPED-UNKNOWN-REJECTED",
        "expense_date": "2026-08-01",
        "amount_ex_tax": "10.00",
        "raw_status": "synthetic-unknown",
        "status_mapping_state": "mapped",
        "normalized_status": "unknown",
        "status_mapping_version": "synthetic-map-v1",
        "reason": "验证 mapped 报销不能伪装成 unknown",
    }
    expense = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json=expense_body,
    )
    assert expense.status_code == 400, expense.text
    assert "mapped" in expense.json()["detail"]
    assert db.get(
        MaintenanceProjectExpenseAttribution,
        "expense-mapped-unknown-rejected",
    ) is None

    with pytest.raises(
        operations_service.MaintenanceOperationError,
        match="mapped.*unknown",
    ):
        operations_service.create_site_issue(
            db,
            project_id=project.project_id,
            issue_no="ISSUE-MAPPED-UNKNOWN-SERVICE",
            issue_date=date(2026, 8, 1),
            raw_status="synthetic-unknown",
            status_mapping_state="mapped",
            normalized_status="unknown",
            status_mapping_version="synthetic-map-v1",
            lines=[{
                "issue_line_id": "issue-line-mapped-unknown-service",
                "line_no": 1,
                "part_id": part.id,
                "pn": part.pn_std,
                "quantity": "1",
            }],
            reason="服务层拒绝非法映射",
            operated_by="mapped-unknown-test",
        )
    db.rollback()
    with pytest.raises(
        operations_service.MaintenanceOperationError,
        match="mapped.*unknown",
    ):
        operations_service.create_expense(
            db,
            project_id=project.project_id,
            expense_id="expense-mapped-unknown-service",
            project_contract_id=None,
            expense_ref="BX-MAPPED-UNKNOWN-SERVICE",
            expense_date=date(2026, 8, 1),
            applicant=None,
            category=None,
            expense_reason=None,
            amount_ex_tax=Decimal("10.00"),
            raw_status="synthetic-unknown",
            status_mapping_state="mapped",
            normalized_status="unknown",
            status_mapping_version="synthetic-map-v1",
            reason="服务层拒绝非法映射",
            operated_by="mapped-unknown-test",
        )
    db.rollback()


def test_site_issue_create_query_count_is_bounded_and_all_lines_are_costed(db):
    project = _project(db, project_id="project-site-issue-create-scale")
    part = DimPart(pn_std="PN-SITE-ISSUE-CREATE-SCALE")
    db.add(part)
    db.flush()
    batch = _batch(db, "site-issue-create-scale")
    order = FPurchaseOrder(
        raw_order_id="PO-H-SITE-ISSUE-CREATE-SCALE",
        order_no="PO-SITE-ISSUE-CREATE-SCALE",
        order_date=date(2026, 8, 1),
        data_status="已生效",
        is_tax_inclusive=False,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    purchase_line = FPurchaseLine(
        raw_line_id="PO-L-SITE-ISSUE-CREATE-SCALE",
        order_id=order.id,
        part_id=part.id,
        pn_std=part.pn_std,
        qty=100,
        unit_price=25,
        import_batch_id=batch.id,
    )
    db.add(purchase_line)
    db.commit()

    def create_payload(*, suffix: str, count: int) -> dict:
        lines = _site_issue_lines(part, count=count, prefix=f"issue-scale-{suffix}")
        for line in lines:
            line["quantity"] = "2"
            line["linked_purchase_line_id"] = purchase_line.id
        return {
            "issue_no": f"ISSUE-CREATE-SCALE-{suffix}",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": lines,
            "reason": "验证现场领用创建批量取价",
        }

    one_payload, one_queries = _count_service_write_queries(
        db,
        lambda: _create_legacy_site_issue_fixture(
            db,
            project_id=project.project_id,
            body=create_payload(suffix="ONE", count=1),
            commit=False,
        ),
    )
    many_payload, many_queries = _count_service_write_queries(
        db,
        lambda: _create_legacy_site_issue_fixture(
            db,
            project_id=project.project_id,
            body=create_payload(suffix="MANY", count=40),
            commit=False,
        ),
    )

    assert many_queries <= one_queries + 8, (one_queries, many_queries)
    assert len(one_payload["lines"]) == 1
    assert len(many_payload["lines"]) == 40
    assert {
        (line["cost_source"], line["unit_cost"], line["cost_amount"])
        for line in many_payload["lines"]
    } == {("direct_purchase", "25.00", "50.00")}


def test_site_issue_confirm_query_count_is_bounded_and_versions_remain_audited(db):
    project = _project(db, project_id="project-site-issue-confirm-scale")
    client = _client(db, username="site_issue_confirm_scale_admin")
    part = DimPart(pn_std="PN-SITE-ISSUE-CONFIRM-SCALE")
    db.add(part)
    db.flush()
    batch = _batch(db, "site-issue-confirm-scale")
    order = FPurchaseOrder(
        raw_order_id="PO-H-SITE-ISSUE-CONFIRM-SCALE",
        order_no="PO-SITE-ISSUE-CONFIRM-SCALE",
        order_date=date(2026, 8, 1),
        data_status="已生效",
        is_tax_inclusive=False,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    purchase_line = FPurchaseLine(
        raw_line_id="PO-L-SITE-ISSUE-CONFIRM-SCALE",
        order_id=order.id,
        part_id=part.id,
        pn_std=part.pn_std,
        qty=100,
        unit_price=25,
        import_batch_id=batch.id,
    )
    db.add(purchase_line)
    db.commit()

    def create_unknown(*, suffix: str, count: int) -> dict:
        lines = _site_issue_lines(part, count=count, prefix=f"issue-confirm-{suffix}")
        for line in lines:
            line["quantity"] = "2"
            line["linked_purchase_line_id"] = purchase_line.id
        return _create_legacy_site_issue_fixture(
            db,
            project_id=project.project_id,
            body={
                "issue_no": f"ISSUE-CONFIRM-SCALE-{suffix}",
                "issue_date": "2026-08-01",
                "raw_status": "synthetic-pending",
                "status_mapping_state": "unmapped",
                "normalized_status": "unknown",
                "status_mapping_version": "synthetic-map-v1",
                "lines": lines,
                "reason": "建立待确认批量现场领用",
            },
        )

    one_issue = create_unknown(suffix="ONE", count=1)
    many_issue = create_unknown(suffix="MANY", count=40)

    def confirm(issue_id: str) -> tuple[object, int]:
        return _count_write_queries(
            db,
            client,
            method="PATCH",
            path=f"/api/maintenance/projects/stable/site-issues/{issue_id}/status",
            payload={
                "version": 1,
                "raw_status": "synthetic-confirmed",
                "normalized_status": "confirmed",
                "status_mapping_version": "synthetic-map-v2",
                "reason": "确认批量现场领用",
            },
        )

    one_response, one_queries = confirm(one_issue["issue_id"])
    many_response, many_queries = confirm(many_issue["issue_id"])

    assert one_response.status_code == 200, one_response.text
    assert many_response.status_code == 200, many_response.text
    assert many_queries <= one_queries + 8, (one_queries, many_queries)
    assert many_response.json()["version"] == 2
    assert len(many_response.json()["lines"]) == 40
    assert {
        (
            line["version"],
            line["cost_source"],
            line["unit_cost"],
            line["cost_amount"],
        )
        for line in many_response.json()["lines"]
    } == {(2, "direct_purchase", "25.00", "50.00")}

    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    assert state.revision == 4
    status_audits = list(
        db.scalars(
            select(MaintenanceProjectOperationAudit).where(
                MaintenanceProjectOperationAudit.project_id == project.project_id,
                MaintenanceProjectOperationAudit.entity_type == "site_issue",
                MaintenanceProjectOperationAudit.action == "status_update",
            )
        )
    )
    assert len(status_audits) == 2
    many_audit = next(
        audit for audit in status_audits if audit.entity_id == many_issue["issue_id"]
    )
    assert many_audit.before_json["normalized_status"] == "unknown"
    assert many_audit.after_json["normalized_status"] == "confirmed"
    assert len(many_audit.after_json["lines"]) == 40


def test_site_issue_batch_confirm_cost_failure_is_atomic(db):
    project = _project(db, project_id="project-site-issue-confirm-atomic")
    client = _client(db, username="site_issue_confirm_atomic_admin")
    valid_part = DimPart(pn_std="PN-SITE-ISSUE-CONFIRM-ATOMIC-VALID")
    overflow_part = DimPart(pn_std="PN-SITE-ISSUE-CONFIRM-ATOMIC-OVERFLOW")
    db.add_all([valid_part, overflow_part])
    db.flush()
    batch = _batch(db, "site-issue-confirm-atomic")
    order = FPurchaseOrder(
        raw_order_id="PO-H-SITE-ISSUE-CONFIRM-ATOMIC",
        order_no="PO-SITE-ISSUE-CONFIRM-ATOMIC",
        order_date=date(2026, 8, 1),
        data_status="已生效",
        is_tax_inclusive=False,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    valid_purchase = FPurchaseLine(
        raw_line_id="PO-L-SITE-ISSUE-CONFIRM-ATOMIC-VALID",
        order_id=order.id,
        part_id=valid_part.id,
        pn_std=valid_part.pn_std,
        qty=1,
        unit_price=10,
        import_batch_id=batch.id,
    )
    overflow_purchase = FPurchaseLine(
        raw_line_id="PO-L-SITE-ISSUE-CONFIRM-ATOMIC-OVERFLOW",
        order_id=order.id,
        part_id=overflow_part.id,
        pn_std=overflow_part.pn_std,
        qty=1,
        unit_price=Decimal("500000000000.00"),
        import_batch_id=batch.id,
    )
    db.add_all([valid_purchase, overflow_purchase])
    db.commit()

    created = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-CONFIRM-ATOMIC",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-pending",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-confirm-atomic-valid",
                    "line_no": 1,
                    "part_id": valid_part.id,
                    "pn": valid_part.pn_std,
                    "quantity": "1",
                    "linked_purchase_line_id": valid_purchase.id,
                },
                {
                    "issue_line_id": "issue-confirm-atomic-overflow",
                    "line_no": 2,
                    "part_id": overflow_part.id,
                    "pn": overflow_part.pn_std,
                    "quantity": "2",
                    "linked_purchase_line_id": overflow_purchase.id,
                },
            ],
            "reason": "建立批量确认失败原子性测试领用单",
        },
    )
    issue_id = created["issue_id"]

    failed = client.patch(
        f"/api/maintenance/projects/stable/site-issues/{issue_id}/status",
        json={
            "version": 1,
            "raw_status": "synthetic-confirmed",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v2",
            "reason": "验证任一行取价失败时整单回滚",
        },
    )
    assert failed.status_code == 400, failed.text
    assert "成本金额" in failed.json()["detail"]

    db.expire_all()
    issue = db.get(MaintenanceSiteIssue, issue_id)
    assert issue.normalized_status == "unknown"
    assert issue.status_mapping_state == "unmapped"
    assert issue.version == 1
    for line_id in (
        "issue-confirm-atomic-valid",
        "issue-confirm-atomic-overflow",
    ):
        line = db.get(MaintenanceSiteIssueLine, line_id)
        assert line.cost_source is None
        assert line.cost_amount is None
        assert line.version == 1
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    assert state.revision == 1
    assert (
        db.query(MaintenanceProjectOperationAudit)
        .filter_by(
            project_id=project.project_id,
            entity_type="site_issue",
            entity_id=issue_id,
            action="status_update",
        )
        .count()
        == 0
    )


def test_numeric_normalizers_reject_non_finite_values_as_business_errors():
    for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(operations_service.MaintenanceOperationError):
            operations_service._quantity(value)
        with pytest.raises(cost_service.CostResolutionError):
            cost_service._amount(value)
        assert cost_service._valid(value, Decimal("1")) is False
        assert cost_service._valid(Decimal("1"), value) is False


def test_money_write_paths_use_half_up_and_reject_rounded_overflow(db):
    project = _project(db, project_id="project-money-normalization")
    client = _client(db, username="money_normalization_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-money-normalization",
            "contract_no": "XS-MONEY-NORMALIZATION",
            "contract_amount": "1.005",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "验证合同金额统一四舍五入",
        },
    )
    assert contract.status_code == 201, contract.text
    assert contract.json()["contract_amount"] == "1.01"

    collection = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": contract.json()["project_contract_id"],
            "report_month": "2026-01-01",
            "cumulative_amount": "1.005",
            "status": "confirmed",
            "reason": "验证回款金额统一四舍五入",
        },
    )
    assert collection.status_code == 201, collection.text
    assert collection.json()["cumulative_amount"] == "1.01"

    expense = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-money-normalization",
            "project_contract_id": contract.json()["project_contract_id"],
            "expense_ref": "BX-MONEY-NORMALIZATION",
            "expense_date": "2026-01-01",
            "amount_ex_tax": "1.005",
            "raw_status": "synthetic-finished",
            "status_mapping_state": "mapped",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "验证报销金额统一四舍五入",
        },
    )
    assert expense.status_code == 201, expense.text
    assert expense.json()["amount_ex_tax"] == "1.01"

    part = DimPart(pn_std="PN-MONEY-NORMALIZATION")
    db.add(part)
    db.commit()
    _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-MONEY-NORMALIZATION",
            "issue_date": "2026-01-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [{
                "issue_line_id": "issue-line-money-normalization",
                "line_no": 1,
                "part_id": part.id,
                "pn": part.pn_std,
                "quantity": "1",
            }],
            "reason": "建立人工成本金额归一化领用",
        },
    )
    manual = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-money-normalization",
            "version": 1,
            "unit_cost_ex_tax": "1.005",
            "evidence": "金额归一化证据",
            "reason": "验证人工成本统一四舍五入",
        },
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["unit_cost"] == "1.01"
    assert manual.json()["cost_amount"] == "1.01"

    rounded_overflow = "999999999999.999"
    overflow_contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-money-overflow",
            "contract_no": "XS-MONEY-OVERFLOW",
            "contract_amount": rounded_overflow,
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "验证舍入后合同金额溢出受控拒绝",
        },
    )
    assert overflow_contract.status_code == 400, overflow_contract.text

    overflow_collection = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": contract.json()["project_contract_id"],
            "report_month": "2026-02-01",
            "cumulative_amount": rounded_overflow,
            "status": "unconfirmed",
            "reason": "验证舍入后回款金额溢出受控拒绝",
        },
    )
    assert overflow_collection.status_code == 400, overflow_collection.text

    overflow_expense = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-money-overflow",
            "expense_ref": "BX-MONEY-OVERFLOW",
            "expense_date": "2026-01-01",
            "amount_ex_tax": rounded_overflow,
            "raw_status": "synthetic-finished",
            "status_mapping_state": "mapped",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "验证舍入后报销金额溢出受控拒绝",
        },
    )
    assert overflow_expense.status_code == 400, overflow_expense.text

    derived_overflow_expense = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-inc-tax-overflow",
            "expense_ref": "BX-INC-TAX-OVERFLOW",
            "expense_date": "2026-01-01",
            "amount_ex_tax": "884955752212.39",
            "raw_status": "synthetic-finished",
            "status_mapping_state": "mapped",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "验证服务器派生含税报销金额溢出受控拒绝",
        },
    )
    assert derived_overflow_expense.status_code == 400, derived_overflow_expense.text
    assert "含税" in derived_overflow_expense.json()["detail"]
    assert db.get(
        MaintenanceProjectExpenseAttribution,
        "expense-inc-tax-overflow",
    ) is None

    overflow_manual = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-money-normalization",
            "version": 2,
            "unit_cost_ex_tax": rounded_overflow,
            "evidence": "舍入后溢出证据",
            "reason": "验证舍入后人工成本溢出受控拒绝",
        },
    )
    assert overflow_manual.status_code == 400, overflow_manual.text


def test_site_issue_quantity_uses_numeric_14_3_boundary_with_controlled_rejection(db):
    project = _project(db, project_id="project-quantity-boundary")
    part = DimPart(pn_std="PN-QUANTITY-BOUNDARY")
    db.add(part)
    db.commit()

    maximum = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-QUANTITY-MAXIMUM",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-quantity-maximum",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "99999999999.999",
                }
            ],
            "reason": "验证 Numeric(14,3) 最大合法数量",
        },
    )
    assert maximum["lines"][0]["quantity"] == "99999999999.999"

    with pytest.raises(
        operations_service.MaintenanceOperationError,
        match="领用数量超出允许范围",
    ):
        _create_legacy_site_issue_fixture(
            db,
            project_id=project.project_id,
            body={
                "issue_no": "ISSUE-QUANTITY-FIRST-ILLEGAL",
                "issue_date": "2026-08-01",
                "raw_status": "synthetic-confirmed",
                "status_mapping_state": "mapped",
                "normalized_status": "confirmed",
                "status_mapping_version": "synthetic-map-v1",
                "lines": [
                    {
                        "issue_line_id": "issue-line-quantity-first-illegal",
                        "line_no": 1,
                        "part_id": part.id,
                        "pn": part.pn_std,
                        "quantity": "100000000000",
                    }
                ],
                "reason": "验证 Numeric(14,3) 首个非法数量受控拒绝",
            },
        )
    db.rollback()
    assert db.get(MaintenanceSiteIssueLine, "issue-line-quantity-first-illegal") is None

    with pytest.raises(operations_service.MaintenanceOperationError):
        _create_legacy_site_issue_fixture(
            db,
            project_id=project.project_id,
            body={
                "issue_no": "ISSUE-QUANTITY-NON-FINITE",
                "issue_date": "2026-08-01",
                "raw_status": "synthetic-confirmed",
                "status_mapping_state": "mapped",
                "normalized_status": "confirmed",
                "status_mapping_version": "synthetic-map-v1",
                "lines": [{
                    "issue_line_id": "issue-line-quantity-non-finite",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "NaN",
                }],
                "reason": "验证非有限数量受控拒绝",
            },
        )
    db.rollback()


def test_manual_cost_amount_uses_numeric_14_2_boundary_with_controlled_rejection(db):
    project = _project(db, project_id="project-cost-amount-boundary")
    client = _client(db, username="cost_amount_boundary_admin")
    part = DimPart(pn_std="PN-COST-AMOUNT-BOUNDARY")
    db.add(part)
    db.commit()
    _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-COST-AMOUNT-BOUNDARY",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-cost-maximum",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                },
                {
                    "issue_line_id": "issue-line-cost-first-illegal",
                    "line_no": 2,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "2",
                },
                {
                    "issue_line_id": "issue-line-cost-inc-tax-overflow",
                    "line_no": 3,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                },
            ],
            "reason": "建立成本金额 Numeric(14,2) 边界领用",
        },
    )

    maximum = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-cost-maximum",
            "version": 1,
            "unit_cost_ex_tax": "884955752212.38",
            "evidence": "最大合法金额边界证据",
            "reason": "验证 Numeric(14,2) 最大合法成本金额",
        },
    )
    assert maximum.status_code == 200, maximum.text
    assert maximum.json()["unit_cost"] == "884955752212.38"
    assert maximum.json()["unit_cost_inc_tax"] == "999999999999.99"
    assert maximum.json()["cost_amount"] == "884955752212.38"
    assert maximum.json()["cost_amount_inc_tax"] == "999999999999.99"

    inc_tax_overflow = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-cost-inc-tax-overflow",
            "version": 1,
            "unit_cost_ex_tax": "884955752212.39",
            "evidence": "含税派生金额溢出边界证据",
            "reason": "验证服务器派生含税成本溢出受控拒绝",
        },
    )
    assert inc_tax_overflow.status_code == 400, inc_tax_overflow.text
    assert "含税" in inc_tax_overflow.json()["detail"]
    db.expire_all()
    inc_tax_rejected = db.get(
        MaintenanceSiteIssueLine,
        "issue-line-cost-inc-tax-overflow",
    )
    assert inc_tax_rejected.unit_cost_ex_tax is None
    assert inc_tax_rejected.unit_cost_inc_tax is None
    assert inc_tax_rejected.cost_amount_ex_tax is None
    assert inc_tax_rejected.cost_amount_inc_tax is None
    assert inc_tax_rejected.version == 1

    first_illegal = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-cost-first-illegal",
            "version": 1,
            "unit_cost_ex_tax": "500000000000.00",
            "evidence": "首个非法金额边界证据",
            "reason": "验证 Numeric(14,2) 首个非法成本金额受控拒绝",
        },
    )
    assert first_illegal.status_code == 400, first_illegal.text
    assert "成本金额" in first_illegal.json()["detail"]
    db.expire_all()
    rejected = db.get(MaintenanceSiteIssueLine, "issue-line-cost-first-illegal")
    assert rejected.unit_cost is None
    assert rejected.cost_amount is None
    assert rejected.version == 1

    with pytest.raises(
        operations_service.MaintenanceOperationError,
        match="人工未税单价超出允许范围",
    ):
        operations_service.fill_manual_cost(
            db,
            project_id=project.project_id,
            issue_line_id="issue-line-cost-first-illegal",
            version=1,
            manual_unit_cost=Decimal("NaN"),
            evidence="非有限金额证据",
            reason="验证非有限成本受控拒绝",
            operated_by="cost-boundary-test",
        )
    db.rollback()
    non_finite_line = db.get(
        MaintenanceSiteIssueLine,
        "issue-line-cost-first-illegal",
    )
    non_finite_line.manual_unit_cost = Decimal("NaN")
    with pytest.raises(
        cost_service.CostResolutionError,
        match="成本单价超出允许范围",
    ):
        cost_service.resolve_line(
            db,
            issue_date=date(2026, 8, 1),
            line=non_finite_line,
        )
    db.rollback()


def test_cost_amount_overflow_is_controlled_on_every_resolution_entrypoint(db):
    client = _client(db, username="cost_overflow_entrypoints_admin")

    def add_overflow_purchase(part: DimPart, suffix: str) -> FPurchaseLine:
        batch = _batch(db, f"cost-overflow-{suffix}")
        order = FPurchaseOrder(
            raw_order_id=f"PO-H-COST-OVERFLOW-{suffix}",
            order_no=f"PO-COST-OVERFLOW-{suffix}",
            order_date=date(2026, 8, 1),
            data_status="已生效",
            is_tax_inclusive=False,
            import_batch_id=batch.id,
        )
        db.add(order)
        db.flush()
        line = FPurchaseLine(
            raw_line_id=f"PO-L-COST-OVERFLOW-{suffix}",
            order_id=order.id,
            part_id=part.id,
            pn_std=part.pn_std,
            qty=1,
            unit_price=Decimal("500000000000.00"),
            import_batch_id=batch.id,
        )
        db.add(line)
        db.commit()
        return line

    create_project = _project(db, project_id="project-cost-overflow-create")
    create_part = DimPart(pn_std="PN-COST-OVERFLOW-CREATE")
    db.add(create_part)
    db.commit()
    direct = add_overflow_purchase(create_part, "CREATE")
    with pytest.raises(
        operations_service.MaintenanceOperationError,
        match="成本金额",
    ):
        _create_legacy_site_issue_fixture(
            db,
            project_id=create_project.project_id,
            body={
                "issue_no": "ISSUE-COST-OVERFLOW-CREATE",
                "issue_date": "2026-08-01",
                "raw_status": "synthetic-confirmed",
                "status_mapping_state": "mapped",
                "normalized_status": "confirmed",
                "status_mapping_version": "synthetic-map-v1",
                "lines": [{
                    "issue_line_id": "issue-line-cost-overflow-create",
                    "line_no": 1,
                    "part_id": create_part.id,
                    "pn": create_part.pn_std,
                    "quantity": "2",
                    "linked_purchase_line_id": direct.id,
                }],
                "reason": "验证创建路径成本金额溢出受控拒绝",
            },
        )
    db.rollback()
    assert db.get(MaintenanceSiteIssueLine, "issue-line-cost-overflow-create") is None

    status_project = _project(db, project_id="project-cost-overflow-status")
    status_part = DimPart(pn_std="PN-COST-OVERFLOW-STATUS")
    db.add(status_part)
    db.commit()
    status_purchase = add_overflow_purchase(status_part, "STATUS")
    pending = _create_legacy_site_issue_fixture(
        db,
        project_id=status_project.project_id,
        body={
            "issue_no": "ISSUE-COST-OVERFLOW-STATUS",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-pending",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [{
                "issue_line_id": "issue-line-cost-overflow-status",
                "line_no": 1,
                "part_id": status_part.id,
                "pn": status_part.pn_std,
                "quantity": "2",
                "linked_purchase_line_id": status_purchase.id,
            }],
            "reason": "建立待确认成本金额溢出领用",
        },
    )
    confirmed = client.patch(
        f"/api/maintenance/projects/stable/site-issues/{pending['issue_id']}/status",
        json={
            "version": 1,
            "raw_status": "synthetic-confirmed",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v2",
            "reason": "验证状态路径成本金额溢出受控拒绝",
        },
    )
    assert confirmed.status_code == 400, confirmed.text
    assert "成本金额" in confirmed.json()["detail"]
    db.expire_all()
    pending_line = db.get(MaintenanceSiteIssueLine, "issue-line-cost-overflow-status")
    assert pending_line.cost_amount is None
    assert pending_line.version == 1

    recompute_project = _project(db, project_id="project-cost-overflow-recompute")
    recompute_part = DimPart(pn_std="PN-COST-OVERFLOW-RECOMPUTE")
    db.add(recompute_part)
    db.commit()
    _create_legacy_site_issue_fixture(
        db,
        project_id=recompute_project.project_id,
        body={
            "issue_no": "ISSUE-COST-OVERFLOW-RECOMPUTE",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [{
                "issue_line_id": "issue-line-cost-overflow-recompute",
                "line_no": 1,
                "part_id": recompute_part.id,
                "pn": recompute_part.pn_std,
                "quantity": "2",
            }],
            "reason": "建立待重算成本金额溢出领用",
        },
    )
    add_overflow_purchase(recompute_part, "RECOMPUTE")
    recomputed = client.post(
        f"/api/maintenance/projects/stable/{recompute_project.project_id}/cost-gaps/recompute",
        json={"reason": "验证重算路径成本金额溢出受控拒绝"},
    )
    assert recomputed.status_code == 400, recomputed.text
    assert "成本金额" in recomputed.json()["detail"]
    db.expire_all()
    gap_line = db.get(MaintenanceSiteIssueLine, "issue-line-cost-overflow-recompute")
    assert gap_line.cost_amount is None
    assert gap_line.version == 1


def test_contract_relationship_create_is_versioned_and_audited(db):
    project = _project(db)
    client = _client(db)

    response = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-synthetic-001",
            "contract_no": "XS-SYNTH-001",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": date(2026, 1, 1).isoformat(),
            "source": "synthetic-test",
            "reason": "建立合成项目合同关系",
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["project_id"] == project.project_id
    assert payload["contract_amount"] == "1000.00"
    assert payload["included_in_total"] is True
    assert payload["version"] == 1
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    assert state is not None
    assert state.revision == 1


def test_contract_relationship_update_and_archive_use_optimistic_lock(db):
    project = _project(db, project_id="project-contract-lifecycle")
    client = _client(db, username="contract_lifecycle_admin")
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-lifecycle-001",
            "contract_no": "XS-LIFECYCLE-001",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立待更新合同关系",
        },
    ).json()

    changed = client.patch(
        f"/api/maintenance/projects/stable/contracts/{created['project_contract_id']}",
        json={
            "version": 1,
            "contract_amount": "1200.00",
            "status_mapping_version": "synthetic-map-v2",
            "reason": "合同金额经确认后更正",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["contract_amount"] == "1200.00"
    assert changed.json()["version"] == 2

    stale = client.patch(
        f"/api/maintenance/projects/stable/contracts/{created['project_contract_id']}",
        json={"version": 1, "contract_amount": "1300.00", "reason": "过期修改"},
    )
    assert stale.status_code == 409

    archived = client.post(
        f"/api/maintenance/projects/stable/contracts/{created['project_contract_id']}/archive",
        json={"version": 2, "effective_to": "2026-08-01", "reason": "合同关系结束"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["effective_to"] == "2026-08-01"
    assert archived.json()["version"] == 3


def test_direct_collection_api_exposes_server_owned_provenance(db):
    project = _project(db, project_id="project-direct-collection-provenance")
    client = _client(db, username="direct_collection_provenance_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-direct-collection-provenance",
            "contract_no": "XS-DIRECT-COLLECTION-PROVENANCE",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立回款来源测试合同",
        },
    )
    assert contract.status_code == 201, contract.text

    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": contract.json()["project_contract_id"],
            "report_month": "2026-08-01",
            "cumulative_amount": "320.00",
            "status": "confirmed",
            "reason": "通过受控 API 新增回款",
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["source"] == "direct_api"
    assert created.json()["import_batch_id"] is None
    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-08-31"},
    )
    assert workspace.status_code == 200, workspace.text
    collection = workspace.json()["collection_snapshots"]["rows"][0]
    assert collection["source"] == "direct_api"
    assert collection["import_batch_id"] is None


def test_confirmed_monthly_collection_snapshot_drives_workspace_progress(db):
    project = _project(db, project_id="project-collection-workspace")
    client = _client(db, username="collection_workspace_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-collection-001",
            "contract_no": "XS-COLLECTION-001",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立回款测试合同",
        },
    ).json()

    january = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": contract["project_contract_id"],
            "report_month": "2026-01-01",
            "cumulative_amount": "200.00",
            "status": "confirmed",
            "receipt_reference": "synthetic-receipt-jan",
            "reason": "一月累计回款确认",
        },
    )
    assert january.status_code == 201, january.text

    duplicate = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": contract["project_contract_id"],
            "report_month": "2026-01-01",
            "cumulative_amount": "999.00",
            "status": "confirmed",
            "reason": "错误的重复月份",
        },
    )
    assert duplicate.status_code == 409

    corrected = client.patch(
        f"/api/maintenance/projects/stable/collections/{january.json()['collection_id']}",
        json={
            "version": 1,
            "cumulative_amount": "250.00",
            "reason": "按财务回执更正一月累计值",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["version"] == 2

    february = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": contract["project_contract_id"],
            "report_month": "2026-02-01",
            "cumulative_amount": "400.00",
            "status": "unconfirmed",
            "reason": "二月数据仍待财务确认",
        },
    )
    assert february.status_code == 201, february.text

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-02-28"},
    )
    assert workspace.status_code == 200, workspace.text
    payload = workspace.json()
    assert payload["project"]["project_id"] == project.project_id
    metrics = payload["project"]["metrics"]
    assert metrics["total_contract_amount"] == "1000.00"
    assert metrics["received_amount"] == "250.00"
    assert metrics["collection_progress_pct"] == "25.00"
    assert payload["as_of"] == "2026-02-28"
    assert payload["data_version"]
    assert payload["completeness"]["status"] == "incomplete"
    assert "expense_data_not_ready" in {
        row["code"] for row in payload["completeness"]["issues"]
    }
    assert "completeness:expense_data_not_ready" in {
        row["rule_key"] for row in payload["reminders"]
    }
    assert "collection:incomplete" in {
        row["rule_key"] for row in payload["reminders"]
    }
    assert payload["workbook_preview"]["sheets"][0]["row_count"] == 3
    filtered = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "as_of": "2026-02-28",
            "q": project.project_code,
            "reminder": "collection:incomplete",
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert filtered.json()["rows"][0]["project_id"] == project.project_id
    db.expire_all()
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == 4


def test_workspace_exposes_all_collection_statuses_through_as_of_and_hides_money(db):
    project = _project(db, project_id="project-collection-detail")
    client = _client(db, username="collection_detail_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-collection-detail",
            "contract_no": "XS-COLLECTION-DETAIL",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立回款下钻测试合同",
        },
    ).json()
    snapshots = [
        ("2026-01-01", "100.00", "confirmed", "RECEIPT-JAN", "一月已确认"),
        ("2026-02-01", "150.00", "unconfirmed", "RECEIPT-FEB", "二月待确认"),
        ("2026-03-01", "180.00", "void", "RECEIPT-MAR", "三月已作废"),
        ("2026-04-01", "250.00", "confirmed", "RECEIPT-APR", "四月已确认"),
    ]
    created = []
    for month, amount, snapshot_status, receipt, remark in snapshots:
        response = client.post(
            f"/api/maintenance/projects/stable/{project.project_id}/collections",
            json={
                "project_contract_id": contract["project_contract_id"],
                "report_month": month,
                "cumulative_amount": amount,
                "status": snapshot_status,
                "receipt_reference": receipt,
                "remark": remark,
                "reason": f"建立 {month} 回款快照",
            },
        )
        assert response.status_code == 201, response.text
        created.append(response.json())

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-03-31"},
    )
    assert workspace.status_code == 200, workspace.text
    rows = workspace.json()["collection_snapshots"]["rows"]
    # 2026-08-20 起未来月份实收不再被 as_of 静默隐藏（770c68a：页面空白无解释的
    # 根因）——行集含四月，指标计算仍只认 <= as_of。
    assert [row["status"] for row in rows] == [
        "confirmed", "unconfirmed", "void", "confirmed"]
    assert [row["report_month"] for row in rows] == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
        "2026-04-01",
    ]
    assert workspace.json()["collection_snapshots"]["total"] == 4
    assert rows[0] == {
        "collection_id": created[0]["collection_id"],
        "project_contract_id": contract["project_contract_id"],
        "contract_no": "XS-COLLECTION-DETAIL",
        "report_month": "2026-01-01",
        "cumulative_amount": "100.00",
        "receipt_reference": "RECEIPT-JAN",
        "status": "confirmed",
        "remark": "一月已确认",
        "source": "direct_api",
        "import_batch_id": None,
        "version": 1,
    }

    restricted = _permission_client(
        db,
        username="collection_detail_restricted",
        permissions={"page_maintenance": True, "data_profit": False},
    )
    hidden = restricted.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-03-31"},
    )
    assert hidden.status_code == 200, hidden.text
    hidden_rows = hidden.json()["collection_snapshots"]["rows"]
    assert len(hidden_rows) == 4
    assert {row["status"] for row in hidden_rows} == {
        "confirmed",
        "unconfirmed",
        "void",
    }
    assert all(row["cumulative_amount"] is None for row in hidden_rows)
    assert all(row["receipt_reference"] is None for row in hidden_rows)
    assert hidden_rows[0]["contract_no"] == "XS-COLLECTION-DETAIL"
    assert all(row["remark"] is None for row in hidden_rows)

    restricted_workbook = operations_service.project_workbook_workspace(
        db,
        project_id=project.project_id,
        as_of=date(2026, 3, 31),
        user_ctx=UserContext(
            user_id="collection-detail-restricted",
            role="boss",
            permissions={"page_maintenance": True, "data_profit": False},
        ),
    )
    assert restricted_workbook is not None
    workbook_rows = restricted_workbook["collection_snapshots"]
    # 工作簿读路径（project_workbook_workspace）是 Excel 总表的时点快照语义，
    # 与面板行集（上面含未来月）不同口径——仍按 <= as_of 过滤。
    assert [row["status"] for row in workbook_rows] == [
        "confirmed",
        "unconfirmed",
        "void",
    ]
    assert [row["report_month"] for row in workbook_rows] == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
    ]
    assert all(row["cumulative_amount"] is None for row in workbook_rows)
    assert all(row["receipt_reference"] is None for row in workbook_rows)
    assert all(row["remark"] is None for row in workbook_rows)


def test_confirmed_collection_is_monotonic_across_months_without_failed_side_effects(db):
    project = _project(db, project_id="project-collection-monotonic")
    client = _client(db, username="collection_monotonic_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-collection-monotonic",
            "contract_no": "XS-COLLECTION-MONOTONIC",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立累计回款单调性合同",
        },
    ).json()

    def create(month: str, amount: str, snapshot_status: str = "confirmed"):
        return client.post(
            f"/api/maintenance/projects/stable/{project.project_id}/collections",
            json={
                "project_contract_id": contract["project_contract_id"],
                "report_month": month,
                "cumulative_amount": amount,
                "status": snapshot_status,
                "reason": "验证累计回款单调性",
            },
        )

    january = create("2026-01-01", "100.00")
    march = create("2026-03-01", "300.00")
    assert january.status_code == march.status_code == 201

    def committed_counts() -> tuple[int, int, int]:
        db.expire_all()
        return (
            len(list(db.scalars(select(MaintenanceCollectionSnapshot).where(
                MaintenanceCollectionSnapshot.project_id == project.project_id
            )))),
            len(list(db.scalars(select(MaintenanceProjectOperationAudit).where(
                MaintenanceProjectOperationAudit.project_id == project.project_id,
                MaintenanceProjectOperationAudit.entity_type == "collection",
            )))),
            db.get(MaintenanceProjectWorkbookState, project.project_id).revision,
        )

    before_failure = committed_counts()
    below_earlier = create("2026-02-01", "50.00")
    assert below_earlier.status_code == 400
    assert "不得低于更早月份" in below_earlier.json()["detail"]
    assert committed_counts() == before_failure

    february = create("2026-02-01", "200.00")
    assert february.status_code == 201, february.text

    before_failure = committed_counts()
    above_later = client.patch(
        f"/api/maintenance/projects/stable/collections/{february.json()['collection_id']}",
        json={
            "version": 1,
            "cumulative_amount": "350.00",
            "reason": "不应超过三月累计回款",
        },
    )
    assert above_later.status_code == 400
    assert "不得高于更晚月份" in above_later.json()["detail"]
    assert committed_counts() == before_failure

    moved_after_later = client.patch(
        f"/api/maintenance/projects/stable/collections/{february.json()['collection_id']}",
        json={
            "version": 1,
            "report_month": "2026-04-01",
            "reason": "不应把较低累计值移到三月之后",
        },
    )
    assert moved_after_later.status_code == 400
    assert "不得低于更早月份" in moved_after_later.json()["detail"]
    assert committed_counts() == before_failure

    second_contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-collection-status-transition",
            "contract_no": "XS-COLLECTION-STATUS",
            "contract_amount": "500.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立确认状态切换测试合同",
        },
    ).json()
    first = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": second_contract["project_contract_id"],
            "report_month": "2026-01-01",
            "cumulative_amount": "100.00",
            "status": "confirmed",
            "reason": "建立一月已确认快照",
        },
    )
    pending = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": second_contract["project_contract_id"],
            "report_month": "2026-02-01",
            "cumulative_amount": "50.00",
            "status": "unconfirmed",
            "reason": "建立二月待确认快照",
        },
    )
    assert first.status_code == pending.status_code == 201
    before_failure = committed_counts()
    confirm_invalid = client.patch(
        f"/api/maintenance/projects/stable/collections/{pending.json()['collection_id']}",
        json={
            "version": 1,
            "status": "confirmed",
            "reason": "不应确认倒退累计回款",
        },
    )
    assert confirm_invalid.status_code == 400
    assert committed_counts() == before_failure


def test_confirmed_site_issue_uses_direct_then_full_purchase_window(db):
    project = _project(db, project_id="project-site-issue-cost")
    client = _client(db, username="site_issue_cost_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-site-issue-001",
            "contract_no": "XS-SITE-ISSUE-001",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立领用成本测试合同",
        },
    ).json()
    assert contract["version"] == 1
    assert contract["contract_amount_basis"] == "inc_tax"

    batch = _batch(db, "site-issue-cost")
    direct_part = DimPart(pn_std="PN-SYNTH-DIRECT")
    window_part = DimPart(pn_std="PN-SYNTH-WINDOW")
    db.add_all([direct_part, window_part])
    db.flush()
    direct_order = FPurchaseOrder(
        raw_order_id="PO-H-DIRECT",
        order_no="PO-DIRECT",
        order_date=date(2026, 5, 1),
        data_status="已生效",
        is_tax_inclusive=True,
        import_batch_id=batch.id,
    )
    window_order_a = FPurchaseOrder(
        raw_order_id="PO-H-WINDOW-A",
        order_no="PO-WINDOW-A",
        order_date=date(2026, 5, 5),
        data_status="已生效",
        import_batch_id=batch.id,
    )
    window_order_b = FPurchaseOrder(
        raw_order_id="PO-H-WINDOW-B",
        order_no="PO-WINDOW-B",
        order_date=date(2026, 5, 15),
        data_status="已生效",
        import_batch_id=batch.id,
    )
    db.add_all([direct_order, window_order_a, window_order_b])
    db.flush()
    direct_line = FPurchaseLine(
        raw_line_id="PO-L-DIRECT",
        order_id=direct_order.id,
        part_id=direct_part.id,
        pn_std=direct_part.pn_std,
        qty=10,
        unit_price=11,
        import_batch_id=batch.id,
    )
    db.add_all(
        [
            direct_line,
            FPurchaseLine(
                raw_line_id="PO-L-WINDOW-A",
                order_id=window_order_a.id,
                part_id=window_part.id,
                pn_std=window_part.pn_std,
                qty=2,
                unit_price=20,
                import_batch_id=batch.id,
            ),
            FPurchaseLine(
                raw_line_id="PO-L-WINDOW-B",
                order_id=window_order_b.id,
                part_id=window_part.id,
                pn_std=window_part.pn_std,
                qty=3,
                unit_price=30,
                import_batch_id=batch.id,
            ),
        ]
    )
    db.commit()

    created = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-SYNTH-001",
            "issue_date": "2026-05-10",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-issue-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-direct",
                    "line_no": 1,
                    "part_id": direct_part.id,
                    "pn": direct_part.pn_std,
                    "quantity": "2",
                    "linked_purchase_line_id": direct_line.id,
                },
                {
                    "issue_line_id": "issue-line-window",
                    "line_no": 2,
                    "part_id": window_part.id,
                    "pn": window_part.pn_std,
                    "quantity": "2",
                },
            ],
            "reason": "导入已确认现场领用事实",
        },
    )
    lines = {row["issue_line_id"]: row for row in created["lines"]}
    assert lines["issue-line-direct"]["cost_source"] == "direct_purchase"
    assert lines["issue-line-direct"]["cost_amount"] == "19.46"
    assert lines["issue-line-direct"]["unit_cost_ex_tax"] == "9.73"
    assert lines["issue-line-direct"]["unit_cost_inc_tax"] == "10.99"
    assert lines["issue-line-direct"]["cost_amount_ex_tax"] == "19.46"
    assert lines["issue-line-direct"]["cost_amount_inc_tax"] == "21.98"
    assert lines["issue-line-direct"]["tax_rate_used"] == "0.13"
    assert lines["issue-line-direct"]["price_basis"] == "ex_tax"
    assert lines["issue-line-direct"]["reference_samples"][0]["tax_conversion"] == "divide_1.13"
    assert lines["issue-line-window"]["cost_source"] == "purchase_window"
    assert lines["issue-line-window"]["unit_cost"] == "26.00"
    assert lines["issue-line-window"]["unit_cost_inc_tax"] == "29.38"
    assert lines["issue-line-window"]["cost_amount_inc_tax"] == "58.76"
    assert lines["issue-line-window"]["reference_sample_count"] == 2

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-05-31"},
    ).json()
    metrics = workspace["project"]["metrics"]
    assert metrics["contract_amount_basis"] == "inc_tax"
    assert metrics["site_requisition_known_cost"] == "80.74"
    assert metrics["site_requisition_known_cost_ex_tax"] == "71.46"
    assert metrics["site_requisition_known_cost_inc_tax"] == "80.74"
    assert metrics["actual_project_cost_known"] == "80.74"
    assert metrics["missing_cost_lines"] == 0
    requisitions = {row["line_id"]: row for row in workspace["requisitions"]["rows"]}
    assert requisitions["issue-line-direct"]["order_no"] == "ISSUE-SYNTH-001"
    assert requisitions["issue-line-direct"]["order_date"] == "2026-05-10"
    assert requisitions["issue-line-direct"]["contract_no"] == "XS-SITE-ISSUE-001"
    assert requisitions["issue-line-direct"]["cost_status"] == "available"
    assert workspace["workbook_preview"]["sheets"][0] == {
        "code": "overview",
        "name": "01_总览",
        "row_count": 1,
        "ownership": "append_only",
    }
    assert workspace["workbook_preview"]["sheets"][1]["row_count"] == 2


def test_site_issue_status_lifecycle_resolves_cost_and_void_only_stops_counting(db):
    project = _project(db, project_id="project-site-issue-status")
    client = _client(db, username="site_issue_status_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-site-status-001",
            "contract_no": "XS-SITE-STATUS-001",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立状态生命周期测试合同",
        },
    )
    assert contract.status_code == 201, contract.text

    batch = _batch(db, "site-issue-status")
    part = DimPart(pn_std="PN-SYNTH-STATUS")
    db.add(part)
    db.flush()
    order = FPurchaseOrder(
        raw_order_id="PO-H-STATUS",
        order_no="PO-STATUS",
        order_date=date(2026, 5, 10),
        data_status="已生效",
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    purchase_line = FPurchaseLine(
        raw_line_id="PO-L-STATUS",
        order_id=order.id,
        part_id=part.id,
        pn_std=part.pn_std,
        qty=10,
        unit_price=25,
        import_batch_id=batch.id,
    )
    db.add(purchase_line)
    db.commit()

    issue = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-SYNTH-STATUS",
            "issue_date": "2026-05-10",
            "raw_status": "synthetic-pending",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-issue-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-status",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "2",
                    "linked_purchase_line_id": purchase_line.id,
                }
            ],
            "reason": "导入待确认现场领用",
        },
    )
    assert issue["normalized_status"] == "unknown"
    assert issue["lines"][0]["cost_amount"] is None
    assert issue["lines"][0]["version"] == 1

    confirmed = client.patch(
        f"/api/maintenance/projects/stable/site-issues/{issue['issue_id']}/status",
        json={
            "version": 1,
            "raw_status": "synthetic-confirmed",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-issue-map-v2",
            "reason": "现场负责人确认领用",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_payload = confirmed.json()
    assert confirmed_payload["status_mapping_state"] == "mapped"
    assert confirmed_payload["normalized_status"] == "confirmed"
    assert confirmed_payload["version"] == 2
    assert confirmed_payload["lines"][0]["cost_source"] == "direct_purchase"
    assert confirmed_payload["lines"][0]["cost_amount"] == "50.00"
    assert confirmed_payload["lines"][0]["version"] == 2

    stale = client.patch(
        f"/api/maintenance/projects/stable/site-issues/{issue['issue_id']}/status",
        json={
            "version": 1,
            "raw_status": "synthetic-void",
            "normalized_status": "void",
            "status_mapping_version": "synthetic-issue-map-v2",
            "reason": "使用过期版本作废",
        },
    )
    assert stale.status_code == 409

    voided = client.patch(
        f"/api/maintenance/projects/stable/site-issues/{issue['issue_id']}/status",
        json={
            "version": 2,
            "raw_status": "synthetic-void",
            "normalized_status": "void",
            "status_mapping_version": "synthetic-issue-map-v2",
            "reason": "原单据已作废",
        },
    )
    assert voided.status_code == 200, voided.text
    voided_payload = voided.json()
    assert voided_payload["normalized_status"] == "void"
    assert voided_payload["version"] == 3
    assert voided_payload["lines"][0]["cost_amount"] == "50.00"
    assert voided_payload["lines"][0]["version"] == 2

    cannot_reopen = client.patch(
        f"/api/maintenance/projects/stable/site-issues/{issue['issue_id']}/status",
        json={
            "version": 3,
            "raw_status": "synthetic-confirmed-again",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-issue-map-v3",
            "reason": "不允许恢复已作废事实",
        },
    )
    assert cannot_reopen.status_code == 400

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-05-31"},
    )
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["project"]["metrics"]["site_requisition_known_cost"] == "0.00"

    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, "issue-line-status")
    assert line.cost_amount == 50
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    assert state.revision == 4
    audits = list(
        db.scalars(
            select(MaintenanceProjectOperationAudit)
            .where(
                MaintenanceProjectOperationAudit.entity_type == "site_issue",
                MaintenanceProjectOperationAudit.entity_id == issue["issue_id"],
                MaintenanceProjectOperationAudit.action == "status_update",
            )
            .order_by(MaintenanceProjectOperationAudit.id)
        )
    )
    assert len(audits) == 2
    assert audits[0].before_json["normalized_status"] == "unknown"
    assert audits[0].before_json["lines"][0]["cost_amount"] is None
    assert audits[0].after_json["normalized_status"] == "confirmed"
    assert audits[0].after_json["lines"][0]["cost_amount"] == "50.00"
    assert audits[1].before_json["normalized_status"] == "confirmed"
    assert audits[1].after_json["normalized_status"] == "void"


def test_sales_fallback_is_ex_tax_and_manual_fill_only_resolves_a_gap(db):
    project = _project(db, project_id="project-sales-manual-gap")
    client = _client(db, username="sales_manual_gap_admin")
    client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-sales-gap-001",
            "contract_no": "XS-SALES-GAP-001",
            "contract_amount": "5000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立销售回退与人工补价测试合同",
        },
    )
    batch = _batch(db, "sales-manual-gap")
    sales_part = DimPart(pn_std="PN-SYNTH-SALES")
    gap_part = DimPart(pn_std="PN-SYNTH-GAP")
    db.add_all([sales_part, gap_part])
    db.flush()
    sales_order_a = FSalesOrder(
        raw_order_id="SO-H-SALES-A",
        order_no="SO-SALES-A",
        order_date=date(2026, 6, 3),
        data_status="已生效",
        import_batch_id=batch.id,
    )
    sales_order_b = FSalesOrder(
        raw_order_id="SO-H-SALES-B",
        order_no="SO-SALES-B",
        order_date=date(2026, 6, 14),
        data_status="已生效",
        import_batch_id=batch.id,
    )
    db.add_all([sales_order_a, sales_order_b])
    db.flush()
    db.add_all(
        [
            FSalesLine(
                raw_line_id="SO-L-SALES-A",
                order_id=sales_order_a.id,
                part_id=sales_part.id,
                pn_std=sales_part.pn_std,
                qty=2,
                unit_price=113,
                import_batch_id=batch.id,
            ),
            FSalesLine(
                raw_line_id="SO-L-SALES-B",
                order_id=sales_order_b.id,
                part_id=sales_part.id,
                pn_std=sales_part.pn_std,
                qty=1,
                unit_price=226,
                import_batch_id=batch.id,
            ),
        ]
    )
    db.commit()

    created = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-SYNTH-SALES-GAP",
            "issue_date": "2026-06-09",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-issue-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-sales-fallback",
                    "line_no": 1,
                    "part_id": sales_part.id,
                    "pn": sales_part.pn_std,
                    "quantity": "3",
                },
                {
                    "issue_line_id": "issue-line-manual-gap",
                    "line_no": 2,
                    "part_id": gap_part.id,
                    "pn": gap_part.pn_std,
                    "quantity": "4",
                },
            ],
            "reason": "导入销售回退与缺价领用",
        },
    )
    lines = {row["issue_line_id"]: row for row in created["lines"]}
    assert lines["issue-line-sales-fallback"]["cost_source"] == "sales_window"
    assert lines["issue-line-sales-fallback"]["unit_cost"] == "133.33"
    assert lines["issue-line-sales-fallback"]["cost_amount"] == "399.99"
    assert lines["issue-line-sales-fallback"]["unit_cost_inc_tax"] == "150.66"
    assert lines["issue-line-sales-fallback"]["cost_amount_inc_tax"] == "451.98"
    assert all(
        sample["tax_conversion"] == "divide_1.13"
        for sample in lines["issue-line-sales-fallback"]["reference_samples"]
    )
    assert lines["issue-line-sales-fallback"]["cost_evidence_kind"] == "sales_estimate"
    assert lines["issue-line-sales-fallback"]["cost_is_estimate"] is True
    assert (
        lines["issue-line-sales-fallback"]["cost_source_label"]
        == "估算（销售前后 7 天数量加权）"
    )
    assert lines["issue-line-manual-gap"]["cost_source"] is None

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-06-30"},
    )
    assert workspace.status_code == 200, workspace.text
    metrics = workspace.json()["project"]["metrics"]
    assert metrics["sales_estimate_lines"] == 1
    assert metrics["sales_estimate_cost_ex_tax"] == "399.99"
    assert metrics["sales_estimate_cost_inc_tax"] == "451.98"
    assert metrics["cost_progress_includes_sales_estimate"] is True
    assert metrics["cost_progress_label"] == "priced_cost_including_sales_estimate"
    assert metrics["cost_status"] == "unknown"
    estimate_warning = next(
        row
        for row in workspace.json()["reminders"]
        if row["rule_key"] == "cost:sales_fallback_estimate"
    )
    assert estimate_warning["title"] == "核对项目成本中的销售回退估算"
    assert "1 条已确认现场领用" in estimate_warning["detail"]
    assert "不等于采购或人工确认单价" in estimate_warning["detail"]
    assert not any(
        row["rule_key"].startswith("cost_ratio:")
        for row in workspace.json()["reminders"]
    )

    directory = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={"as_of": "2026-06-30", "q": project.project_code},
    )
    assert directory.status_code == 200, directory.text
    directory_metrics = directory.json()["rows"][0]["metrics"]
    assert directory_metrics["sales_estimate_lines"] == 1
    assert directory_metrics["sales_estimate_cost_ex_tax"] == "399.99"
    assert directory_metrics["sales_estimate_cost_inc_tax"] == "451.98"
    assert directory_metrics["cost_progress_includes_sales_estimate"] is True
    assert directory_metrics["cost_progress_label"] == (
        "priced_cost_including_sales_estimate"
    )
    estimate_filtered = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "as_of": "2026-06-30",
            "q": project.project_code,
            "reminder": "cost:sales_fallback_estimate",
        },
    )
    assert estimate_filtered.status_code == 200, estimate_filtered.text
    assert estimate_filtered.json()["total"] == 1
    assert estimate_filtered.json()["rows"][0]["project_id"] == project.project_id

    gaps = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps"
    )
    assert gaps.status_code == 200, gaps.text
    assert [row["line_id"] for row in gaps.json()["rows"]] == [
        "issue-line-manual-gap"
    ]
    assert gaps.json()["rows"][0]["project_code"] == project.project_code
    assert gaps.json()["rows"][0]["current_unit_cost"] is None

    filled = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-manual-gap",
            "version": 1,
            "unit_cost_ex_tax": "12.50",
            "evidence": "已审批补价单 SYNTH-001",
            "reason": "按已审批补价单回填",
        },
    )
    assert filled.status_code == 200, filled.text
    assert filled.json()["cost_source"] == "manual"
    assert filled.json()["cost_amount"] == "50.00"
    assert filled.json()["manual_unit_cost_inc_tax"] == "14.13"
    assert filled.json()["unit_cost_ex_tax"] == "12.50"
    assert filled.json()["unit_cost_inc_tax"] == "14.13"
    assert filled.json()["cost_amount_ex_tax"] == "50.00"
    assert filled.json()["cost_amount_inc_tax"] == "56.52"
    assert filled.json()["version"] == 2

    state_before_repeat = db.get(
        MaintenanceProjectWorkbookState, project.project_id
    ).revision
    audit_count_before_repeat = len(
        list(
            db.scalars(
                select(MaintenanceProjectOperationAudit).where(
                    MaintenanceProjectOperationAudit.project_id == project.project_id,
                    MaintenanceProjectOperationAudit.entity_id
                    == "issue-line-manual-gap",
                )
            )
        )
    )
    cannot_replace_manual = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-manual-gap",
            "version": 2,
            "unit_cost_ex_tax": "99.00",
            "evidence": "不应覆盖既有人工成本",
            "reason": "验证人工补价只接管缺价行",
        },
    )
    assert cannot_replace_manual.status_code == 409
    db.expire_all()
    manual_line = db.get(MaintenanceSiteIssueLine, "issue-line-manual-gap")
    assert manual_line.unit_cost == Decimal("12.50")
    assert manual_line.cost_amount == Decimal("50.00")
    assert manual_line.version == 2
    assert (
        db.get(MaintenanceProjectWorkbookState, project.project_id).revision
        == state_before_repeat
    )
    assert len(
        list(
            db.scalars(
                select(MaintenanceProjectOperationAudit).where(
                    MaintenanceProjectOperationAudit.project_id == project.project_id,
                    MaintenanceProjectOperationAudit.entity_id
                    == "issue-line-manual-gap",
                )
            )
        )
    ) == audit_count_before_repeat

    cannot_override = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-sales-fallback",
            "version": 1,
            "unit_cost_ex_tax": "1.00",
            "evidence": "无效人工证据",
            "reason": "不应覆盖自动强证据",
        },
    )
    assert cannot_override.status_code == 409

    restricted = operations_service.project_workspace(
        db,
        project_id=project.project_id,
        as_of=date(2026, 6, 30),
        user_ctx=UserContext(
            user_id="restricted-synthetic-user",
            role="readonly",
            permissions={"page_maintenance": True},
        ),
    )
    restricted_metrics = restricted["project"]["metrics"]
    assert restricted_metrics["contract_amount_complete"] is None
    assert restricted_metrics["site_requisition_known_cost"] is None
    assert restricted_metrics["site_requisition_known_cost_ex_tax"] is None
    assert restricted_metrics["site_requisition_known_cost_inc_tax"] is None
    assert restricted_metrics["site_requisition_priced_cost_ex_tax"] is None
    assert restricted_metrics["site_requisition_priced_cost_inc_tax"] is None
    assert restricted_metrics["sales_estimate_cost_ex_tax"] is None
    assert restricted_metrics["sales_estimate_cost_inc_tax"] is None
    assert restricted_metrics["sales_estimate_lines"] is None
    assert restricted_metrics["cost_progress_includes_sales_estimate"] is None
    assert restricted_metrics["cost_progress_label"] is None
    assert restricted_metrics["actual_project_cost_known"] is None
    assert restricted_metrics["actual_project_cost_known_ex_tax"] is None
    assert restricted_metrics["actual_project_cost_known_inc_tax"] is None
    assert restricted_metrics["cost_status"] is None
    assert restricted["requisitions"]["rows"][0]["unit_cost"] is None
    assert restricted["requisitions"]["rows"][0]["unit_cost_ex_tax"] is None
    assert restricted["requisitions"]["rows"][0]["unit_cost_inc_tax"] is None
    assert restricted["requisitions"]["rows"][0]["cost_amount_ex_tax"] is None
    assert restricted["requisitions"]["rows"][0]["cost_amount_inc_tax"] is None
    assert restricted["requisitions"]["rows"][0]["reference_samples"] == []
    assert restricted["requisitions"]["rows"][0]["cost_source"] is None
    assert restricted["requisitions"]["rows"][0]["cost_evidence_kind"] is None
    assert restricted["requisitions"]["rows"][0]["cost_is_estimate"] is None
    assert restricted["requisitions"]["rows"][0]["cost_source_label"] is None
    assert restricted["requisitions"]["rows"][0]["cost_status"] == "restricted"
    assert not any(
        row["rule_key"].startswith(("collection:", "cost_ratio:"))
        for row in restricted["reminders"]
    )
    restricted_directory = operations_service.project_operations(
        db,
        as_of=date(2026, 6, 30),
        q_text=project.project_code,
        user_ctx=UserContext(
            user_id="restricted-directory-user",
            role="readonly",
            permissions={"page_maintenance": True},
        ),
    )
    restricted_directory_metrics = restricted_directory["rows"][0]["metrics"]
    assert restricted_directory_metrics["site_requisition_priced_cost_ex_tax"] is None
    assert restricted_directory_metrics["site_requisition_priced_cost_inc_tax"] is None
    assert restricted_directory_metrics["sales_estimate_cost_ex_tax"] is None
    assert restricted_directory_metrics["sales_estimate_cost_inc_tax"] is None
    assert restricted_directory_metrics["sales_estimate_lines"] is None
    assert (
        restricted_directory_metrics["cost_progress_includes_sales_estimate"]
        is None
    )
    assert restricted_directory_metrics["cost_progress_label"] is None


def test_only_explicitly_mapped_approved_expense_counts(db):
    project = _project(db, project_id="project-expense-mapping")
    client = _client(db, username="expense_mapping_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-expense-001",
            "contract_no": "XS-EXPENSE-001",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立报销归集测试合同",
        },
    ).json()
    approved = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-approved-001",
            "project_contract_id": contract["project_contract_id"],
            "expense_ref": "BX-SYNTH-APPROVED",
            "expense_date": "2026-07-10",
            "applicant": "合成报销人",
            "category": "差旅费",
            "expense_reason": "项目现场支持",
            "amount_ex_tax": "50.00",
            "raw_status": "synthetic-finished",
            "status_mapping_state": "mapped",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "导入已审批报销事实",
        },
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["amount_ex_tax"] == "50.00"
    assert approved.json()["amount_inc_tax"] == "56.50"
    assert approved.json()["tax_rate_used"] == "0.13"
    derived_expense_write = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-derived-tax-write",
            "expense_ref": "BX-DERIVED-TAX-WRITE",
            "expense_date": "2026-07-10",
            "amount_ex_tax": "50.00",
            "amount_inc_tax": "1.00",
            "raw_status": "synthetic-finished",
            "status_mapping_state": "mapped",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "验证客户端不能写服务器派生报销金额",
        },
    )
    assert derived_expense_write.status_code == 422, derived_expense_write.text
    assert db.get(
        MaintenanceProjectExpenseAttribution,
        "expense-derived-tax-write",
    ) is None
    unmapped = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-unmapped-001",
            "expense_ref": "BX-SYNTH-UNMAPPED",
            "expense_date": "2026-07-11",
            "amount_ex_tax": "999.00",
            "raw_status": "looks-approved-but-is-not-mapped",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "保留未映射报销事实但禁止计入",
        },
    )
    assert unmapped.status_code == 201, unmapped.text

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-07-31"},
    ).json()
    metrics = workspace["project"]["metrics"]
    assert metrics["approved_expense"] == "56.50"
    assert metrics["approved_expense_ex_tax"] == "50.00"
    assert metrics["approved_expense_inc_tax"] == "56.50"
    assert metrics["actual_project_cost_known"] == "56.50"
    assert metrics["actual_project_cost_known_ex_tax"] == "50.00"
    assert metrics["actual_project_cost_known_inc_tax"] == "56.50"
    assert metrics["cost_progress_basis"] == "inc_tax"
    assert workspace["approved_expenses"]["total"] == 1
    assert workspace["approved_expenses"]["rows"][0]["contract_no"] == "XS-EXPENSE-001"
    assert workspace["approved_expenses"]["rows"][0]["category"] == "差旅费"
    assert workspace["approved_expenses"]["rows"][0]["reason"] == "项目现场支持"
    assert workspace["approved_expenses"]["rows"][0]["amount"] == "56.50"
    assert workspace["approved_expenses"]["rows"][0]["amount_ex_tax"] == "50.00"
    assert workspace["approved_expenses"]["rows"][0]["amount_inc_tax"] == "56.50"
    assert workspace["approved_expenses"]["rows"][0]["approval_status"] == "approved"
    assert workspace["completeness"]["status"] == "incomplete"
    assert {row["code"] for row in workspace["completeness"]["issues"]} >= {
        "unmapped_expense_status",
        "expense_data_not_ready",
    }


def test_expense_readiness_is_explicit_monthly_correctable_and_audited(
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        operations_service,
        "business_today",
        lambda: date(2026, 8, 9),
    )
    project = _project(db, project_id="project-expense-readiness")
    client = _client(db, username="expense_readiness_admin")
    client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-expense-readiness",
            "contract_no": "XS-EXPENSE-READINESS",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立费用水位测试合同",
        },
    )

    before = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-07-31"},
    ).json()
    assert before["project"]["metrics"]["approved_expense"] == "0.00"
    assert before["project"]["metrics"]["expense_data_ready"] is False
    assert before["project"]["metrics"]["cost_complete"] is False
    assert before["project"]["metrics"]["cost_status"] == "unknown"

    future = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-09-01",
            "reason": "未来月份不得提前宣告完整",
        },
    )
    assert future.status_code == 400, future.text
    assert "未来" in future.json()["detail"]

    marked = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-07-01",
            "reason": "财务接口确认七月已审批报销同步完成，允许零行",
        },
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["expense_ready_through"] == "2026-07-01"
    assert marked.json()["version"] >= 1
    after = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-07-31"},
    ).json()
    assert after["project"]["metrics"]["expense_data_ready"] is True
    assert after["project"]["metrics"]["cost_complete"] is True
    assert after["project"]["metrics"]["cost_status"] == "normal"
    assert after["workbook_revision"] == marked.json()["version"]
    workspace_version = after["workbook_revision"]
    assert "expense_data_not_ready" not in {
        row["code"] for row in after["completeness"]["issues"]
    }
    audit = db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_type="expense_readiness",
    ).one()
    assert audit.action == "mark_ready"
    assert audit.before_json == {
        "expense_ready_through": None,
        "version": marked.json()["version"] - 1,
    }
    assert audit.after_json == {
        "expense_ready_through": "2026-07-01",
        "version": marked.json()["version"],
    }

    invalid_day = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={"ready_through": "2026-08-02", "reason": "日期格式错误"},
    )
    assert invalid_day.status_code == 400
    regressed = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={"ready_through": "2026-06-01", "reason": "禁止回退"},
    )
    assert regressed.status_code == 409

    stale = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-06-01",
            "expected_version": workspace_version - 1,
            "correction_reason": "使用过期版本尝试纠错",
            "reason": "并发冲突测试",
        },
    )
    assert stale.status_code == 409
    assert "当前版本" in stale.json()["detail"]

    missing_correction_reason = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-06-01",
            "expected_version": workspace_version,
            "reason": "缺少纠错原因",
        },
    )
    assert missing_correction_reason.status_code == 400
    assert "纠错原因" in missing_correction_reason.json()["detail"]

    restricted = _permission_client(
        db,
        username="expense_readiness_correction_denied",
        permissions={
            "page_maintenance": True,
            "data_profit": True,
            "action_maintenance_roundtrip_apply": False,
        },
    )
    denied = restricted.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-06-01",
            "expected_version": workspace_version,
            "correction_reason": "无权限账号不得纠错",
            "reason": "权限测试",
        },
    )
    assert denied.status_code == 403

    corrected = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-06-01",
            "expected_version": workspace_version,
            "correction_reason": "财务复核发现七月数据尚未完整同步",
            "reason": "纠正误填费用水位",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["expense_ready_through"] == "2026-06-01"
    assert corrected.json()["version"] == marked.json()["version"] + 1
    audits = list(
        db.scalars(
            select(MaintenanceProjectOperationAudit)
            .where(
                MaintenanceProjectOperationAudit.project_id == project.project_id,
                MaintenanceProjectOperationAudit.entity_type == "expense_readiness",
            )
            .order_by(MaintenanceProjectOperationAudit.id)
        )
    )
    assert [row.action for row in audits] == ["mark_ready", "correct_ready"]
    assert audits[1].before_json == {
        "expense_ready_through": "2026-07-01",
        "version": marked.json()["version"],
    }
    assert audits[1].after_json == {
        "expense_ready_through": "2026-06-01",
        "version": corrected.json()["version"],
    }
    assert audits[1].reason == "财务复核发现七月数据尚未完整同步"


def test_legacy_future_expense_readiness_fails_closed_until_audited_correction(
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        operations_service,
        "business_today",
        lambda: date(2026, 8, 9),
    )
    project = _project(db, project_id="project-ready-future-legacy")
    client = _client(db, username="expense_readiness_future_legacy_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-expense-readiness-future-legacy",
            "contract_no": "XS-EXPENSE-READINESS-FUTURE-LEGACY",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立历史未来费用水位测试合同",
        },
    )
    assert contract.status_code == 201, contract.text

    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    state.expense_ready_through = date(2026, 9, 1)
    db.commit()

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-08-31"},
    )
    assert workspace.status_code == 200, workspace.text
    payload = workspace.json()
    assert payload["project"]["metrics"]["expense_data_ready"] is False
    assert payload["project"]["metrics"]["cost_complete"] is False
    assert payload["project"]["metrics"]["cost_status"] == "unknown"
    assert {row["code"] for row in payload["completeness"]["issues"]} >= {
        "expense_readiness_in_future",
        "expense_data_not_ready",
    }
    future_filter = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "as_of": "2026-08-31",
            "q": project.project_code,
            "reminder": "completeness:expense_readiness_in_future",
        },
    )
    assert future_filter.status_code == 200, future_filter.text
    assert future_filter.json()["total"] == 1

    restricted = _permission_client(
        db,
        username="expense_readiness_future_restricted",
        permissions={"page_maintenance": True, "data_profit": False},
    )
    restricted_workspace = restricted.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-08-31"},
    )
    assert restricted_workspace.status_code == 200, restricted_workspace.text
    restricted_payload = restricted_workspace.json()
    restricted_metrics = restricted_payload["project"]["metrics"]
    assert restricted_metrics["expense_data_ready"] is None
    assert restricted_metrics["expense_ready_through"] is None
    hidden_expense_codes = {
        "expense_readiness_in_future",
        "expense_data_not_ready",
        "unmapped_expense_status",
    }
    assert hidden_expense_codes.isdisjoint(
        row["code"] for row in restricted_payload["completeness"]["issues"]
    )
    assert all(
        not any(row["rule_key"].endswith(code) for code in hidden_expense_codes)
        for row in restricted_payload["reminders"]
    )

    restricted_directory = restricted.post(
        "/api/maintenance/projects/stable/operations/search",
        json={"as_of": "2026-08-31", "q": project.project_code},
    )
    assert restricted_directory.status_code == 200, restricted_directory.text
    assert restricted_directory.json()["total"] == 1
    restricted_card_metrics = restricted_directory.json()["rows"][0]["metrics"]
    assert restricted_card_metrics["expense_data_ready"] is None
    assert restricted_card_metrics["expense_ready_through"] is None
    restricted_future_filter = restricted.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "as_of": "2026-08-31",
            "q": project.project_code,
            "reminder": "completeness:expense_readiness_in_future",
        },
    )
    assert restricted_future_filter.status_code == 403, restricted_future_filter.text
    assert restricted_future_filter.json() == {
        "detail": "当前账号无权使用该提醒筛选"
    }
    assert "future" not in restricted_future_filter.text

    filtered = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "as_of": "2026-08-31",
            "q": project.project_code,
            "reminder": "completeness:expense_data_not_ready",
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1

    corrected = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-08-01",
            "expected_version": payload["workbook_revision"],
            "correction_reason": "纠正历史误填的未来费用水位",
            "reason": "修复历史未来水位",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["expense_ready_through"] == "2026-08-01"
    audit = db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_type="expense_readiness",
    ).one()
    assert audit.action == "correct_ready"
    assert audit.before_json == {
        "expense_ready_through": "2026-09-01",
        "version": payload["workbook_revision"],
    }
    assert audit.after_json == {
        "expense_ready_through": "2026-08-01",
        "version": corrected.json()["version"],
    }
    assert audit.reason == "纠正历史误填的未来费用水位"


def test_expense_status_lifecycle_counts_only_approved_and_preserves_voided_fact(db):
    project = _project(db, project_id="project-expense-status")
    client = _client(db, username="expense_status_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-expense-status-001",
            "contract_no": "XS-EXPENSE-STATUS-001",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立报销状态测试合同",
        },
    )
    assert contract.status_code == 201, contract.text
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-status-001",
            "project_contract_id": contract.json()["project_contract_id"],
            "expense_ref": "BX-SYNTH-STATUS",
            "expense_date": "2026-07-10",
            "amount_ex_tax": "75.00",
            "raw_status": "synthetic-pending",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "导入待审批报销事实",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["version"] == 1

    unknown = client.patch(
        "/api/maintenance/projects/stable/expenses/expense-status-001/status",
        json={
            "version": 1,
            "raw_status": "synthetic-awaiting-review",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-expense-map-v2",
            "reason": "补充最新待审批状态证据",
        },
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["status_mapping_state"] == "unmapped"
    assert unknown.json()["normalized_status"] == "unknown"
    assert unknown.json()["version"] == 2

    rejected = client.patch(
        "/api/maintenance/projects/stable/expenses/expense-status-001/status",
        json={
            "version": 2,
            "raw_status": "synthetic-rejected",
            "normalized_status": "rejected",
            "status_mapping_version": "synthetic-expense-map-v2",
            "reason": "审批结果为驳回",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status_mapping_state"] == "mapped"
    assert rejected.json()["normalized_status"] == "rejected"
    assert rejected.json()["version"] == 3

    approved = client.patch(
        "/api/maintenance/projects/stable/expenses/expense-status-001/status",
        json={
            "version": 3,
            "raw_status": "synthetic-approved",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v3",
            "reason": "重新提交后审批完成",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["normalized_status"] == "approved"
    assert approved.json()["version"] == 4
    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-07-31"},
    )
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["project"]["metrics"]["approved_expense"] == "84.75"

    stale = client.patch(
        "/api/maintenance/projects/stable/expenses/expense-status-001/status",
        json={
            "version": 3,
            "raw_status": "synthetic-void",
            "normalized_status": "void",
            "status_mapping_version": "synthetic-expense-map-v3",
            "reason": "过期版本作废",
        },
    )
    assert stale.status_code == 409
    voided = client.patch(
        "/api/maintenance/projects/stable/expenses/expense-status-001/status",
        json={
            "version": 4,
            "raw_status": "synthetic-void",
            "normalized_status": "void",
            "status_mapping_version": "synthetic-expense-map-v3",
            "reason": "原报销单已作废",
        },
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["normalized_status"] == "void"
    assert voided.json()["amount_ex_tax"] == "75.00"
    assert voided.json()["version"] == 5
    cannot_reopen = client.patch(
        "/api/maintenance/projects/stable/expenses/expense-status-001/status",
        json={
            "version": 5,
            "raw_status": "synthetic-approved-again",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v4",
            "reason": "不允许恢复已作废报销",
        },
    )
    assert cannot_reopen.status_code == 400

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-07-31"},
    )
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["project"]["metrics"]["approved_expense"] == "0.00"
    assert workspace.json()["approved_expenses"]["total"] == 0

    db.expire_all()
    expense = db.get(MaintenanceProjectExpenseAttribution, "expense-status-001")
    assert expense is not None
    assert expense.normalized_status == "void"
    assert expense.amount_ex_tax == 75
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    assert state.revision == 6
    audits = list(
        db.scalars(
            select(MaintenanceProjectOperationAudit)
            .where(
                MaintenanceProjectOperationAudit.entity_type == "expense",
                MaintenanceProjectOperationAudit.entity_id == "expense-status-001",
                MaintenanceProjectOperationAudit.action == "status_update",
            )
            .order_by(MaintenanceProjectOperationAudit.id)
        )
    )
    assert [row.before_json["normalized_status"] for row in audits] == [
        "unknown",
        "unknown",
        "rejected",
        "approved",
    ]
    assert [row.after_json["normalized_status"] for row in audits] == [
        "unknown",
        "rejected",
        "approved",
        "void",
    ]


def test_archived_project_rejects_site_issue_and_expense_status_changes(db):
    project = _project(db, project_id="project-archived-fact-status")
    client = _client(db, username="archived_fact_status_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-archived-facts",
            "contract_no": "XS-ARCHIVED-FACTS",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立归档写边界测试合同",
        },
    )
    assert contract.status_code == 201, contract.text
    collection = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": contract.json()["project_contract_id"],
            "report_month": "2026-07-01",
            "cumulative_amount": "10.00",
            "status": "confirmed",
            "reason": "建立归档写边界测试回款",
        },
    )
    assert collection.status_code == 201, collection.text
    part = DimPart(pn_std="PN-SYNTH-ARCHIVED-STATUS")
    db.add(part)
    db.commit()
    issue = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-SYNTH-ARCHIVED",
            "issue_date": "2026-07-10",
            "raw_status": "synthetic-pending",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-issue-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-archived-status",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                }
            ],
            "reason": "建立归档拒绝测试领用",
        },
    )
    expense = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-archived-status",
            "expense_ref": "BX-SYNTH-ARCHIVED",
            "expense_date": "2026-07-10",
            "amount_ex_tax": "10.00",
            "raw_status": "synthetic-pending",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "建立归档拒绝测试报销",
        },
    )
    assert expense.status_code == 201, expense.text

    db.expire_all()
    archived_project = db.get(MaintenanceProject, project.project_id)
    archived_project.is_active = False
    archived_project.version += 1
    db.commit()

    issue_update = client.patch(
        f"/api/maintenance/projects/stable/site-issues/{issue['issue_id']}/status",
        json={
            "version": 1,
            "raw_status": "synthetic-confirmed",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-issue-map-v2",
            "reason": "归档后不应生效",
        },
    )
    expense_update = client.patch(
        "/api/maintenance/projects/stable/expenses/expense-archived-status/status",
        json={
            "version": 1,
            "raw_status": "synthetic-approved",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v2",
            "reason": "归档后不应生效",
        },
    )
    contract_update = client.patch(
        f"/api/maintenance/projects/stable/contracts/"
        f"{contract.json()['project_contract_id']}",
        json={
            "version": 1,
            "contract_status": "synthetic-changed",
            "reason": "归档后不应修改合同",
        },
    )
    collection_update = client.patch(
        f"/api/maintenance/projects/stable/collections/"
        f"{collection.json()['collection_id']}",
        json={
            "version": 1,
            "cumulative_amount": "20.00",
            "reason": "归档后不应修改回款",
        },
    )
    cost_update = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-archived-status",
            "version": 1,
            "unit_cost_ex_tax": "1.00",
            "evidence": "归档后无效证据",
            "reason": "归档后不应补价",
        },
    )
    assert issue_update.status_code == 400
    assert issue_update.json()["detail"] == "项目主档已归档"
    assert expense_update.status_code == 400
    assert expense_update.json()["detail"] == "项目主档已归档"
    assert contract_update.status_code == 400
    assert collection_update.status_code == 400
    assert cost_update.status_code == 400

    db.expire_all()
    assert db.get(MaintenanceSiteIssueLine, "issue-line-archived-status").cost_amount is None
    assert db.get(
        MaintenanceProjectExpenseAttribution,
        "expense-archived-status",
    ).normalized_status == "unknown"
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == 4
    assert list(
        db.scalars(
            select(MaintenanceProjectOperationAudit).where(
                MaintenanceProjectOperationAudit.project_id == project.project_id,
                MaintenanceProjectOperationAudit.action == "status_update",
            )
        )
    ) == []


def test_new_fact_commands_fail_closed_by_action_and_data_permissions(db):
    project = _project(db, project_id="project-fact-command-permissions")
    common = {
        "page_maintenance": True,
        "data_purchase_cost": True,
        "data_profit": True,
        "action_maintenance_roundtrip_apply": True,
    }
    no_action = _permission_client(
        db,
        username="fact_command_no_action",
        permissions={**common, "action_maintenance_roundtrip_apply": False},
    )
    no_cost = _permission_client(
        db,
        username="fact_command_no_cost",
        permissions={**common, "data_purchase_cost": False},
    )
    no_profit = _permission_client(
        db,
        username="fact_command_no_profit",
        permissions={**common, "data_profit": False},
    )

    readiness_body = {
        "ready_through": "2026-07-01",
        "reason": "权限测试不应写入",
    }
    site_body = {
        "version": 1,
        "raw_status": "confirmed",
        "normalized_status": "confirmed",
        "status_mapping_version": "permission-test-v1",
        "reason": "权限测试不应写入",
    }
    expense_body = {
        "version": 1,
        "raw_status": "approved",
        "normalized_status": "approved",
        "status_mapping_version": "permission-test-v1",
        "reason": "权限测试不应写入",
    }
    assert no_action.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json=readiness_body,
    ).status_code == 403
    assert no_cost.patch(
        "/api/maintenance/projects/stable/site-issues/not-created/status",
        json=site_body,
    ).status_code == 403
    assert no_profit.patch(
        "/api/maintenance/projects/stable/expenses/not-created/status",
        json=expense_body,
    ).status_code == 403

    anonymous_app = FastAPI()
    anonymous_app.include_router(maintenance_project_operations.router, prefix="/api")
    anonymous = TestClient(anonymous_app)
    assert anonymous.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json=readiness_body,
    ).status_code == 401
    assert db.get(MaintenanceProjectWorkbookState, project.project_id) is None


def test_legacy_site_issue_create_uses_dedicated_action_not_roundtrip_apply(db):
    project = _project(db, project_id="project-site-issue-create-action")
    part = DimPart(pn_std="PN-SITE-ISSUE-CREATE-ACTION")
    db.add(part)
    db.commit()
    path = f"/api/maintenance/projects/stable/{project.project_id}/site-issues"
    body = {
        "issue_no": "ISSUE-CREATE-ACTION-DENIED",
        "issue_date": "2026-08-01",
        "raw_status": "synthetic-confirmed",
        "status_mapping_state": "mapped",
        "normalized_status": "confirmed",
        "status_mapping_version": "synthetic-map-v1",
        "lines": [
            {
                "issue_line_id": "issue-line-create-action-denied",
                "line_no": 1,
                "part_id": part.id,
                "pn": part.pn_std,
                "quantity": "1",
            }
        ],
        "reason": "固定工作簿权限不得代替现场领用专用权限",
    }
    roundtrip_only = _permission_client(
        db,
        username="site_issue_create_roundtrip_only",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "action_maintenance_roundtrip_apply": True,
            "action_maintenance_site_issue_manage": False,
        },
    )

    denied = roundtrip_only.post(path, json=body)

    assert denied.status_code == 403, denied.text
    assert db.scalar(
        select(MaintenanceSiteIssue).where(
            MaintenanceSiteIssue.project_id == project.project_id
        )
    ) is None

    site_issue_only = _permission_client(
        db,
        username="site_issue_create_dedicated_action",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "action_maintenance_roundtrip_apply": False,
            "action_maintenance_site_issue_manage": True,
        },
    )
    allowed = site_issue_only.post(
        path,
        json={
            **body,
            "issue_no": "ISSUE-CREATE-ACTION-ALLOWED",
            "lines": [
                {
                    **body["lines"][0],
                    "issue_line_id": "issue-line-create-action-allowed",
                }
            ],
            "reason": "专用权限保留 legacy 客户端兼容",
        },
    )

    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["normalized_status"] == "confirmed"


def test_legacy_site_issue_status_uses_dedicated_action_for_confirm_and_void(db):
    project = _project(db, project_id="project-site-issue-status-action")
    part = DimPart(pn_std="PN-SITE-ISSUE-STATUS-ACTION")
    db.add(part)
    db.commit()
    issue = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-STATUS-ACTION",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-pending",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-status-action",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                }
            ],
            "reason": "建立专用权限状态迁移测试",
        },
    )
    path = (
        "/api/maintenance/projects/stable/site-issues/"
        f"{issue['issue_id']}/status"
    )
    roundtrip_only = _permission_client(
        db,
        username="site_issue_status_roundtrip_only",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "action_maintenance_roundtrip_apply": True,
            "action_maintenance_site_issue_manage": False,
        },
    )
    site_issue_only = _permission_client(
        db,
        username="site_issue_status_dedicated_action",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "action_maintenance_roundtrip_apply": False,
            "action_maintenance_site_issue_manage": True,
        },
    )
    confirm_body = {
        "version": 1,
        "raw_status": "synthetic-confirmed",
        "normalized_status": "confirmed",
        "status_mapping_version": "synthetic-map-v2",
        "reason": "固定工作簿权限不能确认现场领用",
    }

    denied_confirm = roundtrip_only.patch(path, json=confirm_body)

    assert denied_confirm.status_code == 403, denied_confirm.text
    db.expire_all()
    assert db.get(MaintenanceSiteIssue, issue["issue_id"]).normalized_status == "unknown"

    allowed_confirm = site_issue_only.patch(
        path,
        json={**confirm_body, "reason": "专用权限确认 legacy 现场领用"},
    )
    assert allowed_confirm.status_code == 200, allowed_confirm.text
    assert allowed_confirm.json()["normalized_status"] == "confirmed"

    void_body = {
        "version": allowed_confirm.json()["version"],
        "raw_status": "synthetic-void",
        "normalized_status": "void",
        "status_mapping_version": "synthetic-map-v3",
        "reason": "固定工作簿权限不能作废现场领用",
    }
    denied_void = roundtrip_only.patch(path, json=void_body)

    assert denied_void.status_code == 403, denied_void.text
    db.expire_all()
    assert db.get(MaintenanceSiteIssue, issue["issue_id"]).normalized_status == "confirmed"

    allowed_void = site_issue_only.patch(
        path,
        json={**void_body, "reason": "专用权限作废 legacy 现场领用"},
    )
    assert allowed_void.status_code == 200, allowed_void.text
    assert allowed_void.json()["normalized_status"] == "void"


def test_production_legacy_site_issue_writes_fail_closed_without_canonical_source(
    db,
    monkeypatch,
):
    project = _project(db, project_id="project-site-issue-prod-legacy-gate")
    part = DimPart(pn_std="PN-SITE-ISSUE-PROD-LEGACY-GATE")
    db.add(part)
    db.commit()
    pending = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-PROD-LEGACY-PENDING",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-pending",
            "status_mapping_state": "unmapped",
            "normalized_status": "unknown",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-prod-legacy-pending",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                }
            ],
            "reason": "建立生产 legacy 闸门测试前置事实",
        },
    )
    client = _client(db, username="site_issue_prod_legacy_gate_admin")
    monkeypatch.setattr(
        operations_service,
        "_site_issue_is_production_blocked",
        lambda: True,
    )

    direct_confirm = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/site-issues",
        json={
            "issue_no": "ISSUE-PROD-LEGACY-DIRECT",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-prod-legacy-direct",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                }
            ],
            "reason": "生产不能绕过仓库发货稳定身份直接确认",
        },
    )
    legacy_status_confirm = client.patch(
        "/api/maintenance/projects/stable/site-issues/"
        f"{pending['issue_id']}/status",
        json={
            "version": pending["version"],
            "raw_status": "synthetic-confirmed",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v2",
            "reason": "生产不能通过 legacy 状态入口确认",
        },
    )

    assert direct_confirm.status_code == 400, direct_confirm.text
    assert "仓库发货" in direct_confirm.json()["detail"]
    assert legacy_status_confirm.status_code == 400, legacy_status_confirm.text
    assert "仓库发货" in legacy_status_confirm.json()["detail"]
    db.expire_all()
    assert db.get(MaintenanceSiteIssue, pending["issue_id"]).normalized_status == "unknown"
    assert db.scalar(
        select(MaintenanceSiteIssue).where(
            MaintenanceSiteIssue.issue_no == "ISSUE-PROD-LEGACY-DIRECT"
        )
    ) is None


def test_production_legacy_site_issue_void_remains_available_for_fact_retirement(
    db,
    monkeypatch,
):
    project = _project(db, project_id="project-site-issue-prod-legacy-void")
    part = DimPart(pn_std="PN-SITE-ISSUE-PROD-LEGACY-VOID")
    db.add(part)
    db.commit()
    confirmed = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-PROD-LEGACY-VOID",
            "issue_date": "2026-08-01",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [
                {
                    "issue_line_id": "issue-line-prod-legacy-void",
                    "line_no": 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                }
            ],
            "reason": "建立需要在生产退役的历史现场领用事实",
        },
    )
    client = _client(db, username="site_issue_prod_legacy_void_admin")
    monkeypatch.setattr(
        operations_service,
        "_site_issue_is_production_blocked",
        lambda: True,
    )

    retired = client.patch(
        "/api/maintenance/projects/stable/site-issues/"
        f"{confirmed['issue_id']}/status",
        json={
            "version": confirmed["version"],
            "raw_status": "synthetic-void",
            "normalized_status": "void",
            "status_mapping_version": "synthetic-map-v2",
            "reason": "保留作废历史错误事实的兼容能力",
        },
    )

    assert retired.status_code == 200, retired.text
    assert retired.json()["normalized_status"] == "void"


def test_cost_thresholds_and_generated_tasks_are_deterministic(db):
    projects = [
        _project(db, project_id="project-threshold-80"),
        _project(db, project_id="project-threshold-100"),
        _project(db, project_id="project-threshold-101"),
    ]
    client = _client(db, username="threshold_task_admin")

    threshold_cases = (
        ("113.00", "80.00"),   # inc-tax 90.40 / 113.00 = 80.00%
        ("113.00", "100.00"),  # inc-tax 113.00 / 113.00 = 100.00%
        ("200.00", "177.00"),  # inc-tax 200.01 / 200.00 = 100.01%
    )
    for project, (contract_amount, amount) in zip(
        projects, threshold_cases, strict=True
    ):
        contract = client.post(
            f"/api/maintenance/projects/stable/{project.project_id}/contracts",
            json={
                "contract_id": f"contract-{project.project_id}",
                "contract_no": f"XS-{project.project_id}",
                "contract_amount": contract_amount,
                "contract_status": "synthetic-active",
                "status_mapping_state": "mapped",
                "status_mapping_version": "synthetic-map-v1",
                "included_in_total": True,
                "effective_from": "2026-01-01",
                "source": "synthetic-test",
                "reason": "建立阈值测试合同",
            },
        ).json()
        response = client.post(
            f"/api/maintenance/projects/stable/{project.project_id}/expenses",
            json={
                "expense_id": f"expense-{project.project_id}",
                "project_contract_id": contract["project_contract_id"],
                "expense_ref": f"BX-{project.project_id}",
                "expense_date": "2026-07-10",
                "amount_ex_tax": amount,
                "raw_status": "synthetic-finished",
                "status_mapping_state": "mapped",
                "normalized_status": "approved",
                "status_mapping_version": "synthetic-expense-map-v1",
                "reason": "导入阈值测试报销",
            },
        )
        assert response.status_code == 201, response.text
        ready = client.put(
            f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
            json={
                "ready_through": "2026-07-01",
                "reason": "确认阈值测试月份报销数据完整",
            },
        )
        assert ready.status_code == 200, ready.text

    statuses = []
    for project in projects:
        workspace = client.get(
            f"/api/maintenance/projects/stable/{project.project_id}/workspace",
            params={"as_of": "2026-07-31"},
        ).json()
        statuses.append(workspace["project"]["metrics"]["cost_status"])
    assert statuses == ["normal", "yellow", "red"]

    exact_eighty_tasks = client.get(
        f"/api/maintenance/projects/stable/{projects[0].project_id}/tasks",
        params={"as_of": "2026-07-31"},
    )
    assert exact_eighty_tasks.status_code == 200, exact_eighty_tasks.text
    assert not any(
        row["rule_key"].startswith("cost_ratio:")
        for row in exact_eighty_tasks.json()["rows"]
    )

    first = client.get(
        f"/api/maintenance/projects/stable/{projects[2].project_id}/tasks",
        params={"as_of": "2026-07-31"},
    )
    second = client.get(
        f"/api/maintenance/projects/stable/{projects[2].project_id}/tasks",
        params={"as_of": "2026-07-31"},
    )
    assert first.status_code == 200, first.text
    assert first.json() == second.json()
    assert "cost_ratio:red" in {row["rule_key"] for row in first.json()["rows"]}
    monthly = next(
        row for row in first.json()["rows"] if row["rule_key"] == "manager_update:2026-07"
    )
    assert monthly["task_type"] == "项目经理月度更新"
    assert monthly["due_date"] == "2026-07-31"
    assert monthly["status"] == "pending"

    state = db.get(MaintenanceProjectWorkbookState, projects[2].project_id)
    state.last_applied_at = datetime(2026, 7, 20, 8, tzinfo=UTC)
    db.commit()
    after_legacy_v2_apply = client.get(
        f"/api/maintenance/projects/stable/{projects[2].project_id}/tasks",
        params={"as_of": "2026-07-31"},
    ).json()
    monthly_after_legacy_v2_apply = next(
        row
        for row in after_legacy_v2_apply["rows"]
        if row["rule_key"] == "manager_update:2026-07"
    )
    assert monthly_after_legacy_v2_apply["status"] == "pending"
    assert client.post(
        f"/api/maintenance/projects/stable/{projects[2].project_id}/tasks",
        json={"title": "禁止用户创建"},
    ).status_code == 405

    operations = client.get(
        "/api/maintenance/projects/stable/operations",
        params={"as_of": "2026-07-31"},
    )
    assert operations.status_code == 200, operations.text
    assert operations.json()["total"] == 3
    assert operations.json()["page"] == 1
    assert operations.json()["page_size"] == 24
    assert operations.json()["rows"][0]["as_of"] == "2026-07-31"

    filtered = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "as_of": "2026-07-31",
            "q": "XS-project-threshold-101",
            "lifecycle": "missing",
            "reminder": "cost_ratio:red",
            "page": 1,
            "page_size": 1,
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert filtered.json()["rows"][0]["project_id"] == "project-threshold-101"


def test_directory_reminder_filters_use_the_same_rounded_cost_threshold_as_cards(db):
    client = _client(db, username="rounded_threshold_admin")
    cases = [
        ("rounded-up-to-80", "300.00", "212.38", "normal"),
        ("above-rounded-80", "300.00", "212.41", "yellow"),
        ("rounded-down-to-100", "300.00", "265.49", "yellow"),
        ("half-cent-to-red", "20000.00", "17700.00", "red"),
    ]
    for suffix, contract_amount, cost_amount, _expected in cases:
        project = _project(db, project_id=f"project-{suffix}")
        contract = client.post(
            f"/api/maintenance/projects/stable/{project.project_id}/contracts",
            json={
                "contract_id": f"contract-{suffix}",
                "contract_no": f"XS-{suffix}",
                "contract_amount": contract_amount,
                "contract_status": "synthetic-active",
                "status_mapping_state": "mapped",
                "status_mapping_version": "synthetic-map-v1",
                "included_in_total": True,
                "effective_from": "2026-01-01",
                "source": "synthetic-test",
                "reason": "建立两位小数预警边界合同",
            },
        ).json()
        response = client.post(
            f"/api/maintenance/projects/stable/{project.project_id}/expenses",
            json={
                "expense_id": f"expense-{suffix}",
                "project_contract_id": contract["project_contract_id"],
                "expense_ref": f"BX-{suffix}",
                "expense_date": "2026-07-10",
                "amount_ex_tax": cost_amount,
                "raw_status": "synthetic-finished",
                "status_mapping_state": "mapped",
                "normalized_status": "approved",
                "status_mapping_version": "synthetic-expense-map-v1",
                "reason": "导入两位小数预警边界报销",
            },
        )
        assert response.status_code == 201, response.text
        if suffix == "half-cent-to-red":
            collection = client.post(
                f"/api/maintenance/projects/stable/{project.project_id}/collections",
                json={
                    "project_contract_id": contract["project_contract_id"],
                    "report_month": "2026-07-01",
                    "cumulative_amount": "20001.00",
                    "status": "confirmed",
                    "reason": "验证回款进度 HALF_UP 半分边界",
                },
            )
            assert collection.status_code == 201, collection.text
        ready = client.put(
            f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
            json={
                "ready_through": "2026-07-01",
                "reason": "确认两位小数边界月份报销完整",
            },
        )
        assert ready.status_code == 200, ready.text

    directory = client.get(
        "/api/maintenance/projects/stable/operations",
        params={"as_of": "2026-07-31", "page_size": 10},
    )
    assert directory.status_code == 200, directory.text
    assert {
        row["project_id"]: row["metrics"]["cost_status"]
        for row in directory.json()["rows"]
    } == {
        f"project-{suffix}": expected
        for suffix, _contract, _cost, expected in cases
    }

    yellow = client.get(
        "/api/maintenance/projects/stable/operations",
        params={
            "as_of": "2026-07-31",
            "reminder": "cost_ratio:yellow",
            "page_size": 10,
        },
    )
    assert yellow.status_code == 200, yellow.text
    assert {row["project_id"] for row in yellow.json()["rows"]} == {
        "project-above-rounded-80",
        "project-rounded-down-to-100",
    }

    red = client.get(
        "/api/maintenance/projects/stable/operations",
        params={
            "as_of": "2026-07-31",
            "reminder": "cost_ratio:red",
            "page_size": 10,
        },
    )
    assert red.status_code == 200, red.text
    assert {row["project_id"] for row in red.json()["rows"]} == {
        "project-half-cent-to-red",
    }
    half_cent_workspace = client.get(
        "/api/maintenance/projects/stable/project-half-cent-to-red/workspace",
        params={"as_of": "2026-07-31"},
    )
    assert half_cent_workspace.status_code == 200, half_cent_workspace.text
    assert (
        half_cent_workspace.json()["project"]["metrics"]["collection_progress_pct"]
        == "100.01"
    )


def test_operations_directory_queries_do_not_scale_with_off_page_projects(db):
    client = _client(db, username="directory_query_admin")
    db.add_all(
        MaintenanceProject(
            project_id=f"directory-query-{index:03d}",
            project_code=f"DIRECTORY-{index:03d}",
            display_name=f"目录查询项目 {index:03d}",
            lifecycle_status="ongoing",
        )
        for index in range(2)
    )
    db.commit()

    params = {
        "as_of": "2026-08-31",
        "lifecycle": "ongoing",
        "page": 1,
        "page_size": 1,
    }
    first, baseline_queries = _count_endpoint_queries(db, client, params=params)
    assert first["total"] == 2
    assert len(first["rows"]) == 1

    db.add_all(
        MaintenanceProject(
            project_id=f"directory-query-{index:03d}",
            project_code=f"DIRECTORY-{index:03d}",
            display_name=f"目录查询项目 {index:03d}",
            lifecycle_status="ongoing",
        )
        for index in range(2, 32)
    )
    db.commit()

    expanded, expanded_queries = _count_endpoint_queries(db, client, params=params)
    assert expanded["total"] == 32
    assert expanded["rows"] == first["rows"]
    assert expanded_queries <= baseline_queries + 1


def test_operations_reminder_filter_queries_do_not_load_every_project_workspace(db):
    client = _client(db, username="directory_reminder_query_admin")
    db.add_all(
        MaintenanceProject(
            project_id=f"directory-reminder-{index:03d}",
            project_code=f"REMINDER-{index:03d}",
            display_name=f"提醒目录项目 {index:03d}",
            lifecycle_status="ongoing",
        )
        for index in range(2)
    )
    db.commit()
    db.add_all(
        MaintenanceProjectContract(
            project_contract_id=f"drc-{project_index:03d}-{contract_index}",
            project_id=f"directory-reminder-{project_index:03d}",
            contract_id=f"directory-contract-{project_index:03d}-{contract_index}",
            contract_no=f"XS-DIRECTORY-{project_index:03d}-{contract_index}",
            contract_amount=Decimal("100.00"),
            contract_status="synthetic-active",
            status_mapping_state="mapped",
            status_mapping_version="synthetic-map-v1",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            source="synthetic-test",
        )
        for project_index in range(2)
        for contract_index in range(4)
    )
    db.commit()

    params = {
        "as_of": "2026-08-31",
        "lifecycle": "ongoing",
        "reminder": "all",
        "page": 1,
        "page_size": 1,
    }
    first, baseline_queries = _count_endpoint_queries(db, client, params=params)
    assert first["total"] == 2
    assert len(first["rows"]) == 1

    db.add_all(
        MaintenanceProject(
            project_id=f"directory-reminder-{index:03d}",
            project_code=f"REMINDER-{index:03d}",
            display_name=f"提醒目录项目 {index:03d}",
            lifecycle_status="ongoing",
        )
        for index in range(2, 32)
    )
    db.commit()

    db.add_all(
        MaintenanceProjectContract(
            project_contract_id=f"drc-{project_index:03d}-{contract_index}",
            project_id=f"directory-reminder-{project_index:03d}",
            contract_id=f"directory-contract-{project_index:03d}-{contract_index}",
            contract_no=f"XS-DIRECTORY-{project_index:03d}-{contract_index}",
            contract_amount=Decimal("100.00"),
            contract_status="synthetic-active",
            status_mapping_state="mapped",
            status_mapping_version="synthetic-map-v1",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            source="synthetic-test",
        )
        for project_index in range(2, 32)
        for contract_index in range(4)
    )
    db.commit()

    loaded_contract_projects: list[str] = []

    def record_loaded_contract(target, _context) -> None:
        loaded_contract_projects.append(target.project_id)

    event.listen(MaintenanceProjectContract, "load", record_loaded_contract)
    try:
        expanded, expanded_queries = _count_endpoint_queries(
            db, client, params=params
        )
    finally:
        event.remove(MaintenanceProjectContract, "load", record_loaded_contract)

    assert expanded["total"] == 32
    assert expanded["rows"][0]["project_id"] == first["rows"][0]["project_id"]
    assert expanded_queries <= baseline_queries + 1
    assert loaded_contract_projects == ["directory-reminder-000"] * 4


def test_operations_reminder_predicates_match_canonical_open_tasks(db):
    project = _project(db, project_id="directory-reminder-parity")
    client = _client(db, username="directory_reminder_parity_admin")
    params = {"as_of": "2026-08-30"}
    tasks = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/tasks",
        params=params,
    )
    assert tasks.status_code == 200, tasks.text
    open_tasks = [row for row in tasks.json()["rows"] if row["status"] != "completed"]
    selectors = {
        "all",
        *(row["rule_key"] for row in open_tasks),
        *(row["task_type"] for row in open_tasks),
        *(row["severity"] for row in open_tasks),
    }

    for selector in selectors:
        response = client.post(
            "/api/maintenance/projects/stable/operations/search",
            json={
                **params,
                "q": project.project_code,
                "lifecycle": project.lifecycle_status,
                "reminder": selector,
            },
        )
        assert response.status_code == 200, (selector, response.text)
        assert response.json()["total"] == 1, selector
        assert response.json()["rows"][0]["project_id"] == project.project_id

    unmatched = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            **params,
            "q": project.project_code,
            "lifecycle": project.lifecycle_status,
            "reminder": "cost_ratio:red",
        },
    )
    assert unmatched.status_code == 200, unmatched.text
    assert unmatched.json()["total"] == 0


def test_operations_directory_rejects_cost_ratio_filter_without_financial_permissions(db):
    _project(db, project_id="directory-filter-permission-red")
    client = _permission_client(
        db,
        username="directory_filter_permission_red",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": False,
            "data_profit": False,
        },
    )

    response = client.get(
        "/api/maintenance/projects/stable/operations",
        params={"as_of": "2026-08-31", "reminder": "cost_ratio:red"},
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "当前账号无权使用该提醒筛选"}
    assert "red" not in response.text
    assert "80" not in response.text
    assert "100" not in response.text


def test_operations_directory_rejects_cost_filter_without_cost_permission(db):
    _project(db, project_id="directory-filter-permission-cost")
    client = _permission_client(
        db,
        username="directory_filter_permission_cost",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": False,
            "data_profit": False,
        },
    )

    response = client.get(
        "/api/maintenance/projects/stable/operations",
        params={"as_of": "2026-08-31", "reminder": "cost:missing_price"},
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "当前账号无权使用该提醒筛选"}
    assert "cost" not in response.text
    assert "price" not in response.text


def test_operations_directory_rejects_collection_filter_without_profit_permission(db):
    _project(db, project_id="filter-permission-collection")
    client = _permission_client(
        db,
        username="directory_filter_permission_collection",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "data_profit": False,
        },
    )

    response = client.get(
        "/api/maintenance/projects/stable/operations",
        params={"as_of": "2026-08-31", "reminder": "collection:incomplete"},
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "当前账号无权使用该提醒筛选"}
    assert "collection" not in response.text
    assert "incomplete" not in response.text


def test_operations_directory_financial_selector_permission_matrix(db):
    project = _project(db, project_id="filter-permission-matrix")
    no_financial = _permission_client(
        db,
        username="directory_filter_no_financial",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": False,
            "data_profit": False,
        },
    )
    cost_only = _permission_client(
        db,
        username="directory_filter_cost_only",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "data_profit": False,
        },
    )
    full_financial = _permission_client(
        db,
        username="directory_filter_full_financial",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "data_profit": True,
        },
    )
    endpoint = "/api/maintenance/projects/stable/operations"
    full_financial_selectors = [
        "all",
        "info",
        "warning",
        "critical",
        "completeness",
        "collection",
        "cost_ratio",
        "completeness:missing_contract_amount",
        "completeness:expense_data_not_ready",
        "collection:missing_confirmed",
        "cost_ratio:yellow",
    ]
    cost_selectors = [
        "cost",
        "cost:missing_price",
        "cost:sales_fallback_estimate",
        "completeness:missing_consumption_cost",
        "completeness:unmapped_site_issue_status",
    ]

    for selector in full_financial_selectors:
        params = {"as_of": "2026-08-31", "reminder": selector}
        for client in (no_financial, cost_only):
            denied = client.get(endpoint, params=params)
            assert denied.status_code == 403, (selector, denied.text)
            assert denied.json() == {"detail": "当前账号无权使用该提醒筛选"}
        allowed = full_financial.get(endpoint, params=params)
        assert allowed.status_code == 200, (selector, allowed.text)

    for selector in cost_selectors:
        params = {"as_of": "2026-08-31", "reminder": selector}
        denied = no_financial.get(endpoint, params=params)
        assert denied.status_code == 403, (selector, denied.text)
        assert denied.json() == {"detail": "当前账号无权使用该提醒筛选"}
        for client in (cost_only, full_financial):
            allowed = client.get(endpoint, params=params)
            assert allowed.status_code == 200, (selector, allowed.text)

    for selector in (None, "项目经理月度更新", "manager_update:2026-08"):
        params = {
            "as_of": "2026-08-31",
            "q": project.project_code,
            "lifecycle": "missing",
        }
        if selector is not None:
            params["reminder"] = selector
        allowed = no_financial.post(f"{endpoint}/search", json=params)
        assert allowed.status_code == 200, (selector, allowed.text)


def test_operations_directory_cost_filters_remain_usable_without_profit_permission(db):
    project = _project(db, project_id="filter-cost-only-visible")
    part = DimPart(pn_std="PN-DIRECTORY-COST-FILTER")
    db.add(part)
    db.commit()
    created = _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-DIRECTORY-COST-FILTER",
            "issue_date": "2026-08-15",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-map-v1",
            "lines": [{
                "issue_line_id": "directory-cost-filter-line",
                "line_no": 1,
                "part_id": part.id,
                "pn": part.pn_std,
                "quantity": "1",
            }],
            "reason": "建立仅成本权限可见的缺价提醒",
        },
    )
    assert created["lines"][0]["cost_amount_inc_tax"] is None
    cost_only = _permission_client(
        db,
        username="directory_cost_filter_cost_only",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "data_profit": False,
        },
    )

    for selector in (
        "cost:missing_price",
        "completeness:missing_consumption_cost",
    ):
        response = cost_only.post(
            "/api/maintenance/projects/stable/operations/search",
            json={
                "as_of": "2026-08-31",
                "q": project.project_code,
                "reminder": selector,
            },
        )
        assert response.status_code == 200, (selector, response.text)
        assert response.json()["total"] == 1, selector
        assert response.json()["rows"][0]["project_id"] == project.project_id


def test_sensitive_operations_reads_are_access_logged_with_scope(db):
    project = _project(db, project_id="project-sensitive-read-audit")
    client = _client(db, username="operations_read_audit_admin")
    project_path = f"/api/maintenance/projects/stable/{project.project_id}"

    assert client.get(f"{project_path}/cost-gaps").status_code == 200
    assert client.get(
        f"{project_path}/workspace", params={"as_of": "2026-08-31"}
    ).status_code == 200
    assert client.get(
        f"{project_path}/tasks", params={"as_of": "2026-08-31"}
    ).status_code == 200
    assert client.get(
        "/api/maintenance/projects/stable/operations",
        params={
            "as_of": "2026-08-31",
            "lifecycle": "all",
            "page": 1,
            "page_size": 24,
        },
    ).status_code == 200

    db.expire_all()
    rows = list(
        db.scalars(
            select(SysAccessLog)
            .where(SysAccessLog.username == "operations_read_audit_admin")
            .where(
                SysAccessLog.action.in_(
                    {
                        "stable_project_cost_gaps",
                        "stable_project_workspace",
                        "stable_project_tasks",
                        "stable_project_operations",
                    }
                )
            )
            .order_by(SysAccessLog.id)
        )
    )
    assert [row.action for row in rows] == [
        "stable_project_cost_gaps",
        "stable_project_workspace",
        "stable_project_tasks",
        "stable_project_operations",
    ]
    assert rows[0].detail == {
        "project_id": project.project_id,
        "page": 1,
        "page_size": 20,
        "total": 0,
    }
    assert rows[1].detail["project_id"] == project.project_id
    assert rows[1].detail["as_of"] == "2026-08-31"
    assert rows[3].detail == {
        "as_of": "2026-08-31",
        "searched": False,
        "lifecycle": "all",
        "reminder": None,
        "include_inactive": False,
        "page": 1,
        "page_size": 24,
    }


def test_operations_search_access_log_excludes_query_content_and_derivatives(db):
    project = _project(db, project_id="project-search-log-privacy")
    client = _client(db, username="operations_search_log_privacy_admin")
    contract_no = "XS-隐私合同-2026"
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-search-log-privacy",
            "contract_no": contract_no,
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立访问日志搜索隐私回归合同",
        },
    )
    assert contract.status_code == 201, contract.text

    sensitive_queries = [
        project.project_code,
        contract_no,
        project.display_name,
    ]
    for query in sensitive_queries:
        response = client.post(
            "/api/maintenance/projects/stable/operations/search",
            json={
                "as_of": "2026-08-31",
                "q": query,
                "lifecycle": "all",
                "page": 1,
                "page_size": 24,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1

    db.expire_all()
    rows = list(
        db.scalars(
            select(SysAccessLog)
            .where(
                SysAccessLog.username
                == "operations_search_log_privacy_admin",
                SysAccessLog.action == "stable_project_operations",
            )
            .order_by(SysAccessLog.id)
        )
    )
    assert len(rows) == len(sensitive_queries)
    expected_detail = {
        "as_of": "2026-08-31",
        "searched": True,
        "lifecycle": "all",
        "reminder": None,
        "include_inactive": False,
        "page": 1,
        "page_size": 24,
    }
    for row, query in zip(rows, sensitive_queries, strict=True):
        assert row.resource == "maintenance"
        assert row.detail == expected_detail
        serialized_log = json.dumps(
            {
                "username": row.username,
                "role": row.role,
                "action": row.action,
                "resource": row.resource,
                "detail": row.detail,
                "ip_address": row.ip_address,
                "user_agent": row.user_agent,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        forbidden_derivatives = {
            query,
            hashlib.sha256(query.encode()).hexdigest(),
            query.encode().hex(),
        }
        assert all(value not in serialized_log for value in forbidden_derivatives)


@pytest.mark.parametrize(
    "search",
    [
        "GET-URL-中不得出现的项目搜索词",
        "GET-URL-LONG-PRIVATE-SENTINEL-" + "x" * 512,
    ],
)
def test_operations_directory_rejects_get_query_search_without_audit(db, search):
    client = _client(db, username="operations_get_query_admin")

    response = client.get(
        "/api/maintenance/projects/stable/operations",
        params={"as_of": "2026-08-31", "q": search},
    )

    assert response.status_code == 422
    assert search not in response.text
    db.expire_all()
    assert (
        db.scalar(
            select(SysAccessLog.id).where(
                SysAccessLog.username == "operations_get_query_admin",
                SysAccessLog.action == "stable_project_operations",
            )
        )
        is None
    )


def test_operations_search_rejects_overlong_values_without_reflection_or_audit(db):
    client = _client(db, username="operations_overlong_search_admin")
    q_sentinel = "POST-Q-LONG-PRIVATE-SENTINEL-" + "x" * 512
    reminder_sentinel = "REMINDER-LONG-PRIVATE-SENTINEL-" + "x" * 128

    responses = [
        client.get(
            "/api/maintenance/projects/stable/operations",
            params={"as_of": "2026-08-31", "reminder": reminder_sentinel},
        ),
        client.post(
            "/api/maintenance/projects/stable/operations/search",
            json={
                "as_of": "2026-08-31",
                "q": "safe-search",
                "reminder": reminder_sentinel,
            },
        ),
        client.post(
            "/api/maintenance/projects/stable/operations/search",
            json={"as_of": "2026-08-31", "q": q_sentinel},
        ),
    ]

    for response in responses:
        assert response.status_code == 422
        assert "PRIVATE-SENTINEL" not in response.text
        assert q_sentinel not in response.text
        assert reminder_sentinel not in response.text

    db.expire_all()
    assert (
        db.scalar(
            select(SysAccessLog.id).where(
                SysAccessLog.username == "operations_overlong_search_admin",
                SysAccessLog.action == "stable_project_operations",
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "selector",
    [
        "cost:not-a-rule",
        "completeness:not-a-rule",
        "manager_update:2026-13",
        "totally-unknown",
    ],
)
def test_operations_directory_rejects_unknown_reminder_without_audit(db, selector):
    client = _client(db, username="operations_unknown_reminder_admin")

    response = client.get(
        "/api/maintenance/projects/stable/operations",
        params={"as_of": "2026-08-31", "reminder": selector},
    )

    assert response.status_code == 422
    assert selector not in response.text
    db.expire_all()
    assert (
        db.scalar(
            select(SysAccessLog.id).where(
                SysAccessLog.username == "operations_unknown_reminder_admin",
                SysAccessLog.action == "stable_project_operations",
            )
        )
        is None
    )


def test_operations_directory_accepts_declared_reminder_selectors(db):
    client = _client(db, username="operations_known_reminder_admin")
    selectors = [
        "项目经理月度更新",
        "manager_update:2026-08",
        "all",
        "info",
        "warning",
        "critical",
        "completeness",
        "collection",
        "cost",
        "cost_ratio",
        "completeness:no_effective_contracts",
        "completeness:duplicate_effective_contract",
        "completeness:unmapped_contract_status",
        "completeness:missing_contract_amount",
        "completeness:cross_project_contract_conflict",
        "completeness:missing_consumption_cost",
        "completeness:unmapped_site_issue_status",
        "completeness:unmapped_expense_status",
        "completeness:expense_data_not_ready",
        "completeness:expense_readiness_in_future",
        "collection:missing_confirmed",
        "collection:incomplete",
        "cost:missing_price",
        "cost:sales_fallback_estimate",
        "cost_ratio:yellow",
        "cost_ratio:red",
    ]

    for selector in selectors:
        response = client.get(
            "/api/maintenance/projects/stable/operations",
            params={"as_of": "2026-08-31", "reminder": selector},
        )
        assert response.status_code == 200, (selector, response.text)


def test_operations_directory_query_count_is_constant_across_page_sizes(db):
    client = _client(db, username="directory_page_scale_admin")
    db.add_all(
        MaintenanceProject(
            project_id=f"directory-page-scale-{index:03d}",
            project_code=f"PAGE-SCALE-{index:03d}",
            display_name=f"目录分页项目 {index:03d}",
            lifecycle_status="ongoing",
        )
        for index in range(200)
    )
    db.commit()
    base_params = {
        "as_of": "2026-08-31",
        "lifecycle": "ongoing",
        "page": 1,
    }

    one, one_queries = _count_endpoint_queries(
        db, client, params={**base_params, "page_size": 1}
    )
    twenty_four, twenty_four_queries = _count_endpoint_queries(
        db, client, params={**base_params, "page_size": 24}
    )
    two_hundred, two_hundred_queries = _count_endpoint_queries(
        db, client, params={**base_params, "page_size": 200}
    )

    assert len(one["rows"]) == 1
    assert len(twenty_four["rows"]) == 24
    assert len(two_hundred["rows"]) == 200
    assert twenty_four_queries <= one_queries + 1
    assert two_hundred_queries <= one_queries + 1


def test_operations_directory_card_matches_workspace_summary(db):
    project = _project(db, project_id="project-directory-card-parity")
    project.lifecycle_status = "ongoing"
    db.commit()
    client = _client(db, username="directory_card_parity_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-directory-parity",
            "contract_no": "XS-DIRECTORY-PARITY",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立目录卡片口径测试合同",
        },
    ).json()
    collection = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/collections",
        json={
            "project_contract_id": contract["project_contract_id"],
            "report_month": "2026-08-01",
            "cumulative_amount": "350.00",
            "status": "confirmed",
            "reason": "确认目录卡片测试回款",
        },
    )
    assert collection.status_code == 201, collection.text
    expense = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses",
        json={
            "expense_id": "expense-directory-parity",
            "expense_ref": "BX-DIRECTORY-PARITY",
            "expense_date": "2026-08-10",
            "amount_ex_tax": "125.00",
            "raw_status": "synthetic-approved",
            "status_mapping_state": "mapped",
            "normalized_status": "approved",
            "status_mapping_version": "synthetic-expense-map-v1",
            "reason": "导入目录卡片测试报销",
        },
    )
    assert expense.status_code == 201, expense.text
    ready = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-08-01",
            "reason": "确认目录卡片测试报销完整",
        },
    )
    assert ready.status_code == 200, ready.text

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-08-31"},
    )
    directory = client.post(
        "/api/maintenance/projects/stable/operations/search",
        json={
            "as_of": "2026-08-31",
            "q": project.project_code,
            "lifecycle": "ongoing",
        },
    )

    assert workspace.status_code == 200, workspace.text
    assert directory.status_code == 200, directory.text
    assert directory.json()["rows"] == [workspace.json()["project"]]


def test_workspace_details_are_independently_paged_without_truncating_totals_or_workbook(db):
    project = _project(db, project_id="project-workspace-server-pages")
    contract = MaintenanceProjectContract(
        project_contract_id="pc-workspace-server-pages",
        project_id=project.project_id,
        contract_id="contract-workspace-server-pages",
        contract_no="XS-WORKSPACE-PAGES",
        contract_amount=Decimal("1000.00"),
        contract_status="synthetic-active",
        status_mapping_state="mapped",
        status_mapping_version="synthetic-map-v1",
        included_in_total=True,
        effective_from=date(2023, 1, 1),
        source="synthetic-test",
    )
    part = DimPart(pn_std="PN-WORKSPACE-PAGES", description="分页仍展示的备件")
    db.add_all([contract, part])
    db.flush()

    confirmed_issue = MaintenanceSiteIssue(
        issue_id="issue-workspace-confirmed",
        project_id=project.project_id,
        issue_no="WBDD-WORKSPACE-CONFIRMED",
        issue_date=date(2026, 8, 1),
        raw_status="synthetic-confirmed",
        status_mapping_state="mapped",
        normalized_status="confirmed",
        status_mapping_version="synthetic-map-v1",
    )
    void_issue = MaintenanceSiteIssue(
        issue_id="issue-workspace-void",
        project_id=project.project_id,
        issue_no="WBDD-WORKSPACE-VOID",
        issue_date=date(2026, 8, 2),
        raw_status="synthetic-void",
        status_mapping_state="mapped",
        normalized_status="void",
        status_mapping_version="synthetic-map-v1",
    )
    db.add_all([confirmed_issue, void_issue])
    db.flush()
    for line_no in range(1, 46):
        has_cost = line_no % 2 == 0
        db.add(
            MaintenanceSiteIssueLine(
                issue_line_id=f"workspace-confirmed-{line_no:03d}",
                issue_id=confirmed_issue.issue_id,
                line_no=line_no,
                part_id=part.id,
                pn=part.pn_std,
                quantity=Decimal("1.000"),
                unit_cost=Decimal("10.00") if has_cost else None,
                cost_amount=Decimal("10.00") if has_cost else None,
                unit_cost_ex_tax=Decimal("10.00") if has_cost else None,
                unit_cost_inc_tax=Decimal("11.30") if has_cost else None,
                cost_amount_ex_tax=Decimal("10.00") if has_cost else None,
                cost_amount_inc_tax=Decimal("11.30") if has_cost else None,
                cost_source="direct_purchase" if has_cost else None,
                algorithm_version="synthetic-v1",
            )
        )
    for line_no in range(1, 6):
        db.add(
            MaintenanceSiteIssueLine(
                issue_line_id=f"workspace-void-{line_no:03d}",
                issue_id=void_issue.issue_id,
                line_no=line_no,
                part_id=part.id,
                pn=part.pn_std,
                quantity=Decimal("1.000"),
                algorithm_version="synthetic-v1",
            )
        )
    for offset in range(25):
        report_month = date(2023 + offset // 12, offset % 12 + 1, 1)
        db.add(
            MaintenanceCollectionSnapshot(
                collection_id=f"workspace-collection-{offset + 1:03d}",
                project_id=project.project_id,
                project_contract_id=contract.project_contract_id,
                report_month=report_month,
                cumulative_amount=Decimal(offset + 1) * Decimal("10.00"),
                status="confirmed",
            )
        )
    for row_no in range(1, 51):
        approved = row_no <= 45
        db.add(
            MaintenanceProjectExpenseAttribution(
                expense_id=f"workspace-expense-{row_no:03d}",
                project_id=project.project_id,
                project_contract_id=contract.project_contract_id,
                expense_ref=f"BXD-WORKSPACE-{row_no:03d}",
                expense_date=date(2026, 8, 3),
                amount_ex_tax=Decimal("2.00"),
                amount_inc_tax=Decimal("2.26"),
                raw_status="synthetic-approved" if approved else "synthetic-rejected",
                status_mapping_state="mapped",
                normalized_status="approved" if approved else "rejected",
                status_mapping_version="synthetic-map-v1",
            )
        )
    db.commit()
    client = _client(db, username="workspace_server_pages_admin")
    path = f"/api/maintenance/projects/stable/{project.project_id}/workspace"
    params = {
        "as_of": "2026-12-31",
        "collection_page": 2,
        "collection_page_size": 10,
        "requisition_page": 3,
        "requisition_page_size": 10,
        "expense_page": 4,
        "expense_page_size": 10,
    }

    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    workspace = response.json()
    assert workspace["collection_snapshots"]["total"] == 25
    assert workspace["collection_snapshots"]["page"] == 2
    assert workspace["collection_snapshots"]["page_size"] == 10
    assert [row["collection_id"] for row in workspace["collection_snapshots"]["rows"]] == [
        f"workspace-collection-{row_no:03d}" for row_no in range(11, 21)
    ]
    assert workspace["requisitions"]["total"] == 50
    assert workspace["requisitions"]["page"] == 3
    assert workspace["requisitions"]["page_size"] == 10
    assert [row["line_id"] for row in workspace["requisitions"]["rows"]] == [
        f"workspace-confirmed-{row_no:03d}" for row_no in range(21, 31)
    ]
    assert workspace["approved_expenses"]["total"] == 45
    assert workspace["approved_expenses"]["page"] == 4
    assert workspace["approved_expenses"]["page_size"] == 10
    assert [row["expense_id"] for row in workspace["approved_expenses"]["rows"]] == [
        f"workspace-expense-{row_no:03d}" for row_no in range(31, 41)
    ]
    assert workspace["approved_expenses"]["rows"][0]["amount"] == "2.26"
    assert workspace["approved_expenses"]["rows"][0]["amount_ex_tax"] == "2.00"
    assert workspace["approved_expenses"]["rows"][0]["amount_inc_tax"] == "2.26"
    assert workspace["approved_expenses"]["rows"][0]["tax_rate_used"] == "0.13"
    assert workspace["project"]["metrics"]["site_requisition_known_cost"] == "248.60"
    assert workspace["project"]["metrics"]["site_requisition_known_cost_ex_tax"] == "220.00"
    assert workspace["project"]["metrics"]["missing_cost_lines"] == 23
    assert workspace["project"]["metrics"]["approved_expense"] == "101.70"
    assert [sheet["row_count"] for sheet in workspace["workbook_preview"]["sheets"][:3]] == [
        26,
        45,
        45,
    ]

    full_workspace = operations_service.project_workbook_workspace(
        db,
        project_id=project.project_id,
        as_of=date(2026, 12, 31),
        user_ctx=UserContext(
            user_id="workspace-server-pages",
            role="admin",
            permissions=None,
        ),
    )
    assert full_workspace is not None
    assert len(full_workspace["collection_snapshots"]) == 25
    assert len(full_workspace["confirmed_site_consumptions"]) == 45
    assert len(full_workspace["approved_expenses"]) == 45

    _one_row, one_row_queries = _count_get_queries(
        db,
        client,
        path=path,
        params={
            "as_of": "2026-12-31",
            "collection_page_size": 1,
            "requisition_page_size": 1,
            "expense_page_size": 1,
        },
    )
    _all_rows, all_rows_queries = _count_get_queries(
        db,
        client,
        path=path,
        params={
            "as_of": "2026-12-31",
            "collection_page_size": 100,
            "requisition_page_size": 100,
            "expense_page_size": 100,
        },
    )
    assert all_rows_queries == one_row_queries


def test_workbook_read_transaction_does_not_block_followup_workspace_read(db):
    project = _project(db, project_id="project-workspace-read-lock-free")
    client = _client(db, username="workspace_read_lock_free_admin")
    path = f"/api/maintenance/projects/stable/{project.project_id}/workspace"

    # The workbook adapter deliberately keeps this caller-owned transaction open.
    # A read-only snapshot must not retain a project-row lock that stalls another
    # user's workspace read until this transaction happens to end.
    snapshot = operations_service.project_workbook_workspace(
        db,
        project_id=project.project_id,
        as_of=date(2026, 8, 10),
        user_ctx=UserContext(
            user_id="workspace-read-lock-free",
            role="admin",
            permissions=None,
        ),
    )
    assert snapshot is not None

    timed_out = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.get,
            path,
            params={"as_of": "2026-08-10"},
        )
        try:
            response = future.result(timeout=1)
        except FutureTimeoutError:
            timed_out = True
            # Always release the old implementation's lock so a red test fails
            # promptly instead of hanging the whole suite.
            db.rollback()
            response = future.result(timeout=2)
        finally:
            db.rollback()

    assert not timed_out, "只读工作簿事务持有了项目行锁，阻塞了后续只读请求"
    assert response.status_code == 200, response.text


def test_cost_gap_list_query_count_does_not_scale_with_page_size(db):
    project = _project(db, project_id="project-cost-gap-page-scale")
    client = _client(db, username="cost_gap_page_scale_admin")
    contract = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-cost-gap-page-scale",
            "contract_no": "XS-COST-GAP-PAGE-SCALE",
            "contract_amount": "1000.00",
            "contract_status": "synthetic-active",
            "status_mapping_state": "mapped",
            "status_mapping_version": "synthetic-map-v1",
            "included_in_total": True,
            "effective_from": "2026-01-01",
            "source": "synthetic-test",
            "reason": "建立缺价分页查询测试合同",
        },
    )
    assert contract.status_code == 201, contract.text
    part = DimPart(pn_std="PN-COST-GAP-PAGE-SCALE")
    db.add(part)
    db.commit()
    _create_legacy_site_issue_fixture(
        db,
        project_id=project.project_id,
        body={
            "issue_no": "ISSUE-COST-GAP-PAGE-SCALE",
            "issue_date": "2026-08-10",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-issue-map-v1",
            "lines": [
                {
                    "issue_line_id": f"issue-line-page-scale-{index:03d}",
                    "line_no": index + 1,
                    "part_id": part.id,
                    "pn": part.pn_std,
                    "quantity": "1",
                }
                for index in range(40)
            ],
            "reason": "建立缺价分页查询测试领用行",
        },
    )
    path = f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps"

    one, one_queries = _count_get_queries(
        db, client, path=path, params={"page": 1, "page_size": 1}
    )
    forty, forty_queries = _count_get_queries(
        db, client, path=path, params={"page": 1, "page_size": 40}
    )

    assert one["total"] == forty["total"] == 40
    assert len(one["rows"]) == 1
    assert len(forty["rows"]) == 40
    assert {row["contract_no"] for row in forty["rows"]} == {
        "XS-COST-GAP-PAGE-SCALE"
    }
    assert forty_queries <= one_queries + 1


def test_site_issue_cost_backfill_endpoint_smoke(db):
    """/site-issue-costs/backfill 端点级冒烟。

    该端点曾因引用未定义的 operations_service 全线 500（生产 2026-08-24
    实测，两个回填端点同病），服务层正确性另由 resolve_lines 套件覆盖——
    这里钉住「端点本身可达且返回统计结构」。
    """
    client = _client(db)
    resp = client.post("/api/maintenance/projects/stable/site-issue-costs/backfill")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"total", "resolved", "still_unknown", "projects_touched"} <= set(body)


def test_expense_attribution_backfill_skips_existing_ref_across_runs(db):
    """2026-08-25 生产回归：同单同行的报销事实以不同 raw_line_id 重复入库
    （批次 168/175 存量形态）——第二轮回填必须按库里已有 (project, ref)
    跳过，而不是撞 uq_maintenance_project_expense_ref。"""
    client = _client(db)
    project = MaintenanceProject(project_id="attr-dedup-project",
                                 project_code="ATTR-DEDUP",
                                 display_name="归因去重项目",
                                 lifecycle_status="ongoing")
    db.add(project)
    db.flush()
    db.add(MaintenanceProjectContract(
        project_contract_id="attr-dedup-contract", project_id=project.project_id,
        contract_id="XSDD-ATTR-1", contract_no="XSDD-ATTR-1",
        status_mapping_state="mapped", status_mapping_version="t",
        effective_from=date(2026, 1, 1), source="ledger", version=1))
    from app.models.maintenance import FProjectExpense
    batch_ids = []
    for i in range(2):
        b = SysImportBatch(filename=f"attr-dedup-{i}.xlsx", file_type="expense",
                           file_hash=uuid.uuid4().hex.ljust(64, "0"), status="success")
        db.add(b)
        db.flush()
        batch_ids.append(b.id)
    for i, raw in enumerate(("old-raw", "new-raw")):
        db.add(FProjectExpense(
            raw_line_id=f"attr-dedup-{raw}", bxd_no="BXD-ATTR-0001", line_no=1,
            expense_date=date(2026, 8, 1), person="测试", amount=Decimal("100"),
            amount_ex_tax=Decimal("100"), amount_inc_tax=Decimal("113.00"),
            data_status="已结束", linked_sales_order_no="XSDD-ATTR-1",
            import_batch_id=batch_ids[i]))
    db.commit()

    first = client.post("/api/maintenance/projects/stable/expense-attribution/backfill")
    assert first.status_code == 200, first.text
    assert first.json()["attributed"] == 1
    assert first.json()["skipped_duplicate"] >= 1

    # 第二轮回填（库里已有同 ref 归因、事实行换 raw_line_id 形态）：跳过不撞键
    second = client.post("/api/maintenance/projects/stable/expense-attribution/backfill")
    assert second.status_code == 200, second.text
    assert second.json()["attributed"] == 0
