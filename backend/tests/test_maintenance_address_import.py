"""D-26: real pipeline/API contracts, using synthetic business rows only."""
import json
import uuid

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.etl import pipeline
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.system import SysAuditLog, SysImportError
from app.services import maintenance_project_operations as ops
from tests.test_maintenance_wbdd_import_api import _client, _upload
from tests.test_maintenance_workbook_revision_invalidation import _make_project, _state
from tests.wbdd_fixtures import COLUMNS_90, COLUMNS_91, make_rows, write_workbook


def _run(db, tmp_path, rows, *, columns=COLUMNS_91, mode="upsert", single_header=False):
    path = write_workbook(str(tmp_path / f"{uuid.uuid4()}.xlsx"), columns, rows)
    if single_header:
        workbook = load_workbook(path)
        workbook.active.delete_rows(1)
        workbook.save(path)
    batch = pipeline.run_import(db, path, "synthetic-address.xlsx", mode=mode,
                                uploaded_by="address-test")
    db.commit()
    db.expire_all()
    return batch


def _orders(db):
    return {o.raw_order_id: o for o in db.scalars(select(FMaintenanceOrder))}


@pytest.mark.parametrize("columns", [COLUMNS_90, COLUMNS_91], ids=["old90", "current91"])
@pytest.mark.parametrize("position", [0, 1, 25, 90], ids=["first", "B", "detail", "last"])
@pytest.mark.parametrize("route", ["generic", "dedicated"])
def test_moved_address_reads_by_name_and_keeps_detail_identity(
    db, tmp_path, columns, position, route
):
    columns = [c for c in columns if c != "收货地址"]
    columns.insert(position, "收货地址")
    # Reproduce the real export: a head quantity can occur at index 7.
    columns.remove("需求数量")
    columns.insert(7, "需求数量")
    rows = make_rows(orders=2, lines_per_order=2)
    rows[0]["收货地址"] = rows[1]["收货地址"] = "合成市长地址路" * 60 + "\n1号楼"
    rows[2]["收货地址"] = rows[3]["收货地址"] = "合成乙市2号"
    if route == "dedicated":
        client = _client(db, username="address-uploader", overrides={
            "page_maintenance": True, "action_maintenance_wbdd_import": True})
        path = write_workbook(str(tmp_path / "moved.xlsx"), columns, rows)
        response = _upload(client, path)
        assert response.status_code == 200, response.text
        assert response.json()["address_report"]["set"] == 2
    else:
        batch = _run(db, tmp_path, rows, columns=columns)
        assert batch.report_json["address_report"]["set"] == 2
    orders = _orders(db)
    assert len(orders) == 2
    for number in (1, 2):
        order = orders[f"SYN-O{number:03d}"]
        assert order.receiver_address == rows[(number - 1) * 2]["收货地址"]
        assert order.delivery_address_option == "默认地址"
        assert order.head_demand_qty == 3
    lines = list(db.scalars(select(FMaintenanceLine)))
    assert len(lines) == 4
    for line in lines:
        number, sequence = line.raw_line_id.removeprefix("SYN-L").split("-")
        assert line.order_id == orders[f"SYN-O{number}"].id
        assert line.pn_raw == f"PN-SYN-{number}{sequence}"
        assert line.qty == 3 and line.serial_numbers == "SN001"


@pytest.mark.parametrize("header", ["地址", "地址（选填）", "收货地址(选填)"])
@pytest.mark.parametrize("single_header", [False, True])
def test_address_alias_and_first_blank_row_do_not_inherit_other_order(
    db, tmp_path, header, single_header
):
    columns = [header if c == "收货地址" else c for c in COLUMNS_91]
    rows = make_rows(orders=2, lines_per_order=2)
    for row in rows:
        row[header] = None
    rows[1][header] = "合成续行地址"
    batch = _run(db, tmp_path, rows, columns=columns, single_header=single_header)
    orders = _orders(db)
    assert orders["SYN-O001"].receiver_address == "合成续行地址"
    assert orders["SYN-O002"].receiver_address is None
    assert batch.report_json["address_report"] == {
        "mode": "upsert", "set": 1, "keep": 1, "clear": 0, "conflict": 0, "warnings": []}


