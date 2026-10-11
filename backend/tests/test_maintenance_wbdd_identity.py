"""D-27 identity guard rejects whole imports, without blocking valid edits."""
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.etl import loader, pipeline
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.system import SysImportBatch
from tests.test_maintenance_wbdd_import_api import _client, _upload
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook


def _rows(count=1):
    rows = make_rows(orders=count, lines_per_order=1)
    for number, row in enumerate(rows, start=1):
        row["需求单号"] = f"WBDD-20260609-{number:04d}"
    return rows


def _file(tmp_path, name, rows):
    return write_workbook(str(tmp_path / name), COLUMNS_91, rows)


def _seed(db, tmp_path):
    rows = _rows()
    path = _file(tmp_path, "initial.xlsx", rows)
    batch = pipeline.run_import(db, path, "initial.xlsx", mode="upsert")
    db.commit()
    return rows, batch.id


def test_existing_line_cannot_move_to_another_parent_and_entire_import_rolls_back(db, tmp_path):
    rows, original_batch = _seed(db, tmp_path)
    incoming = _rows(2)
    incoming[0]["需求明细.需求数量"] = "9"
    incoming[0]["需求明细.数据ID(不可修改)"] = "NEW-LEGITIMATE-LINE"
    incoming[1]["需求明细.数据ID(不可修改)"] = rows[0]["需求明细.数据ID(不可修改)"]
    path = _file(tmp_path, "moved.xlsx", incoming)
    with pytest.raises(loader.ImportIntegrityError, match=r"WBDD-20260609-0002（数据行 2）.*另一张单") as exc:
        pipeline.run_import(db, path, "moved.xlsx", mode="upsert")
    assert "合成收货人" not in str(exc.value)
    assert "SYN-L" not in str(exc.value)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(FMaintenanceOrder)) == 1
    assert db.scalar(select(func.count()).select_from(FMaintenanceLine)) == 1
    line = db.scalar(select(FMaintenanceLine))
    assert line.qty == 3 and line.import_batch_id == original_batch


def test_dedicated_api_rejects_same_order_number_with_new_header_id_atomically(db, tmp_path):
    rows, original_batch = _seed(db, tmp_path)
    incoming = _rows()
    incoming[0]["数据ID(不可修改)"] = "DIFFERENT-SOURCE-HEADER"
    incoming[0]["需求明细.数据ID(不可修改)"] = "DIFFERENT-SOURCE-LINE"
    path = _file(tmp_path, "renamed-id.xlsx", incoming)
    client = _client(db, username="identity-uploader", role="admin")
    response = _upload(client, path)
    assert response.status_code == 422
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(FMaintenanceOrder)) == 1
    assert db.scalar(select(func.count()).select_from(FMaintenanceLine)) == 1
    assert db.scalar(select(FMaintenanceLine)).import_batch_id == original_batch
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 1


def test_same_identity_and_parent_can_update_quantity_and_reorder_line_number(db, tmp_path):
    rows, original_batch = _seed(db, tmp_path)
    rows[0]["需求明细.需求数量"] = "7.25"
    rows[0]["需求明细.序号"] = "42"
    path = _file(tmp_path, "quantity.xlsx", rows)
    batch = pipeline.run_import(db, path, "quantity.xlsx", mode="upsert")
    db.commit()
    db.expire_all()
    line = db.scalar(select(FMaintenanceLine))
    assert line.qty == Decimal("7.25")
    assert line.line_no == 42
    assert line.import_batch_id == batch.id != original_batch
    assert db.scalar(select(FMaintenanceOrder)).raw_order_id == "SYN-O001"


def test_invalid_line_does_not_invent_a_parent_move(db, tmp_path):
    rows, original_batch = _seed(db, tmp_path)
    incoming = _rows()
    incoming[0]["需求单号"] = "WBDD-20260609-0002"
    incoming[0]["数据ID(不可修改)"] = "SECOND-HEADER"
    incoming[0]["需求明细.需求数量"] = "not-a-quantity"
    path = _file(tmp_path, "invalid-line.xlsx", incoming)
    batch = pipeline.run_import(db, path, "invalid-line.xlsx", mode="upsert")
    db.commit()
    db.expire_all()
    # The header exists independently, but the rejected child must remain on its
    # original parent and still count as present in the private source snapshot.
    line = db.scalar(select(FMaintenanceLine))
    assert line.import_batch_id == original_batch
    assert db.get(FMaintenanceOrder, line.order_id).raw_order_id == "SYN-O001"
    assert batch.report_json["_wbdd_source_snapshot"]["orders"][0]["blocked_reasons"]


def test_same_raw_header_cannot_hide_two_order_numbers_behind_first_row(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=2)
    rows[0]["需求单号"] = "WBDD-20260609-0001"
    rows[1]["需求单号"] = "WBDD-20260609-0002"
    path = _file(tmp_path, "conflicting-numbers.xlsx", rows)
    with pytest.raises(loader.ImportIntegrityError, match=r"数据行 1、2.*身份冲突"):
        pipeline.run_import(db, path, "conflicting-numbers.xlsx", mode="upsert")
    db.rollback()
    assert db.scalar(select(func.count()).select_from(FMaintenanceOrder)) == 0
    assert db.scalar(select(func.count()).select_from(FMaintenanceLine)) == 0
