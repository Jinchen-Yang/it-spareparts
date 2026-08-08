"""Stable maintenance-project operating facts through their public API."""

from datetime import UTC, date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import auth
from app.auth import hash_password
from app.api import maintenance_project_operations
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
)
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysUser
from app.models.system import SysImportBatch
from app.security import UserContext
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
    filtered = client.get(
        "/api/maintenance/projects/stable/operations",
        params={
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

    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/site-issues",
        json={
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
    assert created.status_code == 201, created.text
    lines = {row["issue_line_id"]: row for row in created.json()["lines"]}
    assert lines["issue-line-direct"]["cost_source"] == "direct_purchase"
    assert lines["issue-line-direct"]["cost_amount"] == "19.46"
    assert lines["issue-line-direct"]["price_basis"] == "ex_tax"
    assert lines["issue-line-direct"]["reference_samples"][0]["tax_conversion"] == "divide_1.13"
    assert lines["issue-line-window"]["cost_source"] == "purchase_window"
    assert lines["issue-line-window"]["unit_cost"] == "26.00"
    assert lines["issue-line-window"]["reference_sample_count"] == 2

    workspace = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-05-31"},
    ).json()
    metrics = workspace["project"]["metrics"]
    assert metrics["site_requisition_known_cost"] == "71.46"
    assert metrics["actual_project_cost_known"] == "71.46"
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


def test_sales_fallback_is_ex_tax_and_manual_fill_only_resolves_a_gap(db):
    project = _project(db, project_id="project-sales-manual-gap")
    client = _client(db, username="sales_manual_gap_admin")
    client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/contracts",
        json={
            "contract_id": "contract-sales-gap-001",
            "contract_no": "XS-SALES-GAP-001",
            "contract_amount": "1000.00",
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

    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/site-issues",
        json={
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
    assert created.status_code == 201, created.text
    lines = {row["issue_line_id"]: row for row in created.json()["lines"]}
    assert lines["issue-line-sales-fallback"]["cost_source"] == "sales_window"
    assert lines["issue-line-sales-fallback"]["unit_cost"] == "133.33"
    assert lines["issue-line-sales-fallback"]["cost_amount"] == "399.99"
    assert all(
        sample["tax_conversion"] == "divide_1.13"
        for sample in lines["issue-line-sales-fallback"]["reference_samples"]
    )
    assert lines["issue-line-manual-gap"]["cost_source"] is None

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
    assert filled.json()["version"] == 2

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
    assert restricted_metrics["actual_project_cost_known"] is None
    assert restricted_metrics["cost_status"] is None
    assert restricted["requisitions"]["rows"][0]["unit_cost"] is None
    assert restricted["requisitions"]["rows"][0]["reference_samples"] == []
    assert restricted["requisitions"]["rows"][0]["cost_status"] == "restricted"
    assert not any(
        row["rule_key"].startswith(("collection:", "cost_ratio:"))
        for row in restricted["reminders"]
    )


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
    assert metrics["approved_expense"] == "50.00"
    assert metrics["actual_project_cost_known"] == "50.00"
    assert workspace["approved_expenses"]["total"] == 1
    assert workspace["approved_expenses"]["rows"][0]["contract_no"] == "XS-EXPENSE-001"
    assert workspace["approved_expenses"]["rows"][0]["category"] == "差旅费"
    assert workspace["approved_expenses"]["rows"][0]["reason"] == "项目现场支持"
    assert workspace["approved_expenses"]["rows"][0]["amount"] == "50.00"
    assert workspace["approved_expenses"]["rows"][0]["approval_status"] == "approved"
    assert workspace["completeness"]["status"] == "incomplete"
    assert {row["code"] for row in workspace["completeness"]["issues"]} >= {
        "unmapped_expense_status",
        "expense_data_not_ready",
    }


def test_expense_readiness_is_explicit_monthly_monotonic_and_audited(db):
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

    marked = client.put(
        f"/api/maintenance/projects/stable/{project.project_id}/expenses/readiness",
        json={
            "ready_through": "2026-07-01",
            "reason": "财务接口确认七月已审批报销同步完成，允许零行",
        },
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["expense_ready_through"] == "2026-07-01"
    after = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/workspace",
        params={"as_of": "2026-07-31"},
    ).json()
    assert after["project"]["metrics"]["expense_data_ready"] is True
    assert after["project"]["metrics"]["cost_complete"] is True
    assert after["project"]["metrics"]["cost_status"] == "normal"
    assert "expense_data_not_ready" not in {
        row["code"] for row in after["completeness"]["issues"]
    }
    audit = db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_type="expense_readiness",
    ).one()
    assert audit.action == "mark_ready"
    assert audit.after_json == {"expense_ready_through": "2026-07-01"}

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


def test_cost_thresholds_and_generated_tasks_are_deterministic(db):
    projects = [
        _project(db, project_id="project-threshold-80"),
        _project(db, project_id="project-threshold-100"),
        _project(db, project_id="project-threshold-101"),
    ]
    client = _client(db, username="threshold_task_admin")

    for project, amount in zip(projects, ("80.00", "100.00", "101.00"), strict=True):
        contract = client.post(
            f"/api/maintenance/projects/stable/{project.project_id}/contracts",
            json={
                "contract_id": f"contract-{project.project_id}",
                "contract_no": f"XS-{project.project_id}",
                "contract_amount": "100.00",
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
    assert statuses == ["yellow", "yellow", "red"]

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
    completed = client.get(
        f"/api/maintenance/projects/stable/{projects[2].project_id}/tasks",
        params={"as_of": "2026-07-31"},
    ).json()
    completed_monthly = next(
        row
        for row in completed["rows"]
        if row["rule_key"] == "manager_update:2026-07"
    )
    assert completed_monthly["status"] == "completed"
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

    filtered = client.get(
        "/api/maintenance/projects/stable/operations",
        params={
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

    expanded, expanded_queries = _count_endpoint_queries(db, client, params=params)
    assert expanded["total"] == 32
    assert expanded["rows"] == first["rows"]
    assert expanded_queries <= baseline_queries + 1
