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
from tests.test_warehouse_analytics_import import CKD_CODES, CKD_HEADERS, ckd_row
from tests.boss_board_helpers import client_for


def _xlsx(*, invalid_sheet=False, ckd_quantity=2, inbound_number="仓单-inbound", inbound_id="inbound"):
    book = Workbook()
    book.remove(book.active)
    for title in ("入库单", "返库单", "直采直退"):
        source, head_fields, line_fields = SHEETS[title]
        sheet = book.create_sheet("入库单号" if source == "inbound" else title)
        sheet.append([*head_fields.values(), *line_fields.values()])
        head = {
            "number": inbound_number if source == "inbound" else f"仓单-{source}",
            "source_id": inbound_id if source == "inbound" else source,
            "business_date": date(2026, 11, 1),
            "source_modified_at": datetime(2026, 11, 1, 13, 0),
            "status": "已生效",
        }
        line = {"source_id": head["source_id"] + "-1", "pn": "TEST-PN", "quantity": 1}
        sheet.append([*(head.get(key) for key in head_fields), *(line.get(key) for key in line_fields)])
    ckd = book.create_sheet("出库_发货单")
    ckd.append(CKD_CODES)
    ckd.append(CKD_HEADERS)
    ckd.append(ckd_row(quantity=ckd_quantity))
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


def test_web_job_imports_all_four_sources_and_rechecks_exact_replay(db, importer):
    blob = _xlsx()
    for index, (inserted, unchanged) in enumerate(((4, 0), (0, 4))):
        accepted = _submit(importer, blob)
        assert accepted.status_code == 202, accepted.text
        job = importer.get(f"/api/import/jobs/{accepted.json()['job_id']}").json()
        assert (job["status"], job["mode"], job["done_files"]) == ("done", "warehouse", 1)
        assert job["note"].count("1 张") == 4
        batch = job["batches"][0]
        assert batch["file_type"] == "warehouse"
        assert batch["status"] == ("success" if index == 0 else "skipped")
        report = importer.get(f"/api/import/batches/{batch['id']}").json()["report"]
        assert report["warehouse_sources"] == {title: {"documents": 1, "lines": 1} for title in ("入库单号", "返库单", "直采直退", "出库_发货单")}
        assert report["warehouse_inserted"] == inserted
        assert report["warehouse_updated"] == 0
        assert report["warehouse_unchanged"] == unchanged

    assert db.scalar(select(func.count()).select_from(WarehouseAnalyticsDocument)) == 4
    assert db.scalar(select(func.count()).select_from(WarehouseAnalyticsLine)) == 4
    assert set(db.scalars(select(WarehouseAnalyticsDocument.source))) == {"inbound", "return", "direct", "outbound"}
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


def test_ckd_version_conflict_rolls_back_an_earlier_sheet(db, importer):
    assert _submit(importer, _xlsx()).status_code == 202
    # The earlier inbound insert must roll back when the CKD conflict is found.
    accepted = _submit(importer, _xlsx(ckd_quantity=4, inbound_number="新入库单", inbound_id="new-inbound"))
    assert accepted.status_code == 202
    job = importer.get(f"/api/import/jobs/{accepted.json()['job_id']}").json()
    assert job["status"] == "failed"
    assert "同一源修改时间" in job["note"]
    assert "新入库单" not in set(db.scalars(select(WarehouseAnalyticsDocument.number)))
    assert db.scalar(select(func.count()).select_from(WarehouseAnalyticsLine)) == 4


def test_imported_ckd_is_visible_by_day_week_picker_and_warehouse(db, importer):
    assert _submit(importer, _xlsx()).status_code == 202
    reader = client_for(db, username="warehouse-reader", overrides={"page_inventory_analytics": True})
    root = "/api/inventory/analytics"
    params = {"source": "outbound"}
    coverage = reader.get(root + "/coverage").json()
    assert coverage["outbound_warehouses"] == [{
        "center": "合成中心", "warehouse": "单头出库仓", "days": ["2026-11-01"],
    }]
    for grain, expected in (("day", "2026-11-01"), ("week", "2026-10-26")):
        cube = reader.get(root + "/cube", params={**params, "row": grain}).json()
        assert [(row["key"], row["value"]) for row in cube["rows"]] == [(expected, 1)]
    rank = reader.get(root + "/rankings", params={"operator_role": "outbound"}).json()
    assert [(row["key"], row["documents"]) for row in rank["items"]] == [("出库拣货甲", 1)]
    doc, = reader.get(root + "/documents", params=params).json()["items"]
    scoped = reader.get(root + "/documents", params={"scope": '{"source":"outbound","outbound_warehouse":"单头出库仓"}'})
    assert scoped.status_code == 200
    assert [row["id"] for row in scoped.json()["items"]] == [doc["id"]]
    assert (doc["number"], doc["operator"], doc["outbound_warehouse"]) == (
        "CKD-20261101-0001", "出库拣货甲", "单头出库仓",
    )
    detail, = reader.get(root + f"/documents/{doc['id']}/lines", params=params).json()["items"]
    assert (detail["pn"], float(detail["quantity"]), detail["unit"], detail["warehouse"], detail["bin_name"]) == (
        "CKD-TEST-PN", 2, "件", "备件所在仓", "A-1",
    )
    equipment, = reader.get(root + "/equipment", params=params).json()["items"]
    assert (equipment["whole_pn"], float(equipment["whole_qty"]), equipment["whole_unit"]) == ("整机-TEST", 1, "台")
