"""D-34: real source upload -> scoped impact -> explicit human confirmation."""
from decimal import Decimal
from uuid import uuid4

from app.etl import pipeline
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project_operations import MaintenanceSiteIssue
from sqlalchemy import select

from tests.boss_board_helpers import assign, client_for, make_project
from tests.test_maintenance_return_metrics import _issue, _receipt
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook


def upload(db, tmp_path, rows, *, mode="upsert"):
    name = f"{uuid4()}.xlsx"
    path = write_workbook(str(tmp_path / name), COLUMNS_91, rows)
    result = pipeline.run_import(db, path, name, uploaded_by="cancel-tester", mode=mode)
    db.commit()
    db.expire_all()
    return result


def setup(db, tmp_path, *, status="已取消", mode="upsert"):
    client = client_for(db, username="cancel-admin", role="admin")
    project = make_project(db, "CANCEL-PROJECT")
    rows = make_rows(orders=1, lines_per_order=2)
    for row in rows:
        row["需求单号"] = "WBDD-20260609-0001"
        row["销售订单"] = ""
    upload(db, tmp_path, rows)
    parent = db.scalar(select(FMaintenanceOrder))
    assign(db, parent, project)
    lines = list(db.scalars(select(FMaintenanceLine).order_by(FMaintenanceLine.id)))
    part = db.get(DimPart, lines[0].part_id)
    exact = _issue(db, project, part, "2", source_order_id=parent.raw_order_id)
    exact.source_line_id = lines[0].raw_line_id
    unknown = _issue(db, project, part, "9")
    unknown.demand_order_no = parent.order_no
    receipt = _receipt(db, project, part, "8", source_order_id=parent.raw_order_id)
    db.commit()
    for row in rows:
        row["数据状态"] = status
    batch = upload(db, tmp_path, rows, mode=mode)
    url = f"/api/maintenance/projects/{project.project_id}/source-cancellations"
    detail_url = f"{url}/{batch.id}/{parent.raw_order_id}"
    return client, project, rows, parent, lines, exact, unknown, receipt, batch, url, detail_url


def test_cancelled_upsert_lists_impact_without_voiding_facts(db, tmp_path):
    client, _project, _rows, _parent, lines, exact, unknown, receipt, _batch, url, detail_url = setup(db, tmp_path)
    response = client.get(url)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 1
    assert result["items"][0]["executable"] is True
    detail = client.get(detail_url)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert {row["raw_line_id"] for row in body["demands"]} == {line.raw_line_id for line in lines}
    assert [row["issue_line_id"] for row in body["exact_site_lines"]] == [exact.issue_line_id]
    assert [row["issue_line_id"] for row in body["unlinked_site_lines"]] == [unknown.issue_line_id]
    db.expire_all()
    assert all(line.is_active for line in lines) and exact.is_active and unknown.is_active
    assert db.get(MaintenanceSiteIssue, exact.issue_id).normalized_status == "confirmed"
    assert receipt.qty == Decimal(8) and receipt.line_status == "active"


def confirm_body(client, detail_url):
    detail = client.get(detail_url)
    assert detail.status_code == 200, detail.text
    return {"expected_digest": detail.json()["expected_digest"], "reason": "核对源单已取消，确认影响",
            "idempotency_key": str(uuid4())}


