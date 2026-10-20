"""ITSP-8 / D-31 §5: partial line and header commands through public HTTP."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import io
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.api import maintenance_project_operations as operations_api
from app.config import get_settings
from app.db import SessionLocal, engine
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssueCommand,
    MaintenanceSiteIssueDeliverySource,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_project_operations as operations
from tests.test_maintenance_site_issue_facts import _demand_prices, _facts, _site_workbook
from tests.test_manual_site_issue_api import _client
from tests.test_site_issue_line_correction_api import _body, _url


def _read_issue(client, project_id, issue_id):
    response = client.post(
        "/api/maintenance/site-issues/search", json={"project_id": project_id},
    )
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["rows"] if row["issue_id"] == issue_id)


def _line(issue, line_id):
    return next(row for row in issue["lines"] if row["issue_line_id"] == line_id)


def _header_url(issue):
    return f"/api/maintenance/site-issues/{issue.issue_id}/header"


def _header_body(changes, *, version=1, key=None):
    return {
        "expected_issue_version": version,
        "changes": changes,
        "reason": "核对现场领用事实",
        "idempotency_key": key or f"header-correction-{uuid4()}",
    }


def _counts(db, project_id):
    """Durable audit/outbox/revision are contract evidence without a read API."""
    db.expire_all()
    return (
        db.query(MaintenanceProjectOperationAudit).count(),
        db.query(MaintenanceSiteIssueReturnEvent).count(),
        db.query(MaintenanceSiteIssueCommand).count(),
        operations.get_or_create_workbook_state(db, project_id=project_id).revision,
    )


def _patch_current(client, project, issue, line, changes):
    current = _read_issue(client, project.project_id, issue.issue_id)
    response = client.patch(_url(issue, line), json=_body(
        issue_version=current["version"],
        line_version=_line(current, line.issue_line_id)["version"],
        changes=changes,
    ))
    assert response.status_code == 200, response.text
    return response.json()


def _header_current(client, project, issue, changes):
    current = _read_issue(client, project.project_id, issue.issue_id)
    response = client.patch(_header_url(issue), json=_header_body(changes, version=current["version"]))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("source,mapping", [
    ("page_manual", "contract-v1"),
    ("workbook", "workbook-v1"),
    ("workbook", "workbook-v2"),
    ("legacy", "contract-v1"),
])
def test_line_edit_is_partial_and_preserves_source_and_untouched_line(db, source, mapping):
    """T1/T2: editing one displayed row never replaces the complete issue."""
    project, issue, (target, other), _ = _facts(db, source=source)
    issue.status_mapping_version = mapping
    db.commit()
    client = _client(db)
    before = _read_issue(client, project.project_id, issue.issue_id)
    changes = {"quantity": "3.125", "serial_number": "SN-现场核对", "remark": "仅更正本行"}
    body = _body(changes=changes)

    response = client.patch(_url(issue, target), json=body)

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert (receipt["issue_version"], receipt["line_version"], receipt["workbook_revision"]) == (2, 2, 1)
    assert receipt["changed"] is True
    after = _read_issue(client, project.project_id, issue.issue_id)
    assert (after["source"], after["status_mapping_version"]) == (source, mapping)
    assert after["issue_id"] == before["issue_id"]
    assert _line(after, target.issue_line_id)["quantity"] == "3.125"
    assert _line(after, target.issue_line_id)["serial_number"] == changes["serial_number"]
    assert _line(after, target.issue_line_id)["remark"] == changes["remark"]
    assert _line(after, target.issue_line_id)["part_id"] == _line(before, target.issue_line_id)["part_id"]
    assert _line(after, other.issue_line_id) == _line(before, other.issue_line_id)
    replay = client.patch(_url(issue, target), json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**receipt, "idempotent_replay": True}


def test_header_edit_has_whole_issue_scope_and_keeps_all_line_facts(db):
    """T2: address belongs to one complete issue, not the selected row."""
    project, issue, _, _ = _facts(db, source="workbook")
    _, other_issue, _, _ = _facts(db)
    other_issue.project_id = project.project_id
    db.commit()
    client = _client(db)
    before = _read_issue(client, project.project_id, issue.issue_id)
    other_before = _read_issue(client, project.project_id, other_issue.issue_id)
    body = _header_body({
        "site_location": "核实后的整单地址", "receiver": "接收人乙", "issued_by": "发出人乙",
    })

    response = client.patch(_header_url(issue), json=body)

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["scope"] == "whole_issue"
    assert receipt["affected_line_count"] == 2
    assert "影响整张领用单全部行" in receipt["message"]
    assert (receipt["issue_version"], receipt["workbook_revision"]) == (2, 1)
    after = _read_issue(client, project.project_id, issue.issue_id)
    assert after["site_location"] == "核实后的整单地址"
    assert after["receiver"] == "接收人乙"
    assert after["issued_by"] == "发出人乙"
    assert after["lines"] == before["lines"]
    assert _read_issue(client, project.project_id, other_issue.issue_id) == other_before
    replay = client.patch(_header_url(issue), json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**receipt, "idempotent_replay": True}


@pytest.mark.parametrize("clear_value", ["", None])
def test_header_omitted_values_are_preserved_and_explicit_empty_clears(db, clear_value):
    project, issue, _, _ = _facts(db, source="legacy")
    client = _client(db)
    first = client.patch(_header_url(issue), json=_header_body({"receiver": "新接收人"}))
    assert first.status_code == 200, first.text
    assert _read_issue(client, project.project_id, issue.issue_id)["site_location"] == "现场"
    second = client.patch(_header_url(issue), json=_header_body(
        {"site_location": clear_value}, version=first.json()["issue_version"],
    ))
    assert second.status_code == 200, second.text
    current = _read_issue(client, project.project_id, issue.issue_id)
    assert current["site_location"] is None
    assert current["receiver"] == "新接收人"
    noop = client.patch(_header_url(issue), json=_header_body(
        {"site_location": clear_value}, version=current["version"],
    ))
    assert noop.status_code == 200, noop.text
    assert noop.json()["changed"] is False
    assert noop.json()["issue_version"] == current["version"]
    assert noop.json()["workbook_revision"] == second.json()["workbook_revision"]


@pytest.mark.parametrize("changes", [
    {"quantity": 3}, {"source": "page_manual"}, {"issue_no": ""},
    {"issue_date": None}, {"issue_date": "2026-02-30"},
    {"site_location": "地" * 257}, {"receiver": 123},
])
def test_header_invalid_fields_are_stable_422_without_partial_writes(db, changes):
    project, issue, _, _ = _facts(db)
    client = _client(db)
    before = _read_issue(client, project.project_id, issue.issue_id)
    response = client.patch(_header_url(issue), json=_header_body(changes))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "site_issue_edit_invalid_fields"
    assert _read_issue(client, project.project_id, issue.issue_id) == before
    assert _counts(db, project.project_id) == (0, 0, 0, 0)


@pytest.mark.parametrize("target", ["line", "header"])
def test_part_identity_and_read_only_costs_cannot_be_partially_written(db, target):
    project, issue, (line, _), part = _facts(db)
    replacement = DimPart(pn_std=f"REPLACEMENT-{uuid4().hex[:8]}")
    db.add(replacement)
    db.commit()
    client = _client(db)
    before = _read_issue(client, project.project_id, issue.issue_id)
    patches = [
        {"pn": replacement.pn_std}, {"part_id": replacement.id},
        {"pn": part.pn_std, "part_id": replacement.id},
        {"manual_unit_cost": "1"}, {"source_order_id": "not-a-demand-link"},
        {"site_location": "not-a-line-field"},
    ] if target == "line" else [{"part_id": replacement.id, "pn": replacement.pn_std}]
    for patch in patches:
        response = client.patch(
            _url(issue, line) if target == "line" else _header_url(issue),
            json=_body(changes=patch) if target == "line" else _header_body(patch),
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "site_issue_edit_invalid_fields"
    assert _read_issue(client, project.project_id, issue.issue_id) == before
    assert _counts(db, project.project_id) == (0, 0, 0, 0)


@pytest.mark.parametrize("case", ["native", "delivery", "void", "inactive_project"])
def test_header_rejects_ineligible_issue_even_for_address_only(db, case):
    project, issue, (line, _), part = _facts(db, source="legacy")
    if case == "native":
        issue.source = "site_issue_v2"
    elif case == "delivery":
        delivery = MaintenanceSiteIssueDeliverySource(
            delivery_line_id=str(uuid4()), adapter_key="synthetic_delivery_v1",
            project_id=project.project_id, source_order_id=f"SOURCE-{uuid4()}",
            source_line_id=f"LINE-{uuid4()}", delivery_no="CKD-SYNTHETIC",
            delivery_date=date(2026, 9, 20), part_id=part.id, pn=part.pn_std,
            delivered_quantity=Decimal("2"), mapping_state="ready", mapping_version="contract-v1",
        )
        db.add(delivery)
        db.flush()
        line.delivery_line_id = delivery.delivery_line_id
    elif case == "void":
        issue.normalized_status = "void"
    else:
        project.is_active = False
    db.commit()
    response = _client(db).patch(_header_url(issue), json=_header_body({"site_location": "不能写入"}))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "site_issue_edit_source_blocked" if case in {"native", "delivery"} else "site_issue_edit_inactive"
    )
    assert _counts(db, project.project_id) == (0, 0, 0, 0)


def test_header_rename_collision_does_not_merge_issues(db):
    project, issue, _, _ = _facts(db)
    _, other_issue, _, _ = _facts(db)
    other_issue.project_id = project.project_id
    db.commit()
    client = _client(db)
    before = _read_issue(client, project.project_id, issue.issue_id)
    other_before = _read_issue(client, project.project_id, other_issue.issue_id)
    response = client.patch(_header_url(issue), json=_header_body({"issue_no": other_issue.issue_no}))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "site_issue_edit_write_conflict"
    assert _read_issue(client, project.project_id, issue.issue_id) == before
    assert _read_issue(client, project.project_id, other_issue.issue_id) == other_before
    assert _counts(db, project.project_id) == (0, 0, 0, 0)


def test_demand_link_keep_change_clear_never_reassigns_return_receipt(db):
    """T5: actual receipts keep their independent stable demand association."""
    from app.api import maintenance_return_receipts as receipts_api

    project, issue, (line, _), part = _facts(db)
    first, second = "WBDD-20260920-0001", "WBDD-20260920-0002"
    _demand_prices(db, project=project, part=part, prices=((first, Decimal("10")), (second, Decimal("20"))))
    line.demand_order_no = first
    db.commit()
    client = _client(db)
    client.app.include_router(receipts_api.router, prefix="/api")
    receipts_url = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    created = client.post(receipts_url, json={"pn": part.pn_std, "qty": 1, "wbdd_no": first})
    assert created.status_code == 201, created.text
    before_receipts = client.get(receipts_url).json()

    _patch_current(client, project, issue, line, {"remark": "省略关联"})
    assert _line(_read_issue(client, project.project_id, issue.issue_id), line.issue_line_id)["demand_order_no"] == first
    _patch_current(client, project, issue, line, {"demand_order_no": second})
    changed = _line(_read_issue(client, project.project_id, issue.issue_id), line.issue_line_id)
    assert changed["demand_order_no"] == second
    assert changed["unit_cost_ex_tax"] == "20.00"
    _patch_current(client, project, issue, line, {"demand_order_no": ""})
    assert _line(_read_issue(client, project.project_id, issue.issue_id), line.issue_line_id)["demand_order_no"] is None
    assert client.get(receipts_url).json() == before_receipts


@pytest.mark.parametrize("case", ["foreign_project", "unassigned", "missing"])
def test_demand_invalid_ownership_rolls_back_other_valid_changes(db, case):
    project, issue, (line, _), part = _facts(db)
    other_project, _, _, _ = _facts(db)
    demand_no = "WBDD-20260920-9999"
    if case == "foreign_project":
        _demand_prices(
            db, project=other_project,
            part=part, prices=((demand_no, Decimal("10")),),
        )
    elif case == "unassigned":
        # Build a genuinely unassigned source fact; assignment history is immutable.
        batch = SysImportBatch(
            filename="unassigned-demand.xlsx", file_type="maintenance",
            file_hash=uuid4().hex, status="success",
        )
        db.add(batch)
        db.flush()
        order = FMaintenanceOrder(
            raw_order_id=str(uuid4()), order_no=demand_no, order_date=date(2026, 9, 20),
            data_status="已生效", import_batch_id=batch.id,
        )
        db.add(order)
        db.flush()
        db.add(FMaintenanceLine(
            raw_line_id=str(uuid4()), order_id=order.id, line_no=1,
            part_id=part.id, pn_raw=part.pn_std, pn_std=part.pn_std,
            qty=Decimal("2"), import_batch_id=batch.id,
        ))
        db.commit()
    client = _client(db)
    before = _read_issue(client, project.project_id, issue.issue_id)
    response = client.patch(_url(issue, line), json=_body(changes={
        "quantity": "3", "remark": "不得半提交", "demand_order_no": demand_no,
    }))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "site_issue_edit_demand_project_mismatch"
    assert _read_issue(client, project.project_id, issue.issue_id) == before
    assert _counts(db, project.project_id) == (0, 0, 0, 0)


def test_quantity_reprices_and_part_change_retires_old_manual_price(db):
    """T4: keep a valid manual price for quantity, never transfer it to a new PN."""
    project, issue, (line, _), _ = _facts(db)
    replacement = DimPart(pn_std=f"UNPRICED-{uuid4().hex[:8]}")
    db.add(replacement)
    db.commit()
    client = _client(db)
    client.app.include_router(operations_api.operations_beta_router, prefix="/api")
    filled = client.patch(f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps", json={
        "line_id": line.issue_line_id, "version": 1, "unit_cost_ex_tax": "10",
        "evidence": "旧型号真实凭据", "reason": "补齐缺价",
    })
    assert filled.status_code == 200, filled.text
    _patch_current(client, project, issue, line, {"quantity": "3"})
    priced = _line(_read_issue(client, project.project_id, issue.issue_id), line.issue_line_id)
    assert priced["cost_amount_ex_tax"] == "30.00"
    assert priced["cost_amount_inc_tax"] == "33.90"
    assert priced["manual_evidence"] == "旧型号真实凭据"
    _patch_current(client, project, issue, line, {"part_id": replacement.id, "pn": replacement.pn_std})
    rebound = _line(_read_issue(client, project.project_id, issue.issue_id), line.issue_line_id)
    assert (rebound["pn"], rebound["part_id"]) == (replacement.pn_std, replacement.id)
    assert all(rebound[field] is None for field in (
        "manual_unit_cost", "manual_unit_cost_inc_tax", "manual_evidence",
        "unit_cost_ex_tax", "cost_amount_ex_tax", "cost_amount_inc_tax",
    ))


def test_header_issue_number_reprices_every_active_line(db):
    project, issue, lines, part = _facts(db, source="workbook")
    old_no, new_no = "WBDD-20260920-0001", "WBDD-20260920-0002"
    issue.issue_no = old_no
    _demand_prices(db, project=project, part=part, prices=((old_no, Decimal("10")), (new_no, Decimal("20"))))
    client = _client(db)
    for line in lines:
        _patch_current(client, project, issue, line, {"quantity": "3"})
    before = _read_issue(client, project.project_id, issue.issue_id)
    assert [row["cost_amount_ex_tax"] for row in before["lines"]] == ["30.00", "30.00"]
    receipt = _header_current(client, project, issue, {"issue_no": new_no})
    after = _read_issue(client, project.project_id, issue.issue_id)
    assert after["issue_no"] == new_no
    assert [row["cost_amount_ex_tax"] for row in after["lines"]] == ["60.00", "60.00"]
    assert all(row["reference_samples"][0]["document_no"] == new_no for row in after["lines"])
    assert set(receipt["line_versions"]) == {line.issue_line_id for line in lines}


def test_date_reprices_all_lines_in_new_window_but_address_and_remark_do_not(db):
    project, issue, lines, part = _facts(db)
    issue.issue_date = date(2026, 9, 1)
    batch = SysImportBatch(filename="window-prices.xlsx", file_type="purchase", file_hash=uuid4().hex, status="success")
    db.add(batch)
    db.flush()
    for order_date, amount in ((date(2026, 9, 1), "10"), (date(2026, 10, 1), "30")):
        order = FPurchaseOrder(
            raw_order_id=str(uuid4()), order_no=f"PO-{uuid4().hex[:8]}",
            order_date=order_date, data_status="已生效", is_tax_inclusive=False, import_batch_id=batch.id,
        )
        db.add(order)
        db.flush()
        db.add(FPurchaseLine(
            raw_line_id=str(uuid4()), order_id=order.id, part_id=part.id,
            qty=Decimal("1"), unit_price=Decimal(amount), import_batch_id=batch.id,
        ))
    db.commit()
    client = _client(db)
    for line in lines:
        _patch_current(client, project, issue, line, {"quantity": "3"})
    before = _read_issue(client, project.project_id, issue.issue_id)
    assert [row["unit_cost_ex_tax"] for row in before["lines"]] == ["10.00", "10.00"]
    _header_current(client, project, issue, {"issue_date": "2026-10-01"})
    after = _read_issue(client, project.project_id, issue.issue_id)
    assert [row["cost_amount_ex_tax"] for row in after["lines"]] == ["90.00", "90.00"]
    assert all(row["reference_samples"][0]["document_date"] == "2026-10-01" for row in after["lines"])
    # Newly available evidence cannot cause a metadata-only edit to reprice.
    price = db.scalar(select(FPurchaseLine).join(FPurchaseOrder).where(FPurchaseOrder.order_date == date(2026, 10, 1)))
    price.unit_price = Decimal("99")
    db.commit()
    _header_current(client, project, issue, {"site_location": "只改整单地址"})
    _patch_current(client, project, issue, lines[0], {"serial_number": "SN-不重价", "remark": "只改现场说明"})
    unchanged_cost = _read_issue(client, project.project_id, issue.issue_id)
    for old, current in zip(after["lines"], unchanged_cost["lines"]):
        assert {key: old[key] for key in ("unit_cost_ex_tax", "cost_amount_ex_tax", "reference_samples")} == {
            key: current[key] for key in ("unit_cost_ex_tax", "cost_amount_ex_tax", "reference_samples")
        }


@pytest.mark.parametrize("field", [
    "serial_number", "remark", "quantity", "pn", "demand_order_no",
    "site_location", "issue_date", "issue_no",
])
def test_every_fact_edit_invalidates_old_line_and_header_drafts(db, field):
    """T3: all facts share issue CAS; line/cost CAS also observes changed rows."""
    project, issue, (line, _), part = _facts(db)
    client = _client(db)
    client.app.include_router(operations_api.operations_beta_router, prefix="/api")
    changes = {
        "serial_number": "SN-new", "remark": "新备注", "quantity": "3",
        "site_location": "新地址", "issue_date": "2026-09-21", "issue_no": "LY-RENAMED",
        "demand_order_no": "WBDD-20260920-0001",
    }
    if field == "pn":
        replacement = DimPart(pn_std=f"CHANGED-{uuid4().hex[:8]}")
        db.add(replacement)
        db.commit()
        patch = {"pn": replacement.pn_std, "part_id": replacement.id}
    else:
        patch = {field: changes[field]}
    if field == "demand_order_no":
        _demand_prices(db, project=project, part=part, prices=((changes[field], Decimal("10")),))
    if field in {"site_location", "issue_date", "issue_no"}:
        response = client.patch(_header_url(issue), json=_header_body(patch))
    else:
        response = client.patch(_url(issue, line), json=_body(changes=patch))
    assert response.status_code == 200, response.text
    after = _read_issue(client, project.project_id, issue.issue_id)
    old_line = client.patch(_url(issue, line), json=_body(changes={"remark": "旧抽屉覆盖"}))
    old_header = client.patch(_header_url(issue), json=_header_body({"receiver": "旧单头覆盖"}))
    for conflict in (old_line, old_header):
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "site_issue_edit_version_conflict"
    # Address changes do not alter line/cost facts and therefore retain line CAS.
    if field != "site_location":
        stale_cost = client.patch(f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps", json={
            "line_id": line.issue_line_id, "version": 1, "unit_cost_ex_tax": "10",
            "evidence": "旧补价抽屉", "reason": "不许覆盖后来的事实",
        })
        assert stale_cost.status_code == 409, stale_cost.text
    assert _read_issue(client, project.project_id, issue.issue_id) == after
    assert response.json()["workbook_revision"] == 1


def _workbook_client(db, monkeypatch):
    from app.api import maintenance_project_master_workbook as master_api

    monkeypatch.setattr(master_api.get_settings(), "maintenance_project_master_v2_enabled", True)
    client = _client(db)
    client.app.include_router(master_api.router, prefix="/api")
    return client


def _upload_book(client, project, book):
    content = io.BytesIO()
    book.save(content)
    return client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/master-workbook/apply",
        files={"file": ("facts.xlsx", content.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


@pytest.mark.parametrize("field,column,web_value,file_value", [
    ("serial_number", "SN", "SN-web", "SN-file"),
    ("remark", "备注", "网页备注", "旧文件备注"),
    ("quantity", "领用数量", "3", 4),
    ("pn", "PN", None, None),
    ("site_location", "领用地址", "网页地址", "旧文件地址"),
    ("issue_date", "领用日期", "2026-09-21", date(2026, 9, 22)),
    ("issue_no", "领用单号", "LY-WEB-RENAMED", "LY-FILE-RENAMED"),
])
def test_old_v2_workbook_conflicting_line_or_header_apply_is_409(
    db, monkeypatch, field, column, web_value, file_value,
):
    project, issue, (line, _), _ = _facts(db, source="workbook")
    client = _workbook_client(db, monkeypatch)
    book, sheet, columns, rows = _site_workbook(db, project)
    if field == "pn":
        # Workbook selection resolves normalized standard PN (uppercase).
        web_part = DimPart(pn_std=f"WEB-{uuid4().hex[:8].upper()}")
        file_part = DimPart(pn_std=f"FILE-{uuid4().hex[:8].upper()}")
        db.add_all([web_part, file_part])
        db.commit()
        patch = {"pn": web_part.pn_std, "part_id": web_part.id}
        file_value = file_part.pn_std
    else:
        patch = {field: web_value}
    header = field in {"site_location", "issue_date", "issue_no"}
    for row in rows.values() if header else [rows[line.issue_line_id]]:
        sheet.cell(row, columns[column]).value = file_value
    db.rollback()
    changed = client.patch(
        _header_url(issue) if header else _url(issue, line),
        json=_header_body(patch) if header else _body(changes=patch),
    )
    assert changed.status_code == 200, changed.text
    before_upload = _read_issue(client, project.project_id, issue.issue_id)
    counts = _counts(db, project.project_id)
    uploaded = _upload_book(client, project, book)
    assert uploaded.status_code == 409, uploaded.text
    assert uploaded.json()["detail"]["code"] == "row_conflicts"
    assert _read_issue(client, project.project_id, issue.issue_id) == before_upload
    assert _counts(db, project.project_id) == counts


def test_old_v2_workbook_keeps_server_fields_that_user_did_not_change(db, monkeypatch):
    project, issue, (line, _), _ = _facts(db, source="workbook")
    client = _workbook_client(db, monkeypatch)
    book, sheet, columns, rows = _site_workbook(db, project)
    sheet.cell(rows[line.issue_line_id], columns["备注"]).value = "Excel 只改备注"
    db.rollback()
    _patch_current(client, project, issue, line, {"serial_number": "网页最新SN"})
    applied = _upload_book(client, project, book)
    assert applied.status_code == 200, applied.text
    after = _line(_read_issue(client, project.project_id, issue.issue_id), line.issue_line_id)
    assert (after["serial_number"], after["remark"]) == ("网页最新SN", "Excel 只改备注")


def test_old_v2_demand_column_keeps_existing_readonly_422_contract(db, monkeypatch):
    """Compatibility limit: old V2 files must be downloaded after link changes."""
    project, issue, (line, _), part = _facts(db, source="workbook")
    demand_no = "WBDD-20260920-0001"
    _demand_prices(db, project=project, part=part, prices=((demand_no, Decimal("10")),))
    client = _workbook_client(db, monkeypatch)
    book, _, _, _ = _site_workbook(db, project)
    db.rollback()
    _patch_current(client, project, issue, line, {"demand_order_no": demand_no})
    after = _read_issue(client, project.project_id, issue.issue_id)
    uploaded = _upload_book(client, project, book)
    assert uploaded.status_code == 422, uploaded.text
    assert uploaded.json()["detail"]["code"] == "readonly_field_changed"
    assert _read_issue(client, project.project_id, issue.issue_id) == after


def test_header_and_line_idempotency_keys_cannot_be_repurposed(db):
    project, issue, (line, _), _ = _facts(db)
    client = _client(db)
    body = _header_body({"site_location": "首次地址"})
    first = client.patch(_header_url(issue), json=body)
    assert first.status_code == 200, first.text
    for url, payload in (
        (_header_url(issue), {**body, "changes": {"site_location": "变造地址"}}),
        (_url(issue, line), _body(key=body["idempotency_key"])),
    ):
        conflict = client.patch(url, json=payload)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "site_issue_edit_idempotency_conflict"
    _patch_current(client, project, issue, line, {"remark": "后续编辑"})
    replay = client.patch(_header_url(issue), json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**first.json(), "idempotent_replay": True}


@pytest.mark.parametrize("case", ["action", "data", "page", "scope", "shared", "replay_revoked"])
def test_header_requires_current_real_name_action_data_page_and_project_scope(db, case):
    project, issue, _, _ = _facts(db)
    client = _client(db)
    body = _header_body({"site_location": "有权限才可写"})
    if case == "replay_revoked":
        first = client.patch(_header_url(issue), json=body)
        assert first.status_code == 200, first.text
    before = _read_issue(client, project.project_id, issue.issue_id)
    user = db.scalar(select(SysUser).where(SysUser.username == "manual_site_admin"))
    user.role = "sales" if case == "scope" else "boss"
    user.permissions = {
        "page_maintenance": case != "page",
        "action_maintenance_site_issue_manage": case not in {"action", "replay_revoked"},
        "data_purchase_cost": case != "data",
    }
    db.commit()
    if case == "shared":
        from app.auth import _make_token

        token, _ = _make_token(role="admin", sub=user.username, name="共享账号", fallback=True)
        client.headers["Authorization"] = f"Bearer {token}"
    else:
        login = client.post("/api/auth/login", json={"username": user.username, "password": "manual-password-123"})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    denied = client.patch(_header_url(issue), json=body)
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["code"] == "site_issue_edit_permission_denied"
    # An independent authorized reader can still inspect the unchanged facts.
    admin = _client(db, username="facts_reader_admin")
    assert _read_issue(admin, project.project_id, issue.issue_id) == before
    assert db.query(MaintenanceSiteIssueCommand).count() == int(case == "replay_revoked")


def test_header_permission_revoked_during_lock_wait_cannot_write(db):
    project, issue, _, _ = _facts(db)
    client = _client(db)
    url, body = _header_url(issue), _header_body({"site_location": "锁后不得越权"})
    db.rollback()
    reached_state = threading.Event()

    def waiting(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO maintenance_project_workbook_state "):
            reached_state.set()

    with SessionLocal() as blocker:
        operations.get_or_create_workbook_state(blocker, project_id=project.project_id, lock=True)
        event.listen(engine, "before_cursor_execute", waiting)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.patch, url, json=body)
                try:
                    assert reached_state.wait(timeout=10)
                    with SessionLocal() as revoker:
                        user = revoker.scalar(select(SysUser).where(SysUser.username == "manual_site_admin"))
                        user.role = "boss"
                        user.permissions = {
                            "page_maintenance": True, "data_purchase_cost": True,
                            "action_maintenance_site_issue_manage": False,
                        }
                        revoker.commit()
                finally:
                    blocker.rollback()
                response = future.result(timeout=20)
        finally:
            blocker.rollback()
            event.remove(engine, "before_cursor_execute", waiting)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "site_issue_edit_permission_denied"
    assert _counts(db, project.project_id) == (0, 0, 0, 0)


def test_concurrent_header_and_line_commands_serialize_at_one_issue_cas(db):
    project, issue, (line, _), _ = _facts(db)
    client = _client(db)
    requests = (
        (_header_url(issue), _header_body({"site_location": "并发单头"})),
        (_url(issue, line), _body(changes={"remark": "并发行"})),
    )
    db.rollback()
    barrier = threading.Barrier(2)

    def synchronize(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            barrier.wait(timeout=10)

    def request(url, body):
        with TestClient(client.app) as concurrent_client:
            concurrent_client.headers.update(client.headers)
            return concurrent_client.patch(url, json=body)

    event.listen(engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request, *item) for item in requests]
            responses = [future.result(timeout=20) for future in futures]
    finally:
        event.remove(engine, "before_cursor_execute", synchronize)
    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"]["code"] == "site_issue_edit_version_conflict"
    assert _counts(db, project.project_id) == (1, 0, 1, 1)


@pytest.mark.parametrize("command,failed_table", [
    ("line", "maintenance_project_operation_audit"),
    ("line", "maintenance_site_issue_return_event"),
    ("line", "maintenance_site_issue_command"),
    ("header", "maintenance_project_operation_audit"),
    ("header", "maintenance_site_issue_command"),
])
def test_extended_fact_cost_audit_event_receipt_and_revision_rollback_together(db, command, failed_table):
    project, issue, (line, _), part = _facts(db)
    new_no = "WBDD-20260920-0001"
    _demand_prices(db, project=project, part=part, prices=((new_no, Decimal("20")),))
    client = _client(db)
    before = _read_issue(client, project.project_id, issue.issue_id)
    url = _url(issue, line) if command == "line" else _header_url(issue)
    body = _body(changes={"quantity": "3", "remark": "不得半写"}) if command == "line" else _header_body({
        "issue_no": new_no, "site_location": "不得半写",
    })

    def reject_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(f"INSERT INTO {failed_table} "):
            raise RuntimeError("synthetic durable-write failure")

    event.listen(engine, "before_cursor_execute", reject_insert)
    try:
        with TestClient(client.app, raise_server_exceptions=False) as failing_client:
            failing_client.headers.update(client.headers)
            response = failing_client.patch(url, json=body)
        assert response.status_code == 500, response.text
    finally:
        event.remove(engine, "before_cursor_execute", reject_insert)
    assert _read_issue(client, project.project_id, issue.issue_id) == before
    assert _counts(db, project.project_id) == (0, 0, 0, 0)


def test_audit_records_real_actor_reason_and_normalized_before_after(db):
    project, issue, (line, _), _ = _facts(db)
    client = _client(db)
    first = client.patch(_url(issue, line), json=_body(changes={"quantity": "3.000", "remark": "实地核对"}))
    assert first.status_code == 200, first.text
    _header_current(client, project, issue, {"site_location": "实际收货地址"})
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.action == "site_issue_facts_edited",
    ).order_by(MaintenanceProjectOperationAudit.operated_at)))
    assert len(audits) == 2
    assert all(audit.operated_by == "manual_site_admin" for audit in audits)
    row_audit = next(audit for audit in audits if line.issue_line_id in audit.after_json["lines"])
    assert row_audit.reason == "依据合同逐行核对返还要求"
    assert row_audit.before_json["lines"][line.issue_line_id]["quantity"] == "2.000"
    assert row_audit.after_json["lines"][line.issue_line_id]["quantity"] == "3.000"
    assert row_audit.after_json["lines"][line.issue_line_id]["remark"] == "实地核对"
    header_audit = next(audit for audit in audits if not audit.after_json["lines"])
    assert header_audit.before_json["header"]["site_location"] == "现场"
    assert header_audit.after_json["header"]["site_location"] == "实际收货地址"
    assert header_audit.reason == "核对现场领用事实"
    assert _counts(db, project.project_id) == (3, 1, 2, 2)


def test_normalized_unchanged_line_does_not_create_fact_audit_or_versions(db):
    project, issue, (line, _), part = _facts(db)
    client = _client(db)
    before = _read_issue(client, project.project_id, issue.issue_id)
    response = client.patch(_url(issue, line), json=_body(changes={
        "quantity": "2.000", "pn": part.pn_std, "part_id": part.id,
        "demand_order_no": "", "serial_number": None, "remark": None,
    }))
    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert response.json()["issue_version"] == response.json()["line_version"] == 1
    assert _read_issue(client, project.project_id, issue.issue_id) == before
    assert _counts(db, project.project_id) == (0, 0, 1, 0)


@pytest.mark.parametrize("beta_enabled", [False, True])
def test_header_route_uses_stable_panel_gate_in_production(db, monkeypatch, beta_enabled):
    project, issue, _, _ = _facts(db)
    client = _client(db, production_routes=True)
    settings = get_settings()
    monkeypatch.setattr(settings, "maintenance_beta_enabled", beta_enabled)
    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", False)
    body = _header_body({"site_location": "已批准页面入口"})
    for path in (_header_url(issue), "/api/maintenance/site-issues/not-real/header"):
        closed = client.patch(path, json=body)
        assert closed.status_code == 404, closed.text
        assert closed.json()["detail"] == "页面不存在"
    assert _counts(db, project.project_id) == (0, 0, 0, 0)
    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", True)
    opened = client.patch(_header_url(issue), json=body)
    assert opened.status_code == 200, opened.text
    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", False)
    replay_closed = client.patch(_header_url(issue), json=body)
    assert replay_closed.status_code == 404, replay_closed.text


def test_issue_search_can_reload_exact_stable_identity(db):
    project, issue, _, _ = _facts(db)
    _, other, _, _ = _facts(db)
    other.project_id = project.project_id
    db.commit()
    client = _client(db)
    response = client.post("/api/maintenance/site-issues/search", json={
        "project_id": project.project_id, "issue_id": issue.issue_id, "page_size": 1,
    })
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert [row["issue_id"] for row in response.json()["rows"]] == [issue.issue_id]
