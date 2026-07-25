import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select

from app import config
from app.api import imports as imports_api
from app.auth import hash_password
from app.config import get_settings
from app.etl import pipeline, reader, sheet_selection
from app.main import app
from app.models.inventory import Inventory
from app.models.system import SysImportBatch, SysRawFile, SysUser


_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PURCHASE_HEADER = [
    "采购单号(必填)",
    "数据ID(不可修改)",
    "明细.数据ID(不可修改)",
    "明细.产品名称(必填)",
    "明细.单价(必填)",
]
_PURCHASE_ROW = ["CGDD-1", "PO-1", "PL-1", "PN-PURCHASE", 100]
_INVENTORY_HEADER = ["产品库存ID", "产品名称(PN)", "库存数量", "仓库"]
_EXPENSE_HEADER = ["报销日期", "费用分类", "报销金额"]


@pytest.fixture()
def import_client(db):
    db.add(SysUser(
        username="precheck-admin",
        role="admin",
        display_name="预检管理员",
        password_hash=hash_password("adminpw"),
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "precheck-admin", "password": "adminpw"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _workbook_bytes(sheets: list[tuple[str, list[list[object]]]]) -> bytes:
    workbook = Workbook()
    for index, (sheet_name, rows) in enumerate(sheets):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        sheet.title = sheet_name
        for row in rows:
            sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _precheck(client: TestClient, payload: bytes, filename: str = "input.xlsx"):
    response = client.post(
        "/api/import/precheck",
        files=[("files", (filename, payload, _XLSX_CONTENT_TYPE))],
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sheet(result: dict, name: str) -> dict:
    return next(sheet for sheet in result["sheets"] if sheet["sheet_name"] == name)


def test_precheck_single_purchase_keeps_v1_fields_and_adds_v2_contract(db, import_client):
    payload = _workbook_bytes([("采购", [_PURCHASE_HEADER, _PURCHASE_ROW])])

    result = _precheck(import_client, payload)
    file_result = result["files"][0]

    assert {
        "filename", "file_type", "ok", "missing_price", "warning",
        "can_import", "severity", "selected_sheets", "sheets", "issues",
    } <= file_result.keys()
    assert {"any_warning", "missing_price_any", "has_errors", "can_import_all"} <= result.keys()
    assert file_result["file_type"] == "purchase"
    assert file_result["ok"] is True
    assert file_result["missing_price"] is False
    assert file_result["warning"] is None
    assert file_result["can_import"] is True
    assert file_result["severity"] == "info"
    assert file_result["selected_sheets"] == ["采购"]
    assert file_result["issues"] == []
    assert file_result["sheets"] == [{
        "sheet_name": "采购",
        "detected_type": "purchase",
        "action": "selected",
        "header_row": 1,
        "data_rows": 1,
        "duplicate_headers": [],
        "issues": [],
    }]
    assert result["any_warning"] is False
    assert result["missing_price_any"] is False
    assert result["has_errors"] is False
    assert result["can_import_all"] is True
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0
    assert db.scalar(select(func.count()).select_from(SysRawFile)) == 0


def test_precheck_recognizes_sales_with_bare_business_type(import_client):
    payload = _workbook_bytes([(
        "销售",
        [
            [
                "订单编号(必填)",
                "业务类型",
                "数据ID(不可修改)",
                "订单明细.数据ID(不可修改)",
                "订单明细.产品名称",
                "订单明细.单价",
            ],
            ["XSDD-1", "维保", "SO-1", "SL-1", "PN-SALES", 120],
        ],
    )])

    file_result = _precheck(import_client, payload)["files"][0]

    assert file_result["file_type"] == "sales"
    assert file_result["selected_sheets"] == ["销售"]
    assert _sheet(file_result, "销售")["detected_type"] == "sales"


def test_precheck_missing_price_is_nonblocking_warning(import_client):
    payload = _workbook_bytes([(
        "采购无价格",
        [
            _PURCHASE_HEADER[:-1],
            _PURCHASE_ROW[:-1],
        ],
    )])

    result = _precheck(import_client, payload)
    file_result = result["files"][0]
    sheet = _sheet(file_result, "采购无价格")

    assert file_result["missing_price"] is True
    assert file_result["ok"] is False
    assert file_result["severity"] == "warning"
    assert file_result["can_import"] is True
    assert result["has_errors"] is False
    assert result["can_import_all"] is True
    assert any(issue["code"] == "missing_price_columns" for issue in sheet["issues"])


def test_precheck_all_unrecognized_is_file_error(import_client):
    payload = _workbook_bytes([
        ("说明", [["说明", "备注"], ["只用于阅读", "不导入"]]),
        ("附件", [["名称", "内容"], ["附件一", "文本"]]),
    ])

    result = _precheck(import_client, payload)
    file_result = result["files"][0]

    assert file_result["file_type"] is None
    assert file_result["selected_sheets"] == []
    assert file_result["severity"] == "error"
    assert file_result["can_import"] is False
    assert file_result["ok"] is False
    assert any(issue["code"] == "no_recognized_sheet" for issue in file_result["issues"])
    assert {sheet["action"] for sheet in file_result["sheets"]} == {"ignored_unrecognized"}
    assert all(sheet["issues"][0]["severity"] == "info" for sheet in file_result["sheets"])
    assert result["has_errors"] is True
    assert result["can_import_all"] is False


def test_precheck_selects_first_recognized_and_marks_later_sheets(import_client):
    payload = _workbook_bytes([
        ("封面", [["标题"], ["月度汇总"]]),
        ("采购", [_PURCHASE_HEADER, _PURCHASE_ROW]),
        (
            "销售",
            [
                ["订单编号(必填)", "业务类型", "订单明细.产品名称", "订单明细.单价"],
                ["XSDD-2", "销售", "PN-SALES", 130],
            ],
        ),
    ])

    file_result = _precheck(import_client, payload)["files"][0]

    assert file_result["file_type"] == "purchase"
    assert file_result["selected_sheets"] == ["采购"]
    assert _sheet(file_result, "封面")["action"] == "ignored_unrecognized"
    assert _sheet(file_result, "采购")["action"] == "selected"
    ignored = _sheet(file_result, "销售")
    assert ignored["action"] == "ignored_recognized"
    assert any(issue["code"] == "sheet_ignored_recognized" for issue in ignored["issues"])
    assert file_result["can_import"] is True


def test_precheck_project_workbook_selects_only_expense(import_client):
    maintenance_header = [
        "数据ID(不可修改)",
        "需求单号",
        "制单日期",
        "需求类型",
        "需求明细.数据ID(不可修改)",
        "需求明细.需供货产品",
        "需求明细.需求数量",
    ]
    payload = _workbook_bytes([
        ("备件明细", [maintenance_header, ["M-1", "WBDD-1", "2026-07-01", "报修", "ML-1", "PN-1", 1]]),
        ("报销明细", [_EXPENSE_HEADER, ["2026-07-01", "快递", 20]]),
        ("填写说明", [["说明"], ["只读"]]),
    ])

    file_result = _precheck(import_client, payload)["files"][0]

    assert file_result["file_type"] == "workbook"
    assert file_result["selected_sheets"] == ["报销明细"]
    assert _sheet(file_result, "备件明细")["action"] == "ignored_recognized"
    assert _sheet(file_result, "报销明细")["action"] == "selected"
    assert _sheet(file_result, "填写说明")["action"] == "ignored_unrecognized"
    assert file_result["can_import"] is True


def test_precheck_single_expense_sheet_keeps_expense_file_type(import_client):
    payload = _workbook_bytes([
        ("报销明细", [_EXPENSE_HEADER, ["2026-07-01", "快递", 20]]),
        ("填写说明", [["说明"], ["只读"]]),
    ])

    file_result = _precheck(import_client, payload)["files"][0]

    assert file_result["file_type"] == "expense"
    assert file_result["selected_sheets"] == ["报销明细"]
    assert _sheet(file_result, "报销明细")["action"] == "selected"
    assert _sheet(file_result, "填写说明")["action"] == "ignored_unrecognized"


def test_precheck_selects_all_expense_sheets(import_client):
    payload = _workbook_bytes([
        ("报销一", [_EXPENSE_HEADER, ["2026-07-01", "快递", 20]]),
        ("采购", [_PURCHASE_HEADER, _PURCHASE_ROW]),
        ("报销二", [_EXPENSE_HEADER, ["2026-07-02", "交通", 30]]),
    ])

    file_result = _precheck(import_client, payload)["files"][0]

    assert file_result["file_type"] == "workbook"
    assert file_result["selected_sheets"] == ["报销一", "报销二"]
    assert _sheet(file_result, "报销一")["action"] == "selected"
    assert _sheet(file_result, "报销二")["action"] == "selected"
    assert _sheet(file_result, "采购")["action"] == "ignored_recognized"


def test_precheck_selected_duplicate_headers_are_blocking_error(import_client):
    duplicate_header = [
        "采购单号(必填)",
        "明细.产品名称(必填)",
        "明细.产品名称(必填)",
        "明细.单价(必填)",
    ]
    payload = _workbook_bytes([
        ("重复采购", [duplicate_header, ["CGDD-1", "PN-A", "PN-B", 100]]),
    ])

    result = _precheck(import_client, payload)
    file_result = result["files"][0]
    sheet = _sheet(file_result, "重复采购")

    assert sheet["duplicate_headers"] == ["明细.产品名称(必填)"]
    assert any(
        issue["severity"] == "error" and issue["code"] == "duplicate_headers"
        for issue in sheet["issues"]
    )
    assert file_result["can_import"] is False
    assert file_result["severity"] == "error"
    assert result["has_errors"] is True


def test_precheck_ignored_duplicate_headers_do_not_block(import_client):
    duplicate_header = [
        "采购单号(必填)",
        "明细.产品名称(必填)",
        "明细.产品名称(必填)",
    ]
    payload = _workbook_bytes([
        ("库存", [_INVENTORY_HEADER, ["INV-1", "PN-A", 1, "总仓"]]),
        ("重复采购", [duplicate_header, ["CGDD-1", "PN-A", "PN-B"]]),
    ])

    result = _precheck(import_client, payload)
    file_result = result["files"][0]
    ignored = _sheet(file_result, "重复采购")

    assert ignored["action"] == "ignored_recognized"
    assert any(
        issue["severity"] == "warning" and issue["code"] == "duplicate_headers_ignored"
        for issue in ignored["issues"]
    )
    assert file_result["severity"] == "warning"
    assert file_result["can_import"] is True
    assert result["has_errors"] is False
    assert result["can_import_all"] is True


def test_precheck_corrupt_workbook_is_error(import_client):
    result = _precheck(import_client, b"not-an-xlsx", "broken.xlsx")
    file_result = result["files"][0]

    assert file_result["sheets"] == []
    assert file_result["severity"] == "error"
    assert file_result["can_import"] is False
    assert file_result["issues"][0]["code"] == "invalid_workbook"


def test_precheck_oversized_row_count_is_error(import_client, monkeypatch):
    monkeypatch.setattr(config, "IMPORT_MAX_ROWS", 3)
    monkeypatch.setattr(
        reader.pd,
        "read_excel",
        lambda *_args, **_kwargs: pytest.fail("超行文件不应进入 pandas 全量读取"),
    )
    payload = _workbook_bytes([(
        "采购",
        [
            _PURCHASE_HEADER,
            _PURCHASE_ROW,
            ["CGDD-2", "PO-2", "PL-2", "PN-2", 100],
            ["CGDD-3", "PO-3", "PL-3", "PN-3", 100],
        ],
    )])

    file_result = _precheck(import_client, payload)["files"][0]

    assert file_result["severity"] == "error"
    assert file_result["can_import"] is False
    assert file_result["issues"][0]["code"] == "row_limit_exceeded"


def test_precheck_upload_size_limit_is_structured_error(import_client, monkeypatch):
    monkeypatch.setattr(imports_api, "MAX_UPLOAD_MB", 0)
    monkeypatch.setattr(
        imports_api.import_precheck,
        "inspect_file",
        lambda *_args, **_kwargs: pytest.fail("超大文件不应进入工作簿解析"),
    )
    payload = _workbook_bytes([("采购", [_PURCHASE_HEADER, _PURCHASE_ROW])])

    file_result = _precheck(import_client, payload)["files"][0]

    assert file_result["severity"] == "error"
    assert file_result["can_import"] is False
    assert file_result["issues"][0]["code"] == "file_too_large"


def test_pipeline_and_precheck_share_sheet_selection(
    db, import_client, tmp_path, monkeypatch,
):
    payload = _workbook_bytes([
        ("封面", [["标题"], ["库存导入"]]),
        ("库存一", [_INVENTORY_HEADER, ["INV-A", "PN-A", 1, "总仓"]]),
        ("库存二", [_INVENTORY_HEADER, ["INV-B", "PN-B", 2, "总仓"]]),
    ])
    path = tmp_path / "selection.xlsx"
    path.write_bytes(payload)
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path / "raw"))
    real_select = sheet_selection.select_workbook_sheets
    calls: list[list[str]] = []

    def capture_selection(sheets):
        selection = real_select(sheets)
        calls.append([sheet.sheet_name for sheet in selection.selected])
        return selection

    monkeypatch.setattr(sheet_selection, "select_workbook_sheets", capture_selection)

    precheck_result = _precheck(import_client, payload)["files"][0]
    batch = pipeline.run_import(db, str(path), path.name)
    db.commit()

    assert precheck_result["selected_sheets"] == ["库存一"]
    assert calls == [["库存一"], ["库存一"]]
    assert batch.file_type == "inventory"
    assert db.scalars(select(Inventory.raw_inventory_id).order_by(Inventory.raw_inventory_id)).all() == [
        "INV-A",
    ]


def test_precheck_does_not_log_business_cell_values(import_client, caplog):
    secret_customer = "仅用于隐私回归的客户名称"
    payload = _workbook_bytes([(
        "销售",
        [
            [
                "订单编号(必填)",
                "业务类型",
                "客户名称",
                "订单明细.产品名称",
                "订单明细.单价",
            ],
            ["XSDD-SECRET", "销售", secret_customer, "PN-SECRET", 987654.32],
        ],
    )])

    _precheck(import_client, payload)

    assert secret_customer not in caplog.text
    assert "987654.32" not in caplog.text