def test_human_confirmation_voids_only_exact_links_preserving_received_and_cost_facts(db, tmp_path):
    from app.models.maintenance import MaintenanceDemandTombstone
    from app.models.maintenance_project_operations import (
        MaintenanceProjectOperationAudit,
    )
    client, _project, _rows, parent, lines, exact, unknown, receipt, _batch, _url, detail_url = setup(db, tmp_path)
    lines[0].cost_amount = Decimal(123)
    exact.cost_amount = Decimal(456)
    exact.unit_cost = Decimal(228)
    exact.cost_source = "manual"
    exact.unit_cost_ex_tax = Decimal(228)
    exact.unit_cost_inc_tax = Decimal("257.64")
    exact.cost_amount_ex_tax = Decimal(456)
    exact.cost_amount_inc_tax = Decimal("515.28")
    db.commit()
    response = client.post(detail_url + "/confirm", json=confirm_body(client, detail_url))
    assert response.status_code == 200, response.text
    assert response.json()["voided_lines"] == 2
    assert response.json()["voided_site_lines"] == 1
    assert response.json()["voided_orders"] == 1
    db.expire_all()
    assert all(not line.is_active for line in lines)
    assert not exact.is_active and unknown.is_active
    assert receipt.qty == Decimal(8) and receipt.line_status == "active"
    assert lines[0].cost_amount == Decimal(123) and exact.cost_amount == Decimal(456)
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "maintenance_line")))
    assert len(audits) == 2
    assert all(row.operated_by == "cancel-admin" and row.reason == "核对源单已取消，确认影响"
               and row.before_json["is_active"] and not row.after_json["is_active"] for row in audits)


from copy import deepcopy

import pytest
from app.models.maintenance import MaintenanceDemandTombstone
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch


@pytest.mark.parametrize("status", ["草稿", "进行中", "", None, "已作废", "未来未知状态"])
def test_only_cancelled_status_can_execute_and_unknown_status_warns(db, tmp_path, status):
    client, _, _, _, lines, exact, _, _, _, url, detail_url = setup(db, tmp_path, status=status)
    response = client.get(url)
    assert response.status_code == 200
    items = response.json()["items"]
    if status in {"已作废", "未来未知状态"}:
        assert len(items) == 1 and items[0]["executable"] is False
        assert "未知非生效状态，需人工" in items[0]["warnings"]
    else:
        assert items == []
    response = client.post(detail_url + "/confirm", json=confirm_body(client, detail_url))
    assert response.status_code == 409
    db.expire_all()
    assert all(line.is_active for line in lines) and exact.is_active


def test_skip_archived_cancel_does_not_offer_executable_candidate(db, tmp_path):
    client, _, _, parent, lines, exact, _, _, _, url, detail_url = setup(db, tmp_path, mode="skip")
    assert parent.data_status == "已生效"
    assert client.get(url).json()["items"] == []
    detail = client.get(detail_url).json()
    assert detail["executable"] is False
    assert "upsert" in " ".join(detail["warnings"])
    assert client.post(detail_url + "/confirm", json=confirm_body(client, detail_url)).status_code == 409
    db.expire_all()
    assert all(line.is_active for line in lines) and exact.is_active


@pytest.mark.parametrize("drift", ["header", "demand", "issue", "unlinked", "assignment", "project", "batch"])
def test_reviewed_impact_drift_returns_409_without_partial_void(db, tmp_path, drift):
    client, project, _, parent, lines, exact, unknown, _, batch, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    if drift == "header":
        parent.data_status = "已生效"
    elif drift == "demand":
        lines[1].line_note = "确认前他人修改"
    elif drift == "issue":
        exact.quantity = Decimal(7)
    elif drift == "unlinked":
        unknown.remark = "新核对证据"
    elif drift == "assignment":
        from datetime import UTC, datetime

        other = make_project(db, "OTHER-PROJECT")
        previous = db.scalar(select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == parent.raw_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True)))
        # Preserve the cancelled header to isolate an ownership-generation
        # drift. The public assign command only accepts effective headers.
        previous.is_active = False
        previous.version += 1
        previous.archived_at = datetime.now(UTC)
        previous.archived_by = "cancel-admin"
        db.flush()
        db.add(MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()), source_order_id=parent.raw_order_id,
            project_id=other.project_id, is_active=True, created_by="cancel-admin"))
    elif drift == "project":
        project.is_active = False
    else:
        changed = deepcopy(batch.report_json)
        changed["_wbdd_source_status_snapshot"]["mode"] = "skip"
        batch.report_json = changed
    db.commit()
    response = client.post(detail_url + "/confirm", json=body)
    assert response.status_code == 409, response.text
    db.expire_all()
    assert all(line.is_active for line in lines) and exact.is_active and unknown.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id) is None
    assert "_source_cancel_confirmations" not in db.get(SysImportBatch, batch.id).report_json


