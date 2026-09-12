"""Machine evidence follows corrected sources and inherits ledger read scope."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app import config
from app.db import engine
from app.models.maintenance_doc_import import MaintenanceDocLineRow, MaintenanceRkdReturnLine
from app.services import maintenance_return_receipts as ledger
from tests.test_return_ledger_stable_release import _stable_client
from tests.test_return_receipt_hardening import _grant
from tests.test_return_receipt_import import apply, preview, project, workbook
from tests.test_site_issue_v2_api import _project


def test_machine_components_follow_explicit_source_correction_without_counting(db, project):
    apply(db, preview(db, workbook(machine=True, qty="1.250")))
    first = ledger.search_receipts(db, project_id=project.project_id)["items"][0]
    assert first["qty"] == "1.000"
    assert [(r["pn"], r["qty"]) for r in first["components"]] == [("MEMORY", "1.250")]
    old_head = first["head_row_id"]
    batch = preview(db, workbook(machine=True, qty="3.500", extra=[{
        "备件明细.备件PN": "DISK",
        "备件明细.备件描述": "New disk",
        "备件明细.入库数量": "6",
        "备件明细.测试结果": "坏品",
        "备件明细.数据ID(不可修改)": "SECOND",
        "备件明细.序号": "2",
    }]))
    assert batch.report_json["summary"]["change"] == 1
    # Preview does not silently replace the ledger's current source evidence.
    assert ledger.search_receipts(db, project_id=project.project_id)["items"][0] == first
    apply(db, batch, confirm_changes=True, reason="source component correction")
    latest = ledger.search_receipts(db, project_id=project.project_id)["items"][0]
    assert latest["receipt_id"] == first["receipt_id"]
    assert latest["head_row_id"] != old_head
    assert latest["version"] == 2
    assert [(r["pn"], r["qty"]) for r in latest["components"]] == [
        ("MEMORY", "3.500"), ("DISK", "6"),
    ]
    assert latest["components"][1]["description"] == "New disk"
    assert latest["components"][1]["condition"] == "坏品"
    for component in latest["components"]:
        assert set(component) == {"row_id", "pn", "description", "qty", "condition"}
        detail = db.get(MaintenanceDocLineRow, component["row_id"])
        assert detail.head_row_id == latest["head_row_id"]
        assert detail.qty is None  # Display original precision; never round or count it.
    original = db.scalar(select(MaintenanceDocLineRow).where(MaintenanceDocLineRow.head_row_id == old_head))
    assert original.raw_json["备件明细.入库数量"] == "1.250"
    assert ledger.receipt_summary(db, project_id=project.project_id)["project_total_qty"] == "1.000"


def test_machine_component_query_is_batched_for_authorized_page_only(db, project, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "maintenance_beta_enabled", False)
    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", True)
    for number in range(3):
        apply(db, preview(db, workbook(machine=True, head_id=f"HEAD-{number}", line_id=f"LINE-{number}")))
    other = _project(db, project_id=str(uuid4()))
    other.display_name = "PRIVATE-PROJECT"
    db.commit()
    apply(db, preview(db, workbook(machine=True, project=other.display_name, head_id="PRIVATE-HEAD", line_id="PRIVATE-LINE")))
    client = _stable_client(db, username="machine-reader", manage=False)
    _grant(db, project.project_id, "machine-reader")
    url = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    captured = []

    def record(_conn, _cursor, statement, parameters, _context, _executemany):
        if "FROM maintenance_doc_line_row" in statement:
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get(url, params={"page_size": 2})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == 3 and len(payload["items"]) == 2
        assert len(captured) == 1
        assert set(captured[0][1].values()) == {r["head_row_id"] for r in payload["items"]}
        assert all(len(r["components"]) == 1 for r in payload["items"])
        captured.clear()
        denied = client.get(f"/api/maintenance/projects/stable/{other.project_id}/return-receipts")
        assert denied.status_code == 403
        assert not captured
        empty_page = client.get(url, params={"page": 4, "page_size": 1})
        assert empty_page.json()["items"] == []
        assert not captured
    finally:
        event.remove(engine, "before_cursor_execute", record)
    summary = client.get(f"/api/maintenance/projects/stable/{project.project_id}/return-receipt-summary")
    assert summary.json()["project_total_qty"] == "3.000"


def test_machine_quantity_edit_cannot_count_its_components_as_more_machines(db, project):
    apply(db, preview(db, workbook(machine=True)))
    row = db.scalar(select(MaintenanceRkdReturnLine))
    with pytest.raises(ledger.ReturnReceiptValidation, match="固定记 1 台"):
        ledger.update_receipt(db, receipt_id=row.rkd_line_id, expected_version=1,
                              updates={"qty": 5, "note": "must not persist"},
                              reason="mistaken count", operated_by="tester")
    db.commit()
    db.refresh(row)
    assert row.qty == Decimal(1) and row.version == 1 and row.note is None
    ledger.update_receipt(db, receipt_id=row.rkd_line_id, expected_version=1,
                          updates={"qty": 1, "note": "source inspected"},
                          reason="inspect", operated_by="tester")
    db.commit()
    assert row.version == 2 and row.note == "source inspected"
    part = ledger.register_receipt(db, project_id=project.project_id, pn="MANUAL-PART",
                                   qty=2, operated_by="tester")
    db.commit()
    result = ledger.search_receipts(db, project_id=project.project_id, q="MANUAL-PART")
    assert result["items"][0]["receipt_id"] == part["receipt_id"]
    assert "components" not in result["items"][0]
