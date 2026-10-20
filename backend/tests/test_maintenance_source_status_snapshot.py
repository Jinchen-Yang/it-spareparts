"""D-34: successful source uploads retain immutable header-status evidence."""
from copy import deepcopy
from datetime import datetime

import pytest
from app.etl import pipeline
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.system import SysImportBatch
from sqlalchemy import func, select

from tests.test_maintenance_wbdd_import_api import _client, _upload
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

SNAPSHOT_KEY = "_wbdd_source_status_snapshot"


def _run(db, tmp_path, rows, name, *, mode="upsert", columns=None):
    path = write_workbook(str(tmp_path / name), columns or COLUMNS_91, rows)
    return pipeline.run_import(db, path, name, uploaded_by="实名测试人", mode=mode)


def test_successful_upsert_records_applied_cancelled_header_evidence(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["数据状态"] = "已取消"
    path = write_workbook(str(tmp_path / "cancelled.xlsx"), COLUMNS_91, rows)

    batch = pipeline.run_import(
        db, path, "cancelled.xlsx", uploaded_by="实名测试人", mode="upsert",
    )

    assert batch.status == "success"
    snapshot = batch.report_json.get(SNAPSHOT_KEY)
    assert snapshot is not None, "成功 upsert 必须保留独立的不可变单头状态证据"
    assert snapshot["version"] == 1
    assert snapshot["mode"] == "upsert"
    assert snapshot["file_hash"] == batch.file_hash
    assert snapshot["batch_id"] == batch.id
    assert datetime.fromisoformat(snapshot["captured_at"]).tzinfo is not None
    assert snapshot["orders"] == [{
        "raw_order_id": "SYN-O001", "data_status": "已取消", "applied": True,
        "blocked_reasons": [],
    }]


@pytest.mark.parametrize("existing", [False, True])
def test_skip_upload_never_records_applied_cancellation(db, tmp_path, existing):
    rows = make_rows(orders=1, lines_per_order=1)
    if existing:
        _run(db, tmp_path, rows, "active.xlsx")
        db.commit()
    rows[0]["数据状态"] = "已取消"
    batch = _run(db, tmp_path, rows, "skip-cancelled.xlsx", mode="skip")
    assert batch.report_json[SNAPSHOT_KEY]["mode"] == "skip"
    assert batch.report_json[SNAPSHOT_KEY]["orders"] == [{
        "raw_order_id": "SYN-O001", "data_status": "已取消", "applied": False,
        "blocked_reasons": [],
    }]
    assert db.scalar(select(FMaintenanceOrder.data_status)) == ("已生效" if existing else "已取消")


def test_conflicting_header_statuses_are_not_explicit_cancellation_evidence(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=2)
    rows[0]["数据状态"] = "已取消"
    rows[1]["数据状态"] = "已生效"
    batch = _run(db, tmp_path, rows, "conflicting-status.xlsx")
    assert batch.report_json[SNAPSHOT_KEY]["orders"] == [{
        "raw_order_id": "SYN-O001", "data_status": None, "applied": False,
        "blocked_reasons": ["conflicting_header_status"],
    }]


def test_missing_status_column_is_recorded_as_unusable_evidence(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=1)
    batch = _run(db, tmp_path, rows, "missing-status.xlsx",
                 columns=[column for column in COLUMNS_91 if column != "数据状态"])
    assert batch.report_json[SNAPSHOT_KEY]["orders"] == [{
        "raw_order_id": "SYN-O001", "data_status": None, "applied": False,
        "blocked_reasons": ["missing_status_column"],
    }]


def test_duplicate_status_columns_do_not_create_successful_status_evidence(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["数据状态"] = "已取消"
    with pytest.raises(pipeline.ReaderError):
        _run(db, tmp_path, rows, "duplicate-status.xlsx", columns=COLUMNS_91 + ["数据状态"])
    db.rollback()
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0
    assert db.scalar(select(func.count()).select_from(FMaintenanceOrder)) == 0


@pytest.mark.parametrize("bad_field,bad_value", [
    ("需求明细.需供货产品", ""), ("需求明细.需求数量", "invalid"),
])
def test_rejected_children_do_not_block_applied_header_status(db, tmp_path, bad_field, bad_value):
    rows = make_rows(orders=1, lines_per_order=1)
    _run(db, tmp_path, rows, "valid.xlsx")
    db.commit()
    rows[0]["数据状态"] = "已取消"
    rows[0][bad_field] = bad_value
    batch = _run(db, tmp_path, rows, "cancelled-bad-child.xlsx")
    assert batch.report_json[SNAPSHOT_KEY]["orders"][0] == {
        "raw_order_id": "SYN-O001", "data_status": "已取消", "applied": True,
        "blocked_reasons": [],
    }
    assert db.scalar(select(FMaintenanceLine.is_active)) is True
    assert db.scalar(select(FMaintenanceLine.qty)) == 3


@pytest.mark.parametrize("status,expected", [
    ("草稿", "草稿"), ("进行中", "进行中"), ("", None),
    ("已作废", "已作废"), (" 已取消 ", "已取消"),
])
def test_evidence_preserves_source_status_without_inventing_terminal_meaning(db, tmp_path, status, expected):
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["数据状态"] = status
    batch = _run(db, tmp_path, rows, "source-status.xlsx")
    assert batch.report_json[SNAPSHOT_KEY]["orders"][0] == {
        "raw_order_id": "SYN-O001", "data_status": expected, "applied": True,
        "blocked_reasons": [],
    }


def test_reactivation_keeps_old_status_snapshots_immutable(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=1)
    original = _run(db, tmp_path, rows, "original.xlsx")
    db.commit()
    original_id = original.id
    original_snapshot = deepcopy(original.report_json[SNAPSHOT_KEY])
    rows[0]["数据状态"] = "已取消"
    cancelled = _run(db, tmp_path, rows, "cancelled.xlsx")
    db.commit()
    cancelled_id = cancelled.id
    cancelled_snapshot = deepcopy(cancelled.report_json[SNAPSHOT_KEY])
    rows[0]["数据状态"] = "已生效"
    restored = _run(db, tmp_path, rows, "restored.xlsx")
    db.commit()
    db.expire_all()
    assert db.get(SysImportBatch, original_id).report_json[SNAPSHOT_KEY] == original_snapshot
    assert db.get(SysImportBatch, cancelled_id).report_json[SNAPSHOT_KEY] == cancelled_snapshot
    assert restored.report_json[SNAPSHOT_KEY]["orders"][0]["data_status"] == "已生效"
    assert db.scalar(select(FMaintenanceOrder.data_status)) == "已生效"


def test_rollback_discards_new_status_evidence_with_imported_facts(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=1)
    original = _run(db, tmp_path, rows, "original.xlsx")
    db.commit()
    original_id = original.id
    rows[0]["数据状态"] = "已取消"
    pending = _run(db, tmp_path, rows, "cancelled-rollback.xlsx")
    pending_id = pending.id
    assert pending.report_json[SNAPSHOT_KEY]["orders"][0]["applied"] is True
    db.rollback()
    assert db.get(SysImportBatch, pending_id) is None
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 1
    assert db.scalar(select(FMaintenanceOrder.import_batch_id)) == original_id
    assert db.scalar(select(FMaintenanceOrder.data_status)) == "已生效"


def test_private_status_evidence_does_not_escape_upload_or_batch_reports(db, tmp_path):
    client = _client(db, username="source-status-report-admin", role="admin")
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["数据状态"] = "已取消"
    path = write_workbook(str(tmp_path / "private-evidence.xlsx"), COLUMNS_91, rows)
    response = _upload(client, path)
    assert response.status_code == 200, response.text
    assert SNAPSHOT_KEY not in response.json()
    batch_id = response.json()["batch_id"]
    history = client.get(f"/api/import/batches/{batch_id}")
    assert history.status_code == 200, history.text
    assert SNAPSHOT_KEY not in history.json()["report"]
    assert SNAPSHOT_KEY in db.get(SysImportBatch, batch_id).report_json