def test_replay_returns_original_result_after_new_and_restored_lines_without_mutation(db, tmp_path):
    client, project, _, parent, lines, exact, _, _, _, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    first = client.post(detail_url + "/confirm", json=body)
    assert first.status_code == 200, first.text
    db.expire_all()
    # Simulate separately authorized correction/restoration; replay must not undo it.
    exact.is_active = True
    lines[0].is_active = True
    added = _issue(db, project, db.get(DimPart, lines[0].part_id), "6", source_order_id=parent.raw_order_id)
    added.source_line_id = lines[0].raw_line_id
    db.commit()
    response = client.post(detail_url + "/confirm", json=body)
    assert response.status_code == 200, response.text
    assert {**response.json(), "replayed": False} == first.json()
    db.expire_all()
    assert exact.is_active and lines[0].is_active and added.is_active
    changed = {**body, "reason": "同键改变原因"}
    assert client.post(detail_url + "/confirm", json=changed).status_code == 409


@pytest.mark.parametrize("confirmed", [False, True])
def test_latest_active_upload_restores_visibility_only_before_system_void(db, tmp_path, confirmed):
    from app.services import maintenance_analytics
    client, _, rows, parent, lines, exact, _, _, _, url, detail_url = setup(db, tmp_path)
    assert Decimal(maintenance_analytics.pn_ranking(db, range_="all", can_cost=True)["summary"]["total_effective_qty"]) == 0
    if confirmed:
        response = client.post(detail_url + "/confirm", json=confirm_body(client, detail_url))
        assert response.status_code == 200, response.text
    for row in rows:
        row["数据状态"] = "已生效"
    upload(db, tmp_path, rows)
    assert parent.data_status == "已生效"
    assert client.get(url).json()["items"] == []
    summary = maintenance_analytics.pn_ranking(db, range_="all", can_cost=True)["summary"]
    assert Decimal(summary["total_effective_qty"]) == (0 if confirmed else 6)
    assert all(line.is_active is not confirmed for line in lines)
    assert exact.is_active is not confirmed
    if confirmed:
        assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None


def test_no_permission_and_cross_project_requests_are_forbidden(db, tmp_path):
    client, _, _, parent, lines, exact, _, _, _, url, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    no_action = client_for(db, username="cancel-no-action", role="boss", overrides={
        "page_maintenance": True, "action_maintenance_demand_delete": False})
    assert no_action.post(detail_url + "/confirm", json=body).status_code == 403
    outsider = client_for(db, username="cancel-outsider", overrides={
        "page_maintenance": True, "action_maintenance_demand_delete": True})
    for method, path in [("get", url), ("get", detail_url), ("post", detail_url + "/confirm")]:
        response = getattr(outsider, method)(path, **({"json": body} if method == "post" else {}))
        assert response.status_code == 403, response.text
    db.expire_all()
    assert all(line.is_active for line in lines) and exact.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id) is None


def test_historical_cancel_without_new_evidence_is_warning_only_and_reads_do_not_write(db, tmp_path):
    from app.models.maintenance_project_operations import (
        MaintenanceProjectOperationAudit,
    )
    from sqlalchemy import func
    client, _, _, parent, lines, exact, _, _, batch, url, detail_url = setup(db, tmp_path)
    old = dict(batch.report_json)
    old.pop("_wbdd_source_status_snapshot")
    batch.report_json = old
    db.commit()
    before = db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit))
    for _ in range(2):
        item = client.get(url).json()["items"][0]
        assert not item["executable"]
        assert not client.get(detail_url).json()["executable"]
    assert client.post(detail_url + "/confirm", json=confirm_body(client, detail_url)).status_code == 409
    db.expire_all()
    assert before == db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit))
    assert all(line.is_active for line in lines) and exact.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id) is None


