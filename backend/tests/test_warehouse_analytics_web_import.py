from datetime import date, datetime
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook
import pytest
from sqlalchemy import func, select

from app.etl.pipeline import successful_batch_ids_by_hash
from app.main import app
from app.models.system import SysImportBatch, SysUser
from app.models.warehouse_analytics import WarehouseAnalyticsDocument, WarehouseAnalyticsLine
from app.services.warehouse_analytics_import import SHEETS
from tests.boss_board_helpers import client_for


def _xlsx(*, invalid_sheet=False):
    book = Workbook()
    book.remove(book.active)
    for title, (source, head_fields, line_fields) in SHEETS.items():
        sheet = book.create_sheet(title)
        sheet.append([*head_fields.values(), *line_fields.values()])
        head = {
            "number": f"仓单-{source}",
            "source_id": source,
            "business_date": date(2026, 11, 1),
            "source_modified_at": datetime(2026, 11, 1, 13, 0),
            "status": "已生效",
        }
        line = {"source_id": source + "-1", "pn": "TEST-PN", "quantity": 1}
        sheet.append([*(head.get(key) for key in head_fields), *(line.get(key) for key in line_fields)])
    helper = book.create_sheet("Hidden_Options_1")
    helper.sheet_state = "veryHidden"
    if invalid_sheet:
        book.create_sheet("出库单")
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def _submit(client, blob):
    return client.post("/api/import/warehouse-workbook", files={
        "file": ("仓单.xlsx", blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    })


@pytest.fixture
def importer(db):
    return client_for(db, username="warehouse-importer", overrides={"page_import": True})


def test_web_job_imports_all_three_sources_and_rechecks_exact_replay(db, importer):
    blob = _xlsx()
    for index, (inserted, unchanged) in enumerate(((3, 0), (0, 3))):
        accepted = _submit(importer, blob)
        assert accepted.status_code == 202, accepted.text
        job = importer.get(f"/api/import/jobs/{accepted.json()['job_id']}").json()
        assert (job["status"], job["mode"], job["done_files"]) == ("done", "warehouse", 1)
        assert job["note"].count("1 张") == 3
        batch = job["batches"][0]
        assert batch["file_type"] == "warehouse"
        assert batch["status"] == ("success" if index == 0 else "skipped")
        report = importer.get(f"/api/import/batches/{batch['id']}").json()["report"]
        assert report["warehouse_sources"] == {title: {"documents": 1, "lines": 1} for title in SHEETS}
        assert report["warehouse_inserted"] == inserted
        assert report["warehouse_updated"] == 0
        assert report["warehouse_unchanged"] == unchanged

    assert db.scalar(select(func.count()).select_from(WarehouseAnalyticsDocument)) == 3
    assert db.scalar(select(func.count()).select_from(WarehouseAnalyticsLine)) == 3
    assert set(db.scalars(select(WarehouseAnalyticsDocument.source))) == {"inbound", "return", "direct"}
    fingerprint = db.scalar(select(SysImportBatch.file_hash).where(SysImportBatch.file_type == "warehouse").limit(1))
    assert not successful_batch_ids_by_hash(db, {fingerprint})


def test_unsupported_sheet_fails_whole_workbook_without_rows(db, importer):
    accepted = _submit(importer, _xlsx(invalid_sheet=True))
    assert accepted.status_code == 202, accepted.text
    job = importer.get(f"/api/import/jobs/{accepted.json()['job_id']}").json()
    assert job["status"] == "failed"
    assert "出库单" in job["note"]
    assert db.scalar(select(func.count()).select_from(WarehouseAnalyticsDocument)) == 0
    assert db.scalar(select(func.count()).select_from(WarehouseAnalyticsLine)) == 0


def test_warehouse_analytics_read_grant_does_not_authorize_web_upload(db):
    reader = client_for(db, username="warehouse-reader", overrides={"page_inventory_analytics": True})
    assert reader.get("/api/inventory/analytics/coverage").status_code == 200
    assert _submit(reader, _xlsx()).status_code == 403
    assert _submit(TestClient(app), _xlsx()).status_code == 401


def test_warehouse_web_import_requires_a_current_named_account(db, importer):
    account = db.scalar(select(SysUser).where(SysUser.username == "warehouse-importer"))
    db.delete(account)
    db.commit()
    assert _submit(importer, _xlsx()).status_code == 403