@pytest.mark.parametrize("missing_column", [False, True])
@pytest.mark.parametrize("route", ["generic", "dedicated"])
def test_blank_or_missing_address_retains_existing_value_and_updates_quantity(
    db, tmp_path, missing_column, route
):
    rows = make_rows(orders=1, lines_per_order=1)
    _run(db, tmp_path, rows)
    columns = [c for c in COLUMNS_91 if not (missing_column and c == "收货地址")]
    rows[0].update({"收货地址": "  ", "需求明细.需求数量": "7"})
    if route == "dedicated":
        client = _client(db, username="address-legacy", overrides={
            "page_maintenance": True, "action_maintenance_wbdd_import": True})
        path = write_workbook(str(tmp_path / "legacy.xlsx"), columns, rows)
        response = _upload(client, path)
        assert response.status_code == 200, response.text
        assert response.json()["address_report"]["keep"] == 1
    else:
        _run(db, tmp_path, rows, columns=columns)
    assert _orders(db)["SYN-O001"].receiver_address == "合成市合成路1号"
    assert db.scalar(select(FMaintenanceLine)).qty == 7


def test_skip_mode_keeps_existing_address_and_quantity(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=1)
    _run(db, tmp_path, rows)
    rows[0].update({"收货地址": "合成新地址", "需求明细.需求数量": "7"})
    batch = _run(db, tmp_path, rows, mode="skip")
    assert _orders(db)["SYN-O001"].receiver_address == "合成市合成路1号"
    assert db.scalar(select(FMaintenanceLine)).qty == 3
    assert batch.report_json["address_report"]["mode"] == "skip"


def test_explicit_operation_only_changes_address_and_literal_clear_is_an_address(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=2)
    _run(db, tmp_path, rows)
    # The operation is explicit, even when the old value is still in the export.
    rows[0]["地址操作"] = "清空"
    batch = _run(db, tmp_path, rows, columns=COLUMNS_91 + ["地址操作"])
    assert _orders(db)["SYN-O001"].receiver_address is None
    assert batch.report_json["address_report"]["clear"] == 1
    assert all(line.qty == 3 for line in db.scalars(select(FMaintenanceLine)))
    for row in rows:
        row.pop("地址操作", None)
        row["收货地址"] = "清空"
    _run(db, tmp_path, rows)
    assert _orders(db)["SYN-O001"].receiver_address == "清空"


@pytest.mark.parametrize("operation", [None, "清空", "保留"])
def test_conflicting_addresses_keep_original_other_order_and_quantity_continue(
    db, tmp_path, operation
):
    rows = make_rows(orders=2, lines_per_order=2)
    _run(db, tmp_path, rows)
    rows[0]["收货地址"] = "合成冲突地址甲"
    rows[1]["收货地址"] = "合成冲突地址乙"
    rows[0]["地址操作"] = operation
    rows[0]["需求明细.需求数量"] = "7"
    rows[2]["收货地址"] = rows[3]["收货地址"] = "合成另一单新地址"
    batch = _run(db, tmp_path, rows, columns=COLUMNS_91 + ["地址操作"])
    orders = _orders(db)
    assert orders["SYN-O001"].receiver_address == "合成市合成路1号"
    assert orders["SYN-O002"].receiver_address == "合成另一单新地址"
    line = db.scalar(select(FMaintenanceLine).where(FMaintenanceLine.raw_line_id == "SYN-L001-1"))
    assert line.qty == 7
    report = batch.report_json["address_report"]
    assert report["conflict"] == 1 and report["set"] == 1
    assert batch.report_json["fact_rows_error"] == 0
    warning = report["warnings"][0]
    assert warning["order_no"] == "WBDD-20260001" and warning["data_rows"] == [1, 2]
    assert "原地址保留" in warning["message"]
    error = db.scalar(select(SysImportError).where(SysImportError.batch_id == batch.id))
    assert error.error_type == "maintenance_address_conflict"
    assert "合成冲突地址" not in json.dumps(error.raw_row, ensure_ascii=False)
    assert "合成冲突地址" not in json.dumps(batch.report_json, ensure_ascii=False)