def test_inventory_and_receipt_rows_are_preserved_after_confirmation(db, tmp_path):
    from app.models.inventory import Inventory
    client, _, _, _, lines, _, _, receipt, _, _, detail_url = setup(db, tmp_path)
    stock = Inventory(raw_inventory_id="CANCEL-STOCK", part_id=lines[0].part_id,
                      pn_std=lines[0].pn_std, warehouse="合成仓", source_qty=Decimal(20), manual_qty=Decimal(19))
    db.add(stock)
    db.commit()
    before_stock = {c.name: getattr(stock, c.name) for c in stock.__table__.columns}
    before_receipt = {c.name: getattr(receipt, c.name) for c in receipt.__table__.columns}
    response = client.post(detail_url + "/confirm", json=confirm_body(client, detail_url))
    assert response.status_code == 200, response.text
    db.expire_all()
    assert {c.name: getattr(stock, c.name) for c in stock.__table__.columns} == before_stock
    assert {c.name: getattr(receipt, c.name) for c in receipt.__table__.columns} == before_receipt


@pytest.mark.parametrize("relation", ["warehouse", "wrong_header", "same_pn_only"])
def test_ambiguous_lineage_is_never_voided_by_identifier_or_pn_coincidence(db, tmp_path, relation):
    client, _project, _, _parent, lines, exact, _, _, _, _, detail_url = setup(db, tmp_path)
    if relation == "warehouse":
        db.get(MaintenanceSiteIssue, exact.issue_id).source = "site_issue_v2"
    elif relation == "wrong_header":
        exact.source_order_id = "another-raw-order"
    else:
        exact.source_line_id = None
        exact.source_order_id = None
    db.commit()
    detail = client.get(detail_url).json()
    assert detail["exact_site_lines"] == []
    if relation != "same_pn_only":
        assert exact.issue_line_id in {row["issue_line_id"] for row in detail["unlinked_site_lines"]}
    response = client.post(detail_url + "/confirm", json=confirm_body(client, detail_url))
    assert response.status_code == 200, response.text
    assert response.json()["voided_site_lines"] == 0
    db.expire_all()
    assert exact.is_active and all(not line.is_active for line in lines)


def test_consumed_candidate_cannot_execute_with_new_key_after_manual_restore(db, tmp_path):
    from app.services import maintenance_demands
    client, project, _, parent, lines, exact, _, _, _, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    assert client.post(detail_url + "/confirm", json=body).status_code == 200
    db.expire_all()
    maintenance_demands.restore_demand(db, source_order_id=parent.raw_order_id,
                                      operated_by="cancel-admin", reason="实名恢复核对")
    # Existing recovery deliberately does not reactivate assignment or children.
    assign(db, parent, project)
    lines[0].is_active = True
    exact.is_active = True
    db.commit()
    detail = client.get(detail_url).json()
    assert detail["executable"] is False
    changed = {**body, "idempotency_key": str(uuid4()), "expected_digest": detail["expected_digest"]}
    assert client.post(detail_url + "/confirm", json=changed).status_code == 409
    response = client.post(detail_url + "/confirm", json=body)
    assert response.status_code == 200 and response.json()["replayed"] is True
    db.expire_all()
    assert lines[0].is_active and exact.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is not None


