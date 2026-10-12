"""D-28 entry uses the frozen cross-project receipt protocol, never raw writes."""
from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project_operations import MaintenanceCollectionReceipt
from app.models.system import SysImportBatch, SysAccessLog
from app.services import maintenance_bulk_import as bulk
from tests.boss_board_helpers import client_for
from tests.test_maintenance_receipt_ledger_import import ORDER_NO, _project_with_contract, _receipt_xlsx, _preview, _apply

BASE = "/api/maintenance/project-batch-transfer"


def test_receipt_entry_previews_instalments_without_recording_money(db):
    project, contract = _project_with_contract(db)
    for sequence in (1, 2):
        db.add(MaintenanceCollectionMilestone(
            milestone_id=str(uuid4()), project_id=project.project_id,
            project_contract_id=contract.project_contract_id, sequence=sequence,
            planned_date=date(2026, sequence, 1), planned_amount=Decimal("100"),
            completeness_state="complete", source="direct_api"))
    db.commit()
    client = client_for(db, username="receipt-entry-admin", role="admin")
    data = _receipt_xlsx([(ORDER_NO, "SKD-ENTRY", "2026-03-01", "150")])
    response = client.post(f"{BASE}/preview", data={"import_kind": "receipt"},
        files={"files": ("收款单.xlsx", data)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"][0]["source_filter"]["maintenance_rows"] == 1
    progress = body["collection_progress"][0]
    assert progress["before"]["cumulative_received"] is None
    after = progress["candidates"][0]["after"]["milestones"]
    assert [(item["payment_state"], item["received_for_period"]) for item in after] == [
        ("paid", "100.00"), ("partial", "50.00")]
    assert db.scalar(select(func.count()).select_from(MaintenanceCollectionReceipt)) == 0
    assert client.get(f"{BASE}/options").json()["max_file_bytes"] == 32 * 1024 * 1024
    assert db.scalar(select(func.count()).select_from(SysAccessLog).where(
        SysAccessLog.action == "maintenance_batch_preview",
        SysAccessLog.username == "receipt-entry-admin")) == 1


def test_receipt_only_entry_rejects_sales_form_before_freezing_a_preview(db):
    client = client_for(db, username="receipt-entry-kind", role="admin")
    workbook = Workbook()
    workbook.active.append(["订单编号(必填)", "订单金额", "是否含税(必填)", "税率(必填)"])
    workbook.active.append(["XSDD-20260301-0001", "100", "含税", "0"])
    buffer = BytesIO(); workbook.save(buffer)
    before = db.scalar(select(func.count()).select_from(SysImportBatch))
    response = client.post(f"{BASE}/preview", data={"import_kind": "receipt"},
        files={"files": ("销售.xlsx", buffer.getvalue())})
    assert response.status_code == 422, response.text
    assert "维保收款入口只接受收款单原件" in response.text
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == before


def test_preview_checks_total_size_while_reading(db, monkeypatch):
    from app.api import maintenance_project_batch_transfer as endpoint
    from app.services import import_safety
    client = client_for(db, username="receipt-entry-resource", role="admin")
    monkeypatch.setattr(endpoint.bulk, "MAX_TRANSFER_TOTAL_BYTES", 5)
    monkeypatch.setattr(import_safety, "validate_xlsx_zip", lambda *_a, **_k: None)
    called = []
    monkeypatch.setattr(endpoint.bulk, "preview_transfer", lambda *_a, **_k: called.append(True))
    response = client.post(f"{BASE}/preview", files=[
        ("files", ("one.xlsx", b"1234")), ("files", ("two.xlsx", b"1234"))])
    assert response.status_code == 422
    assert called == []


@pytest.mark.parametrize("scope_kind", ["empty", "other"])
def test_known_receipts_still_require_project_scope_before_exposing_finances(db, scope_kind):
    permitted, _ = _project_with_contract(db, contract_no="XSDD-20990101-0001")
    forbidden, contract = _project_with_contract(db, contract_no="XSDD-20990101-0002")
    db.add(MaintenanceCollectionMilestone(
        milestone_id=str(uuid4()), project_id=forbidden.project_id,
        project_contract_id=contract.project_contract_id, sequence=1,
        planned_date=date(2026, 1, 1), planned_amount=Decimal("100"),
        completeness_state="complete", source="direct_api"))
    db.commit()
    data = _receipt_xlsx([(contract.contract_no, "SKD-SCOPE", "2026-01-10", "25")])
    initial = _preview(db, data)
    _apply(db, initial, [row["row_key"] for row in initial["rows"] if row["row_status"] == "ready"])
    before = db.scalar(select(func.count()).select_from(SysImportBatch))
    allowed = set() if scope_kind == "empty" else {permitted.project_id}
    with pytest.raises(bulk.BulkImportScopeDenied):
        bulk.preview_transfer(db, [("known.xlsx", data)], operated_by="scoped-reviewer",
            allowed_project_ids=allowed, import_kind="receipt")
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == before
    own = bulk.preview_transfer(db, [("known.xlsx", data)], operated_by="scoped-reviewer",
        allowed_project_ids={forbidden.project_id}, import_kind="receipt")
    assert own["summary"]["ready"] == 0
    assert own["collection_progress"][0]["before"]["cumulative_received"] == "25.00"


def test_batch_download_records_access_audit_without_a_schema_change(db):
    _project_with_contract(db)
    client = client_for(db, username="receipt-download-admin", role="admin")
    response = client.post(f"{BASE}/download", json={
        "forms": ["project_master"], "fields": ["project_code"]})
    assert response.status_code == 200, response.text
    assert db.scalar(select(func.count()).select_from(SysAccessLog).where(
        SysAccessLog.action == "maintenance_batch_download",
        SysAccessLog.username == "receipt-download-admin")) == 1