@pytest.mark.parametrize("same_value", [False, True])
def test_address_aliases_are_compared_instead_of_silently_choosing_one(db, tmp_path, same_value):
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["地址"] = rows[0]["收货地址"] if same_value else "合成冲突地址"
    batch = _run(db, tmp_path, rows, columns=COLUMNS_91 + ["地址"])
    order = _orders(db)["SYN-O001"]
    assert order.receiver_address == ("合成市合成路1号" if same_value else None)
    assert batch.report_json["address_report"]["conflict"] == (0 if same_value else 1)


@pytest.mark.parametrize("operations", [("错误操作", ""), ("清空", "设置"), ("设置", "")])
def test_invalid_or_conflicting_operations_retain_existing_address(db, tmp_path, operations):
    rows = make_rows(orders=1, lines_per_order=2)
    _run(db, tmp_path, rows)
    for row, operation in zip(rows, operations):
        row.update({"收货地址": None, "地址操作": operation})
    batch = _run(db, tmp_path, rows, columns=COLUMNS_91 + ["地址操作"])
    assert _orders(db)["SYN-O001"].receiver_address == "合成市合成路1号"
    assert batch.report_json["address_report"]["conflict"] == 1


def test_address_change_invalidates_workbook_and_audits_but_keep_does_not(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=1)
    _run(db, tmp_path, rows)
    project = _make_project(db, tag="ADDR", source_order_id="SYN-O001")
    ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()
    rows[0]["收货地址"] = None
    batch = _run(db, tmp_path, rows)
    assert _state(db, project.project_id).revision == 0
    assert batch.report_json["workbook_projects_bumped"] == 0
    rows[0]["收货地址"] = "合成新地址"
    batch = _run(db, tmp_path, rows)
    assert _state(db, project.project_id).revision == 1
    assert batch.report_json["workbook_projects_bumped"] == 1
    audit = db.scalar(select(SysAuditLog).where(
        SysAuditLog.entity_type == "import_overwrite",
        SysAuditLog.after_json["receiver_address"].astext == "合成新地址"))
    assert audit.before_json["receiver_address"] == "合成市合成路1号"
    rows[0]["地址操作"] = "清空"
    _run(db, tmp_path, rows, columns=COLUMNS_91 + ["地址操作"])
    assert _state(db, project.project_id).revision == 2


def test_keep_and_set_operations_apply_to_their_own_order(db, tmp_path):
    rows = make_rows(orders=2, lines_per_order=1)
    _run(db, tmp_path, rows)
    rows[0].update({"地址操作": "保留", "收货地址": "合成忽略新地址"})
    rows[1].update({"地址操作": "设置", "收货地址": "合成采用新地址"})
    batch = _run(db, tmp_path, rows, columns=COLUMNS_91 + ["地址操作"])
    orders = _orders(db)
    assert orders["SYN-O001"].receiver_address == "合成市合成路1号"
    assert orders["SYN-O002"].receiver_address == "合成采用新地址"
    assert batch.report_json["address_report"]["keep"] == 1
    assert batch.report_json["address_report"]["set"] == 1


def test_dedicated_conflict_response_and_idempotent_replay(db, tmp_path):
    rows = make_rows(orders=2, lines_per_order=2)
    rows[0]["收货地址"] = "合成冲突甲"
    rows[1]["收货地址"] = "合成冲突乙"
    client = _client(db, username="address-conflict-api", role="admin", overrides={
        "page_maintenance": True, "action_maintenance_wbdd_import": True})
    path = write_workbook(str(tmp_path / "conflict.xlsx"), COLUMNS_91, rows)
    response = _upload(client, path, key="address-contract-replay")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["address_report"]["conflict"] == 1
    assert body["address_report"]["warnings"][0]["data_rows"] == [1, 2]
    assert body["fact_rows_error"] == 0
    assert "合成冲突" not in response.text
    replay = _upload(client, path, key="address-contract-replay")
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["address_report"] == body["address_report"]
    assert len(_orders(db)) == 2
    detail = client.get(f"/api/import/batches/{body['batch_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["issue_count"] == 1
    assert detail.json()["errors"][0]["nature"] == "提示"
    download = client.get(f"/api/import/batches/{body['batch_id']}/errors.csv")
    assert download.status_code == 200, download.text
    assert "合成冲突" not in download.text and "WBDD-20260001" in download.text