@pytest.mark.parametrize("failing_table", ["maintenance_site_issue_return_event", "maintenance_demand_tombstone"])
def test_database_failure_rolls_back_cascade_audit_receipt_and_revision(db, tmp_path, failing_table):
    from app.db import engine
    from app.main import app
    from fastapi.testclient import TestClient
    from sqlalchemy import event

    from tests.test_maintenance_source_cancel_concurrency import _business_snapshot
    client, _, _, _, _, _, _, _, _, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    before = _business_snapshot(db)
    db.commit()

    def fail_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO " + failing_table + " "):
            raise RuntimeError("synthetic database write failure")

    requester = TestClient(app, raise_server_exceptions=False)
    requester.headers["Authorization"] = client.headers["Authorization"]
    event.listen(engine, "before_cursor_execute", fail_write)
    try:
        response = requester.post(detail_url + "/confirm", json=body)
    finally:
        event.remove(engine, "before_cursor_execute", fail_write)
        requester.close()
    assert response.status_code == 500, response.text
    db.expire_all()
    assert _business_snapshot(db) == before


def test_pagination_and_candidate_detail_do_not_leak_other_project(db, tmp_path):
    from tests.test_maintenance_wbdd_reconciliation_candidates import (
        order as make_order,
    )
    client, project, _, parent, _, _, _, _, _, url, _ = setup(db, tmp_path)
    second = make_order(db, project, 7)
    second.data_status = "已作废"
    other_project = make_project(db, "CANCEL-HIDDEN")
    hidden = make_order(db, other_project, 8)
    hidden.data_status = "已取消"
    db.commit()
    first = client.get(url, params={"page": 1, "page_size": 1}).json()
    later = client.get(url, params={"page": 2, "page_size": 1}).json()
    assert first["total"] == later["total"] == 2
    assert len(first["items"]) == len(later["items"]) == 1
    assert {first["items"][0]["source_order_id"], later["items"][0]["source_order_id"]} == {
        parent.raw_order_id, second.raw_order_id}
    assert "_wbdd_source_status_snapshot" not in str(first)


def test_old_cancel_batch_cannot_be_reused_after_new_legal_upload(db, tmp_path):
    client, _, rows, _, lines, exact, _, _, _, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    for row in rows:
        row["需求明细.需求数量"] = "4"
    upload(db, tmp_path, rows)
    response = client.post(detail_url + "/confirm", json=body)
    assert response.status_code == 409
    db.expire_all()
    assert all(line.is_active for line in lines) and exact.is_active


def test_partial_issue_void_keeps_unrelated_sibling_and_rechecks_new_siblings(db, tmp_path):
    from app.models.maintenance_project_operations import MaintenanceSiteIssueLine
    client, _, _, _, lines, exact, _, _, _, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    sibling = MaintenanceSiteIssueLine(
        issue_line_id=str(uuid4()), issue_id=exact.issue_id, line_no=2,
        part_id=lines[0].part_id, pn=lines[0].pn_std, quantity=Decimal(5),
        is_active=True, algorithm_version="synthetic-v1")
    db.add(sibling)
    db.commit()
    assert client.post(detail_url + "/confirm", json=body).status_code == 409
    fresh = confirm_body(client, detail_url)
    response = client.post(detail_url + "/confirm", json=fresh)
    assert response.status_code == 200, response.text
    assert response.json()["voided_site_lines"] == 1
    assert response.json()["voided_site_issues"] == 0
    db.expire_all()
    assert not exact.is_active and sibling.is_active and sibling.quantity == 5
    assert db.get(MaintenanceSiteIssue, exact.issue_id).normalized_status == "confirmed"
    assert exact.version == 2 and sibling.version == 1


def test_confirm_requires_nonempty_reason_and_rejects_reusing_key_for_other_batch(db, tmp_path):
    client, _, rows, _, lines, exact, _, _, _, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    assert client.post(detail_url + "/confirm", json={**body, "reason": "   "}).status_code == 422
    assert client.post(detail_url + "/confirm", json=body).status_code == 200
    new_batch = upload(db, tmp_path, rows)
    different = detail_url.rsplit("/", 2)[0] + f"/{new_batch.id}/SYN-O001/confirm"
    assert client.post(different, json=body).status_code == 409
    db.expire_all()
    assert all(not line.is_active for line in lines) and not exact.is_active
