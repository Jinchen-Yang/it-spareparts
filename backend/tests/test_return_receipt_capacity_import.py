"""D-31 capacity contracts through standard import HTTP jobs on real PostgreSQL."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api import maintenance_return_receipt_import as api
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment

from tests.test_maintenance_return_receipts_api import _client, _wbdd
from tests.test_return_receipt_import import workbook
from tests.test_site_issue_v2_api import _project


JOBS = "/api/maintenance/doc-imports/return-receipts/jobs"


def setup_capacity(db, *, demand_qty=10):
    project = _project(db, project_id=str(uuid4()))
    project.project_code = project.display_name = "TEST-PROJECT"
    order = _wbdd(db, project=project)
    part = DimPart(pn_std="DEMAND-PART", status="active")
    db.add(part)
    db.flush()
    db.add(FMaintenanceLine(
        raw_line_id=f"capacity-{uuid4()}", order_id=order.id,
        import_batch_id=order.import_batch_id, part_id=part.id,
        qty=Decimal(demand_qty), is_active=True,
    ))
    db.commit()
    client = _client(db, username="capacity-import-user")
    client.app.include_router(api.router, prefix="/api")
    return project, order, client


def preview(client, data):
    response = client.post(
        JOBS, headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("capacity.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 202, response.text
    job = client.get(f"{JOBS}/{response.json()['batch_id']}")
    assert job.status_code == 200, job.text
    assert job.json()["status"] == "ready", job.text
    return job.json()


def apply(client, plan, **kwargs):
    return client.post(f"{JOBS}/{plan['batch_id']}/apply", json={
        "plan_hash": plan["plan_hash"], "preview_token": plan["preview_token"],
        **kwargs,
    })


def receipts(client, project):
    response = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_import_reports_cumulative_capacity_per_row_and_applies_nothing(db):
    project, order, client = setup_capacity(db)
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "ALREADY", "qty": 9, "wbdd_no": order.order_no},
    )
    assert created.status_code == 201, created.text
    plan = preview(client, workbook(qty="1", wbdd=order.order_no, extra=[{
        "备件明细.备件PN": "SECOND", "备件明细.入库数量": "1",
        "备件明细.测试结果": "坏品", "备件明细.数据ID(不可修改)": "DETAIL-2",
        "备件明细.序号": "2",
    }]))
    assert [row["action"] for row in plan["rows"]] == ["create", "invalid"]
    error = plan["rows"][1]
    assert error["error"]["code"] == "return_capacity_exceeded"
    assert "最多还能录入 0" in error["reason"]
    assert plan["summary"]["blocking_errors"] == 1
    denied = apply(client, plan)
    assert denied.status_code == 422, denied.text
    assert denied.json()["detail"]["code"] == "blocking_errors"
    current = receipts(client, project)
    assert [(row["pn"], row["qty"]) for row in current] == [("ALREADY", "9.000")]
    audit = client.get(f"/api/maintenance/return-receipts/{current[0]['receipt_id']}/audit")
    assert audit.status_code == 200, audit.text
    assert len(audit.json()["items"]) == 1


def test_import_full_order_keeps_nonincrease_corrections_and_void_available(db):
    project, order, client = setup_capacity(db)
    plan = preview(client, workbook(qty="10", wbdd=order.order_no))
    response = apply(client, plan)
    assert response.status_code == 200, response.text
    assert receipts(client, project)[0]["qty"] == "10.000"

    # A legitimate later demand correction creates historical excess.
    line = db.scalar(select(FMaintenanceLine).where(FMaintenanceLine.order_id == order.id))
    line.qty = Decimal(8)
    db.commit()
    plan = preview(client, workbook(qty="10", wbdd=order.order_no, condition="坏品"))
    assert plan["rows"][0]["action"] == "change"
    response = apply(client, plan, confirm_changes=True, reason="核对件况")
    assert response.status_code == 200, response.text
    assert receipts(client, project)[0]["qty"] == "10.000"

    blocked = preview(client, workbook(qty="11", wbdd=order.order_no, condition="坏品"))
    assert blocked["rows"][0]["error"]["code"] == "return_capacity_exceeded"
    assert apply(client, blocked).status_code == 422
    assert receipts(client, project)[0]["qty"] == "10.000"

    decrease = preview(client, workbook(qty="9", wbdd=order.order_no, condition="坏品"))
    response = apply(client, decrease, confirm_changes=True, reason="减少实收数量")
    assert response.status_code == 200, response.text
    assert receipts(client, project)[0]["qty"] == "9.000"
    cancel = preview(client, workbook(qty="9", wbdd=order.order_no, status="已取消"))
    response = apply(client, cancel, confirm_changes=True, reason="源单作废")
    assert response.status_code == 200, response.text
    assert receipts(client, project) == []
    all_rows = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        params={"line_status": "all"},
    ).json()["items"]
    assert [(row["qty"], row["line_status"]) for row in all_rows] == [("9.000", "voided")]


def test_import_final_capacity_credits_later_reduction_before_earlier_increase(db):
    project, order, client = setup_capacity(db)

    def data(first, second):
        return workbook(qty=first, wbdd=order.order_no, extra=[{
            "备件明细.备件PN": "SECOND", "备件明细.入库数量": second,
            "备件明细.测试结果": "坏品", "备件明细.数据ID(不可修改)": "DETAIL-2",
            "备件明细.序号": "2",
        }])

    response = apply(client, preview(client, data("5", "5")))
    assert response.status_code == 200, response.text
    correction = preview(client, data("8", "2"))
    assert [row["action"] for row in correction["rows"]] == ["change", "change"]
    response = apply(client, correction, confirm_changes=True, reason="同批数量核对")
    assert response.status_code == 200, response.text
    assert {row["pn"]: row["qty"] for row in receipts(client, project)} == {
        "MEMORY": "8.000", "SECOND": "2.000",
    }


def test_import_unlinked_quantity_stays_independent_and_linking_full_order_fails(db):
    project, order, client = setup_capacity(db)
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "ALREADY", "qty": 10, "wbdd_no": order.order_no},
    )
    assert created.status_code == 201, created.text
    response = apply(client, preview(client, workbook(qty="2")))
    assert response.status_code == 200, response.text
    correction = preview(client, workbook(qty="2", wbdd=order.order_no))
    assert correction["rows"][0]["error"]["code"] == "return_capacity_exceeded"
    assert apply(client, correction, confirm_changes=True, reason="补关联").status_code == 422
    imported = next(row for row in receipts(client, project) if row["source"] == "rkd_import")
    assert imported["source_order_id"] is None
    assert imported["qty"] == "2.000"


def test_import_change_link_checks_new_target_and_preserves_both_orders(db):
    project, order, client = setup_capacity(db)
    target = _wbdd(db, project=project, order_no="WBDD-20260911-0002", demand_qty=1)
    response = apply(client, preview(client, workbook(qty="2", wbdd=order.order_no)))
    assert response.status_code == 200, response.text
    correction = preview(client, workbook(qty="2", wbdd=target.order_no))
    assert correction["rows"][0]["error"]["details"]["demand_order_id"] == target.raw_order_id
    response = apply(client, correction, confirm_changes=True, reason="改关联")
    assert response.status_code == 422, response.text
    assert [(row["source_order_id"], row["qty"], row["version"])
            for row in receipts(client, project)] == [(order.raw_order_id, "2.000", 1)]


def test_import_rechecks_capacity_when_preview_was_consumed_by_another_command(db):
    project, order, client = setup_capacity(db)
    plan = preview(client, workbook(qty="10", wbdd=order.order_no))
    assert plan["rows"][0]["action"] == "create"
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "LATER", "qty": 1, "wbdd_no": order.order_no},
    )
    assert created.status_code == 201, created.text
    response = apply(client, plan)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "stale_preview"
    assert [(row["pn"], row["qty"]) for row in receipts(client, project)] == [("LATER", "1.000")]
    retry = client.post(f"{JOBS}/{plan['batch_id']}/retry")
    assert retry.status_code == 202, retry.text
    fresh = client.get(f"{JOBS}/{plan['batch_id']}").json()
    assert fresh["status"] == "ready"
    assert fresh["rows"][0]["error"]["code"] == "return_capacity_exceeded"


@pytest.mark.parametrize("demand_qty", [0, -1])
def test_import_unknown_capacity_is_a_per_row_error_not_a_zero_baseline(db, demand_qty):
    project, order, client = setup_capacity(db, demand_qty=demand_qty)
    plan = preview(client, workbook(qty="1", wbdd=order.order_no))
    assert plan["rows"][0]["action"] == "invalid"
    assert plan["rows"][0]["error"]["code"] == "return_capacity_unknown"
    assert "基数不完整" in plan["rows"][0]["reason"]
    assert apply(client, plan).status_code == 422
    assert receipts(client, project) == []


def test_import_historical_nan_is_per_row_unknown_and_void_preserves_evidence(db):
    project, order, client = setup_capacity(db)
    source = workbook(qty="2", wbdd=order.order_no)
    response = apply(client, preview(client, source))
    assert response.status_code == 200, response.text
    row = db.scalar(select(MaintenanceRkdReturnLine))
    row.qty = Decimal("NaN")
    db.commit()

    correction = preview(client, workbook(qty="3", wbdd=order.order_no))
    assert correction["rows"][0]["action"] == "invalid"
    assert correction["rows"][0]["error"]["code"] == "return_capacity_unknown"
    assert correction["rows"][0]["error"]["details"]["R"] is None
    assert correction["rows"][0]["error"]["message"] == (
        "这条记录的历史数量无法识别，不能直接修改关联收货数量；"
        "可以在页面修改备注等其他字段，或作废后重新录入"
    )
    assert apply(client, correction, confirm_changes=True, reason="核对数量").status_code == 422
    assert receipts(client, project)[0]["qty"] == "NaN"

    # Replaying unchanged source evidence must preserve the historical row.
    replay = preview(client, source)
    assert replay["rows"][0]["action"] == "unchanged"
    assert apply(client, replay).status_code == 200
    mixed = preview(client, workbook(
        qty="2", wbdd=order.order_no, status="已取消", extra=[{
            "数据ID(不可修改)": "HEAD-NEW", "入库单号": "RKD-20260912-0002",
            "入库日期": "2026-09-12", "入库类别": "维保拆旧返件",
            "入库备件/整机": "备件", "数据状态": "已生效", "项目名称": "TEST-PROJECT",
            "维保需求单": order.order_no, "备件明细.备件PN": "NEW",
            "备件明细.入库数量": "1", "备件明细.测试结果": "成品",
            "备件明细.数据ID(不可修改)": "NEW-DETAIL", "备件明细.序号": "1",
        }],
    ))
    assert [item["action"] for item in mixed["rows"]] == ["change", "invalid"]
    assert mixed["rows"][1]["error"]["code"] == "return_capacity_unknown"
    assert apply(client, mixed, confirm_changes=True, reason="作废并补录").status_code == 422
    assert [(item["qty"], item["line_status"]) for item in receipts(client, project)] == [
        ("NaN", "active"),
    ]
    cancelled = preview(client, workbook(qty="2", wbdd=order.order_no, status="已取消"))
    assert cancelled["rows"][0]["action"] == "change"
    response = apply(client, cancelled, confirm_changes=True, reason="源单作废")
    assert response.status_code == 200, response.text
    all_rows = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        params={"line_status": "all"},
    ).json()["items"]
    assert [(item["qty"], item["line_status"]) for item in all_rows] == [("NaN", "voided")]


def test_import_same_order_changed_project_cannot_bypass_ownership_guard(db):
    project, order, client = setup_capacity(db)
    response = apply(client, preview(client, workbook(qty="2", wbdd=order.order_no)))
    assert response.status_code == 200, response.text
    target = _project(db, project_id=str(uuid4()))
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ))
    assignment.is_active = False
    assignment.version += 1
    assignment.archived_by = "synthetic"
    assignment.archived_at = datetime.now(UTC)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid4()), project_id=target.project_id,
        source_order_id=order.raw_order_id, is_active=True,
        created_by="synthetic",
    ))
    db.commit()

    correction = preview(client, workbook(
        qty="2", wbdd=order.order_no, project=target.project_code,
    ))
    assert correction["rows"][0]["action"] == "invalid"
    assert correction["rows"][0]["error"]["code"] == "return_capacity_unknown"
    assert apply(client, correction, confirm_changes=True, reason="核对归属").status_code == 422
    assert [(item["project_id"], item["qty"], item["version"])
            for item in receipts(client, project)] == [(project.project_id, "2.000", 1)]
    assert receipts(client, target) == []
