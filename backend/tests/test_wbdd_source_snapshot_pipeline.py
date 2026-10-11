"""D-27: source membership survives validation, reimport, and public reporting."""
from copy import deepcopy
from datetime import datetime
import json

from openpyxl import load_workbook
import pytest
from sqlalchemy import select

from app.etl import pipeline
from app.models.maintenance import FMaintenanceLine
from app.models.system import SysImportBatch
from tests.test_maintenance_wbdd_import_api import _client, _upload
from tests.wbdd_fixtures import COLUMNS_90, COLUMNS_91, make_rows, write_workbook


PRIVATE_KEY = "_wbdd_source_snapshot"


def _rows(orders=2, lines=2):
    rows = make_rows(orders=orders, lines_per_order=lines)
    for index, row in enumerate(rows):
        row["需求单号"] = f"WBDD-20260609-{index // lines + 1:04d}"
    return rows


def _import(db, tmp_path, rows, *, name="source.xlsx", columns=COLUMNS_91, mode="upsert"):
    path = write_workbook(str(tmp_path / name), columns, rows)
    batch = pipeline.run_import(db, path, name, uploaded_by="snapshot-test", mode=mode)
    db.commit()
    db.refresh(batch)
    return batch, path


@pytest.mark.parametrize("columns", [COLUMNS_90, COLUMNS_91], ids=["legacy90", "current91"])
def test_pipeline_captures_source_rows_before_bad_pn_and_quantity_filtering(db, tmp_path, columns):
    rows = _rows(orders=3, lines=1)
    rows[0]["需求明细.需供货产品"] = None
    rows[1]["需求明细.需求数量"] = "invalid-quantity"
    batch, path = _import(db, tmp_path, rows, columns=columns)
    source = batch.report_json[PRIVATE_KEY]
    assert source["version"] == 1
    assert source["row_count"] == 3
    assert source["sheet_name"] == "维保备件需求单"
    assert source["mode"] == "upsert"
    assert source["file_hash"] == batch.file_hash == pipeline.sha256_file(path)
    assert datetime.fromisoformat(source["captured_at"]).utcoffset() is not None
    assert set(source["line_ids"]) == {"SYN-L001-1", "SYN-L002-1", "SYN-L003-1"}
    orders = {row["raw_order_id"]: row for row in source["orders"]}
    assert set(orders) == {"SYN-O001", "SYN-O002", "SYN-O003"}
    assert orders["SYN-O001"]["blocked_reasons"]
    assert orders["SYN-O002"]["blocked_reasons"]
    assert orders["SYN-O003"]["blocked_reasons"] == []
    assert len(list(db.scalars(select(FMaintenanceLine)))) == 1
    # Membership evidence carries identities only, never raw contacts or errors.
    serialized = json.dumps(source, ensure_ascii=False)
    assert "合成市合成路" not in serialized
    assert "invalid-quantity" not in serialized


def test_pipeline_capture_uses_reader_head_inheritance_and_does_not_mutate_prior_batch(db, tmp_path):
    rows = _rows(orders=1, lines=2)
    for key in list(rows[1]):
        if not key.startswith("需求明细."):
            rows[1][key] = None
    first, _ = _import(db, tmp_path, rows, name="first.xlsx")
    frozen = deepcopy(first.report_json[PRIVATE_KEY])
    assert len(frozen["orders"]) == 1
    assert set(frozen["orders"][0]["line_ids"]) == {"SYN-L001-1", "SYN-L001-2"}
    assert frozen["orders"][0]["row_numbers"] == [1, 2]
    rows[0]["需求明细.需求数量"] = "9"
    second, _ = _import(db, tmp_path, rows, name="second.xlsx")
    assert second.id != first.id
    db.expire_all()
    assert db.get(SysImportBatch, first.id).report_json[PRIVATE_KEY] == frozen
    assert db.scalar(select(FMaintenanceLine).where(FMaintenanceLine.raw_line_id == "SYN-L001-1")).import_batch_id == second.id


def test_pipeline_marks_ignored_wbdd_page_without_claiming_its_rows_were_processed(db, tmp_path):
    path = write_workbook(str(tmp_path / "two-pages.xlsx"), COLUMNS_91, _rows(orders=1, lines=1))
    workbook = load_workbook(path)
    ignored = workbook.copy_worksheet(workbook.active)
    ignored.title = "第二张维保需求"
    workbook.save(path)
    batch = pipeline.run_import(db, path, "two-pages.xlsx", mode="upsert")
    db.commit()
    source = batch.report_json[PRIVATE_KEY]
    assert source["sheet_name"] == "维保备件需求单"
    assert source["row_count"] == 1
    assert source["ignored_wbdd_sheets"] == ["第二张维保需求"]
    assert source["blocked_reasons"]
    assert batch.report_json["fact_rows_inserted"] == 1


def test_address_conflict_does_not_make_source_membership_incomplete(db, tmp_path):
    rows = _rows(orders=1, lines=2)
    rows[1]["收货地址"] = "合成另一地址"
    batch, _ = _import(db, tmp_path, rows)
    assert batch.report_json["address_report"]["conflict"] == 1
    source = batch.report_json[PRIVATE_KEY]
    assert source["blocked_reasons"] == []
    assert source["orders"][0]["blocked_reasons"] == []
    assert set(source["orders"][0]["line_ids"]) == {"SYN-L001-1", "SYN-L001-2"}


@pytest.mark.parametrize("route", ["generic", "dedicated"])
def test_api_upload_and_batch_history_keep_source_membership_private(db, tmp_path, route):
    client = _client(db, username="source-private-" + route, role="admin")
    path = write_workbook(str(tmp_path / (route + ".xlsx")), COLUMNS_91, _rows(orders=1, lines=1))
    if route == "generic":
        with open(path, "rb") as content:
            response = client.post("/api/import/upload?mode=upsert", files={"file": ("source.xlsx", content)})
    else:
        response = _upload(client, path)
    assert response.status_code == 200, response.text
    body = response.json()
    batch_id = body["batch_id"]
    batch = db.get(SysImportBatch, batch_id)
    assert PRIVATE_KEY in batch.report_json
    assert PRIVATE_KEY not in json.dumps(body)
    for url in ("/api/import/batches", f"/api/import/batches/{batch_id}"):
        history = client.get(url)
        assert history.status_code == 200, history.text
        assert PRIVATE_KEY not in history.text
    assert PRIVATE_KEY in db.get(SysImportBatch, batch_id).report_json


def test_skip_mode_records_present_source_identity_without_claiming_an_update(db, tmp_path):
    rows = _rows(orders=1, lines=1)
    _, _ = _import(db, tmp_path, rows, name="existing.xlsx")
    rows[0]["需求明细.需求数量"] = "9"
    batch, _ = _import(db, tmp_path, rows, name="skip.xlsx", mode="skip")
    assert batch.report_json[PRIVATE_KEY]["mode"] == "skip"
    assert batch.report_json[PRIVATE_KEY]["line_ids"] == ["SYN-L001-1"]
    assert db.scalar(select(FMaintenanceLine)).qty == 3
