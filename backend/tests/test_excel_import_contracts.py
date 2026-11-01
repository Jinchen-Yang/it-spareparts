"""通用 Excel 导入契约（D-0050 第一批）：类型内别名、同义列冲突、身份门禁、重复明细。

覆盖真实入口：reader.read_excel / precheck.inspect_file / pipeline.load_workbook +
transform_workbook / run_import。全部合成数据，不连接生产库。
"""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from sqlalchemy import func, select

from app.etl import mapping, pipeline, precheck, reader
from app.etl.transform import ErrorRec, TransformResult, transform
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysImportBatch, SysImportError, SysRawFile


def _write_workbook(
    tmp_path: Path,
    name: str,
    headers: list[str],
    rows: list[list[object]],
    *,
    double_header: bool = True,
) -> str:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    if double_header:
        sheet.append([f"F{i:07d}" for i in range(1, len(headers) + 1)])
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    return str(path)


_PURCHASE_HEADERS = [
    "采购单号(必填)", "数据ID(不可修改)", "供应商(必填)", "不含税金额",
    "税率(选填)", "税金", "采购金额", "是否含税(选填)",
    "明细.数据ID(不可修改)", "明细.产品名称(必填)", "明细.采购数量(必填)",
    "明细.单价(必填)", "明细.合计金额",
]
_PURCHASE_ROW = [
    "CGDD-1", "O1", "合成供应商", 100, "13%", 13, 113, "含税",
    "L1", "PN-A", 1, 113, 113,
]


@pytest.mark.parametrize("tax_header", ["税率", "税率(选填)", "税率(不可修改)", "税率(必填)"])
def test_purchase_tax_rate_variants_are_read_with_purchase_canonical_key(
    tmp_path, tax_header,
):
    headers = [
        tax_header if column == "税率(选填)" else column
        for column in _PURCHASE_HEADERS
    ]
    path = _write_workbook(tmp_path, "purchase-tax.xlsx", headers, [_PURCHASE_ROW])

    frame, file_type = reader.read_excel(path)
    result = transform(frame, file_type)

    assert file_type == mapping.PURCHASE
    assert "税率(必填)" in frame.columns
    assert "税率" not in frame.columns
    assert "是否含税(必填)" in frame.columns
    assert result.orders["O1"]["tax_rate"] == Decimal("0.13")
    assert not result.errors


def test_wbdd_project_name_and_annotated_header_aliases_are_read(tmp_path):
    headers = [
        "需求单号", "数据ID(不可修改)", "需求类型", "项目名称(必填)",
        "维保起始日期(必填)", "维保终止日期(必填)", "项目经理(必填)",
        "需求明细.数据ID(不可修改)", "需求明细.需供货产品", "需求明细.需求数量",
    ]
    path = _write_workbook(
        tmp_path,
        "wbdd-aliases.xlsx",
        headers,
        [["WBDD-1", "M1", "报修供货", "合成项目", "2026-01-01", "2026-12-31",
          "项目经理甲", "ML1", "PN-A", 1]],
    )

    frame, file_type = reader.read_excel(path)
    result = transform(frame, file_type)

    assert file_type == mapping.MAINTENANCE
    assert "项目名" in frame.columns and "项目名称(必填)" not in frame.columns
    assert "维保起始日期" in frame.columns
    assert "维保终止日期" in frame.columns
    assert "项目经理" in frame.columns and "项目经理(必填)" not in frame.columns
    head = result.orders["M1"]
    assert head["project_raw"] == "合成项目"
    assert head["maint_start"].isoformat() == "2026-01-01"
    assert head["maint_end"].isoformat() == "2026-12-31"
    assert head["project_manager_raw"] == "项目经理甲"
    assert not result.errors


def test_sales_aliases_keep_existing_target_preference_and_tax_fields(tmp_path):
    headers = [
        "订单编号(必填)", "数据ID(不可修改)", "业务类型#", "业务类型",
        "维保起始日期(必填)", "项目经理(必填)", "税率", "税率(必填)",
        "订单明细.数据ID(不可修改)", "订单明细.产品名称",
        "订单明细.订单数量", "订单明细.单价", "订单明细.金额",
    ]
    path = _write_workbook(
        tmp_path,
        "sales-aliases.xlsx",
        headers,
        [
            ["XSDD-A", "SA", "标准A", "裸A", "2026-01-01", "销售经理甲",
             "13%", "13%", "SA-L1", "PN-A", 1, 100, 100],
            ["XSDD-B", "SB", None, "裸B", None, None, None, None,
             "SB-L1", "PN-B", 1, 200, 200],
        ],
    )

    frame, file_type = reader.read_excel(path)
    result = transform(frame, file_type)

    assert file_type == mapping.SALES
    assert "业务类型" not in frame.columns
    assert "税率" in frame.columns and "税率(必填)" not in frame.columns
    assert result.orders["SA"]["business_type"] == "标准A"
    assert result.orders["SB"]["business_type"] == "裸B"
    assert result.orders["SA"]["tax_rate"] == Decimal("0.13")
    assert not result.errors


@pytest.mark.parametrize(
    ("headers", "rows"),
    [
        (
            ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
             "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.采购数量",
             "明细.单价(必填)"],
            [["CGDD-1", "O1", "L1", "PN-A", 2, 99, 100]],
        ),
        (
            ["采购单号(必填)", "数据ID(不可修改)", "数据ID",
             "明细.数据ID(不可修改)", "明细.产品名称(必填)",
             "明细.采购数量(必填)", "明细.单价(必填)"],
            [["CGDD-1", "O1", "O2", "L1", "PN-A", 1, 100]],
        ),
        (
            ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
             "明细.产品名称(必填)", "明细.采购数量(必填)",
             "明细.单价(必填)", "明细.单价"],
            [["CGDD-1", "O1", "L1", "PN-A", 1, 100, 101]],
        ),
        (
            ["订单编号(必填)", "数据ID(不可修改)", "业务类型#", "税率", "税率(必填)",
             "订单明细.数据ID(不可修改)", "订单明细.产品名称",
             "订单明细.订单数量", "订单明细.单价", "订单明细.金额"],
            [["XSDD-1", "S1", "备件销售", "13%", "6%",
              "S1-L1", "PN-A", 1, 100, 100]],
        ),
    ],
    ids=["quantity", "identity", "price", "sales-tax"],
)
def test_critical_alias_conflicts_reject_whole_file(tmp_path, headers, rows):
    path = _write_workbook(tmp_path, "alias-conflict.xlsx", headers, rows)

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)

    assert exc_info.value.code == "alias_column_conflict"
    assert "取值不一致" in str(exc_info.value)


def test_precheck_reports_critical_alias_conflict_before_import(tmp_path):
    """表头预检不能只看列名：规范数量=2、裸别名=99 必须报歧义并拦住导入。"""
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.采购数量",
               "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path, "precheck-alias-conflict.xlsx", headers,
        [["CGDD-1", "O1", "L1", "PN-A", 2, 99, 100]],
    )

    result = precheck.inspect_file(path, "precheck-alias-conflict.xlsx")

    assert result["can_import"] is False
    selected = next(
        sheet for sheet in result["sheets"] if sheet["action"] == "selected")
    issue = next(
        issue for issue in selected["issues"]
        if issue["code"] == "alias_column_conflict")
    assert issue["severity"] == "error"
    assert "取值不一致" in issue["message"] and "明细.采购数量" in issue["message"]
    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)
    assert exc_info.value.code == "alias_column_conflict"


def test_same_value_aliases_are_merged_without_error(tmp_path):
    path = _write_workbook(
        tmp_path,
        "alias-same.xlsx",
        ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
         "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.采购数量",
         "明细.单价(必填)"],
        [["CGDD-1", "O1", "L1", "PN-A", 2, 2, 100]],
    )

    preview = precheck.inspect_file(path, "alias-same.xlsx")
    assert preview["can_import"] is True

    frame, file_type = reader.read_excel(path)
    result = transform(frame, file_type)

    quantity_columns = [column for column in frame.columns if "采购数量" in column]
    assert quantity_columns == ["明细.采购数量(必填)"]
    assert result.lines[0]["qty"] == 2
    assert not result.errors


def test_independent_hash_and_address_columns_are_not_merged(tmp_path):
    headers = [
        "需求单号", "数据ID(不可修改)", "需求类型", "快递单号", "快递单号#",
        "选择收货地址", "收货地址",
        "需求明细.数据ID(不可修改)", "需求明细.需供货产品", "需求明细.需求数量",
    ]
    path = _write_workbook(
        tmp_path,
        "wbdd-independent.xlsx",
        headers,
        [["WBDD-1", "M1", "报修供货", "SF-1", "SF-2", "选项地址", "正文地址",
          "ML1", "PN-A", 1]],
    )

    frame, file_type = reader.read_excel(path)
    result = transform(frame, file_type)

    for column in ("快递单号", "快递单号#", "选择收货地址", "收货地址"):
        assert column in frame.columns
    head = result.orders["M1"]
    assert head["express_no"] == "SF-1"
    assert head["express_no2"] == "SF-2"
    assert head["delivery_address_option"] == "选项地址"
    assert head["receiver_address"] == "正文地址"


@pytest.mark.parametrize(
    ("headers", "rows", "missing"),
    [
        (
            ["采购单号(必填)", "明细.产品名称(必填)", "明细.采购数量(必填)",
             "明细.单价(必填)"],
            [["CGDD-1", "PN-A", 1, 100]],
            ("数据ID(不可修改)", "明细.数据ID(不可修改)"),
        ),
        (
            ["需求单号", "记录ID", "需求类型", "需求明细.记录ID",
             "需求明细.需供货产品", "需求明细.需求数量"],
            [["WBDD-1", "M1", "报修供货", "ML1", "PN-A", 1]],
            ("数据ID(不可修改)", "需求明细.数据ID(不可修改)"),
        ),
    ],
    ids=["purchase-no-ids", "wbdd-old-record-id"],
)
def test_missing_identity_columns_rejected_by_precheck_and_execution(
    tmp_path, headers, rows, missing,
):
    path = _write_workbook(tmp_path, "missing-ids.xlsx", headers, rows)

    result = precheck.inspect_file(path, "missing-ids.xlsx")
    selected = next(
        sheet for sheet in result["sheets"] if sheet["action"] == "selected"
    )
    identity_issue = next(
        issue for issue in selected["issues"]
        if issue["code"] == "missing_identity_columns"
    )
    assert identity_issue["severity"] == "error"
    assert result["can_import"] is False

    loaded = pipeline.load_workbook(path)
    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline.require_clean_workbook(loaded)
    assert exc_info.value.code == "missing_identity_columns"
    assert str(exc_info.value) == identity_issue["message"]
    for column in missing:
        assert column in str(exc_info.value)


def test_valid_purchase_with_identity_columns_is_not_blocked(tmp_path):
    path = _write_workbook(
        tmp_path,
        "valid-purchase.xlsx",
        ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
         "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"],
        [["CGDD-1", "O1", "L1", "PN-A", 1, 100]],
    )

    result = precheck.inspect_file(path, "valid-purchase.xlsx")

    assert result["can_import"] is True
    assert result["file_type"] == mapping.PURCHASE
    assert not any(
        issue["code"] == "missing_identity_columns"
        for sheet in result["sheets"] for issue in sheet["issues"]
    )


def test_missing_identity_columns_do_not_block_ignored_sheets(tmp_path):
    path = _write_workbook(
        tmp_path,
        "ignored-sales.xlsx",
        ["产品库存ID", "产品名称(PN)", "库存数量", "仓库"],
        [["INV-1", "PN-A", 1, "总仓"]],
    )
    workbook = openpyxl.load_workbook(path)
    sales = workbook.create_sheet("销售")
    sales.append(["订单编号", "业务类型", "订单明细.产品名称", "订单明细.单价"])
    sales.append(["XSDD-IGNORED", "备件销售", "PN-B", 100])
    workbook.save(path)

    result = precheck.inspect_file(path, "ignored-sales.xlsx")

    assert result["can_import"] is True
    assert not any(
        issue["code"] == "missing_identity_columns"
        for sheet in result["sheets"] for issue in sheet["issues"]
    )


def test_blank_data_rows_are_trimmed_instead_of_becoming_errors(tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "blank-rows.xlsx",
        headers,
        [
            ["CGDD-1", "O1", "L1", "PN-A", 1, 100],
            ["" for _ in headers],
            ["CGDD-2", "O2", "L2", "PN-B", 1, 200],
        ],
    )

    transformed = pipeline.transform_workbook(pipeline.load_workbook(path))

    assert transformed.result.rows_total == 2
    assert len(transformed.result.lines) == 2
    assert not transformed.result.errors


def test_duplicate_raw_line_same_content_is_counted_and_collapsed(tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    row = ["CGDD-1", "O1", "L1", "PN-A", 1, 100]
    path = _write_workbook(
        tmp_path, "duplicate-same.xlsx", headers,
        [row, list(row), ["CGDD-2", "O2", "L2", "PN-B", 1, 200]],
    )

    transformed = pipeline.transform_workbook(pipeline.load_workbook(path))

    assert transformed.result.rows_total == 3      # 源行数不因折叠减少
    assert len(transformed.result.lines) == 2
    assert transformed.extra_report["duplicate_lines_collapsed"] == 1
    assert not transformed.result.errors
    pipeline._ensure_importable_result(transformed.result)   # 同内容不算全错


def test_duplicate_raw_line_header_value_conflict_rejected(tmp_path):
    """同明细ID只改了头字段：旧实现只比 line dict 会静默折叠，必须按源行拒绝。"""
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)",
               "采购金额"]
    path = _write_workbook(
        tmp_path,
        "duplicate-head-conflict.xlsx",
        headers,
        [
            ["CGDD-1", "O1", "L1", "PN-A", 1, 100, 113],
            ["CGDD-1", "O1", "L1", "PN-A", 1, 100, 114],
        ],
    )

    transformed = pipeline.transform_workbook(pipeline.load_workbook(path))

    conflict = next(
        error for error in transformed.result.errors
        if error.error_type == "duplicate_raw_line_id_conflict")
    assert conflict.row_no == 2
    assert conflict.identity["raw_line_id"] == "L1"
    assert conflict.identity["source_rows"] == [1, 2]
    assert "采购金额" in conflict.error_detail
    assert "数据行 1、2" in conflict.error_detail
    assert transformed.result.lines == []
    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline._ensure_importable_result(transformed.result)
    assert exc_info.value.code == "duplicate_raw_line_id_conflict"


def test_duplicate_raw_line_detected_across_large_file_chunks(tmp_path):
    """1,001 源行整表扫描：重复身份不能被任何分块边界漏掉。"""
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    rows = [
        [f"CGDD-{index}", f"O{index}", f"L{index}", "PN-A", 1, 100]
        for index in range(1, 1001)
    ]
    rows.append(list(rows[0]))
    path = _write_workbook(tmp_path, "duplicate-large.xlsx", headers, rows)

    transformed = pipeline.transform_workbook(pipeline.load_workbook(path))

    assert transformed.result.rows_total == 1001
    assert transformed.extra_report["duplicate_lines_collapsed"] == 1
    assert len(transformed.result.lines) == 1000
    assert not transformed.result.errors


def test_duplicate_raw_line_across_orders_is_a_conflict(tmp_path):
    """同明细ID出现在不同父单：即使明细字段相同也必须拒绝。"""
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "duplicate-parent.xlsx",
        headers,
        [
            ["CGDD-1", "O1", "L1", "PN-A", 1, 100],
            ["CGDD-2", "O2", "L1", "PN-A", 1, 100],
        ],
    )

    transformed = pipeline.transform_workbook(pipeline.load_workbook(path))

    conflict = next(
        error for error in transformed.result.errors
        if error.error_type == "duplicate_raw_line_id_conflict")
    assert conflict.identity["raw_line_id"] == "L1"
    assert conflict.identity["source_rows"] == [1, 2]
    assert "数据ID(不可修改)" in conflict.error_detail
    assert transformed.result.lines == []
    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline._ensure_importable_result(transformed.result)
    assert exc_info.value.code == "duplicate_raw_line_id_conflict"


def test_duplicate_raw_line_conflicting_content_rejected_before_load(tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "duplicate-conflict.xlsx",
        headers,
        [
            ["CGDD-1", "O1", "L1", "PN-A", 1, 100],
            ["CGDD-1", "O1", "L1", "PN-A", 2, 100],
            ["CGDD-2", "O2", "L2", "PN-B", 1, 200],
        ],
    )

    transformed = pipeline.transform_workbook(pipeline.load_workbook(path))

    assert [line["raw_line_id"] for line in transformed.result.lines] == ["L2"]
    conflict = next(
        error for error in transformed.result.errors
        if error.error_type == "duplicate_raw_line_id_conflict"
    )
    assert conflict.identity["raw_line_id"] == "L1"
    assert conflict.row_no == 2
    assert conflict.identity["source_rows"] == [1, 2]
    assert "明细.采购数量" in conflict.error_detail
    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline._ensure_importable_result(transformed.result)
    assert exc_info.value.code == "duplicate_raw_line_id_conflict"


_TAX_RATE_PURCHASE_HEADERS = [
    "采购单号(必填)", "数据ID(不可修改)", "税率(必填)", "税率",
    "明细.数据ID(不可修改)", "明细.产品名称(必填)",
    "明细.采购数量(必填)", "明细.单价(必填)",
]
_TAX_RATE_SALES_HEADERS = [
    "订单编号(必填)", "数据ID(不可修改)", "业务类型#", "税率", "税率(必填)",
    "订单明细.数据ID(不可修改)", "订单明细.产品名称",
    "订单明细.订单数量", "订单明细.单价", "订单明细.金额",
]


@pytest.mark.parametrize(
    ("file_type", "headers", "row", "order_id"),
    [
        (mapping.PURCHASE, _TAX_RATE_PURCHASE_HEADERS,
         ["CGDD-1", "O1", "13.0%", 0.13, "L1", "PN-A", 1, 100], "O1"),
        (mapping.SALES, _TAX_RATE_SALES_HEADERS,
         ["XSDD-1", "S1", "备件销售", 0.13, "13.0%", "S1-L1", "PN-A", 1, 100, 100], "S1"),
    ],
    ids=["purchase", "sales"],
)
def test_tax_rate_alias_equivalence_passes_precheck_and_execution(
    tmp_path, file_type, headers, row, order_id,
):
    """13%、13.0%、numeric 0.13 按 cleaner.parse_rate 既有语义等价，不误报别名冲突。"""
    path = _write_workbook(
        tmp_path, "tax-rate-equivalent.xlsx", headers, [row], double_header=False)

    preview = precheck.inspect_file(path, "tax-rate-equivalent.xlsx")
    assert preview["can_import"] is True

    frame, detected = reader.read_excel(path)
    result = transform(frame, detected)
    assert detected == file_type
    assert result.orders[order_id]["tax_rate"] == Decimal("0.1300")
    assert not result.errors


@pytest.mark.parametrize(
    ("headers", "row"),
    [
        (_TAX_RATE_PURCHASE_HEADERS,
         ["CGDD-1", "O1", "13%", "6%", "L1", "PN-A", 1, 100]),
        (_TAX_RATE_SALES_HEADERS,
         ["XSDD-1", "S1", "备件销售", 0.13, "6%", "S1-L1", "PN-A", 1, 100, 100]),
    ],
    ids=["purchase", "sales"],
)
def test_tax_rate_alias_conflict_rejected_by_precheck_and_execution(
    tmp_path, headers, row,
):
    path = _write_workbook(
        tmp_path, "tax-rate-conflict.xlsx", headers, [row], double_header=False)

    preview = precheck.inspect_file(path, "tax-rate-conflict.xlsx")
    assert preview["can_import"] is False
    selected = next(
        sheet for sheet in preview["sheets"] if sheet["action"] == "selected")
    issue = next(
        issue for issue in selected["issues"]
        if issue["code"] == "alias_column_conflict")
    assert issue["severity"] == "error"
    assert "税率" in issue["message"] and "取值不一致" in issue["message"]

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)
    assert exc_info.value.code == "alias_column_conflict"


_TAX_RATE_PURCHASE_REVERSED_HEADERS = [
    "采购单号(必填)", "数据ID(不可修改)", "税率", "税率(必填)",
    "明细.数据ID(不可修改)", "明细.产品名称(必填)",
    "明细.采购数量(必填)", "明细.单价(必填)",
]
_TAX_RATE_SALES_REVERSED_HEADERS = [
    "订单编号(必填)", "数据ID(不可修改)", "业务类型#", "税率(必填)", "税率",
    "订单明细.数据ID(不可修改)", "订单明细.产品名称",
    "订单明细.订单数量", "订单明细.单价", "订单明细.金额",
]


@pytest.mark.parametrize(
    ("file_type", "headers", "row", "order_id", "target"),
    [
        (mapping.PURCHASE, _TAX_RATE_PURCHASE_HEADERS,
         ["CGDD-1", "O1", "   ", "13%", "L1", "PN-A", 1, 100], "O1", "税率(必填)"),
        (mapping.PURCHASE, _TAX_RATE_PURCHASE_HEADERS,
         ["CGDD-1", "O1", "13%", "   ", "L1", "PN-A", 1, 100], "O1", "税率(必填)"),
        (mapping.PURCHASE, _TAX_RATE_PURCHASE_REVERSED_HEADERS,
         ["CGDD-1", "O1", "   ", "13%", "L1", "PN-A", 1, 100], "O1", "税率(必填)"),
        (mapping.PURCHASE, _TAX_RATE_PURCHASE_REVERSED_HEADERS,
         ["CGDD-1", "O1", "13%", "   ", "L1", "PN-A", 1, 100], "O1", "税率(必填)"),
        (mapping.SALES, _TAX_RATE_SALES_HEADERS,
         ["XSDD-1", "S1", "备件销售", "   ", "13%", "S1-L1", "PN-A", 1, 100, 100],
         "S1", "税率"),
        (mapping.SALES, _TAX_RATE_SALES_HEADERS,
         ["XSDD-1", "S1", "备件销售", "13%", "   ", "S1-L1", "PN-A", 1, 100, 100],
         "S1", "税率"),
        (mapping.SALES, _TAX_RATE_SALES_REVERSED_HEADERS,
         ["XSDD-1", "S1", "备件销售", "   ", "13%", "S1-L1", "PN-A", 1, 100, 100],
         "S1", "税率"),
        (mapping.SALES, _TAX_RATE_SALES_REVERSED_HEADERS,
         ["XSDD-1", "S1", "备件销售", "13%", "   ", "S1-L1", "PN-A", 1, 100, 100],
         "S1", "税率"),
    ],
    ids=["purchase-target-blank-first", "purchase-alias-blank-first",
         "purchase-alias-blank-reversed", "purchase-target-blank-reversed",
         "sales-target-blank-first", "sales-alias-blank-first",
         "sales-alias-blank-reversed", "sales-target-blank-reversed"],
)
def test_whitespace_tax_rate_alias_fills_and_preserves_legal_rate(
    tmp_path, file_type, headers, row, order_id, target,
):
    """空白列按同一口径置空：预检与执行都必须由合法别名补位，不误报冲突。"""
    path = _write_workbook(
        tmp_path, "tax-rate-whitespace.xlsx", headers, [row], double_header=False)

    preview = precheck.inspect_file(path, "tax-rate-whitespace.xlsx")
    assert preview["can_import"] is True

    frame, detected = reader.read_excel(path)
    assert detected == file_type
    assert frame[target].iloc[0] == "13%"
    result = transform(frame, detected)
    assert result.orders[order_id]["tax_rate"] == Decimal("0.1300")
    assert not result.errors


def test_tax_rate_parse_none_on_nonblank_source_is_not_treated_as_blank(tmp_path):
    """非空源值即使 parse_rate 返回 None，也保留非空标记，不能由合法别名补位。"""
    path = _write_workbook(
        tmp_path, "tax-rate-parse-none.xlsx", _TAX_RATE_PURCHASE_HEADERS,
        [["CGDD-1", "O1", " nan ", 0.13, "L1", "PN-A", 1, 100]],
        double_header=False,
    )

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)
    assert exc_info.value.code == "alias_column_conflict"
    assert "nan" in str(exc_info.value)


def test_invalid_tax_rate_alias_is_not_treated_as_blank(tmp_path):
    """非法非空税率保持非空标记：不能落成 None 让合法别名静默补位。"""
    path = _write_workbook(
        tmp_path, "tax-rate-invalid-mixed.xlsx", _TAX_RATE_PURCHASE_HEADERS,
        [["CGDD-1", "O1", "abc", 0.13, "L1", "PN-A", 1, 100]],
        double_header=False,
    )

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)
    assert exc_info.value.code == "alias_column_conflict"
    assert "abc" in str(exc_info.value) and "0.13" in str(exc_info.value)


def test_same_invalid_tax_rate_alias_merges_then_transform_rejects_order(tmp_path):
    """同值非法税率在别名比较里合并不冲突；transform 按 D-0050 第五条拒整单。"""
    path = _write_workbook(
        tmp_path, "tax-rate-invalid-same.xlsx", _TAX_RATE_PURCHASE_HEADERS,
        [["CGDD-1", "O1", "abc", "abc", "L1", "PN-A", 1, 100]],
        double_header=False,
    )

    frame, file_type = reader.read_excel(path)
    assert file_type == mapping.PURCHASE
    # reader 必须保留非法原值，不能吞成空值让后续流程看似合法。
    assert frame["税率(必填)"].iloc[0] == "abc"
    assert "税率" not in frame.columns   # 同值别名列已合并，不产生冲突拒批
    result = transform(frame, file_type)
    # 非法税率拒绝 O1 及其明细，不写入半张单。
    assert result.orders == {}
    assert result.lines == []
    errors = [e for e in result.errors if e.error_type == "invalid_head_value"]
    assert len(errors) == 1
    error = errors[0]
    assert error.row_no == 1
    assert error.raw_row == {"tax_rate": "abc"}
    assert "税率「abc」无法解析（税率非法: 'abc'）" in error.error_detail
    assert "该单本次不导入，保留库内已有事实" in error.error_detail


_FLAG_PURCHASE_HEADERS = [
    "采购单号(必填)", "数据ID(不可修改)", "不含税金额", "税率(必填)", "税金",
    "采购金额", "是否含税(必填)", "是否含税",
    "明细.数据ID(不可修改)", "明细.产品名称(必填)",
    "明细.采购数量(必填)", "明细.单价(必填)", "明细.合计金额",
]


def _flag_purchase_row(
    *, canonical: object, alias: object,
) -> list[object]:
    return ["CGDD-1", "O1", 100, "13%", 13, 113, canonical, alias,
            "L1", "PN-A", 1, 113, 113]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("含税", True), ("含", True), ("不含税", False), ("未税", False),
     ("不含", False)],
)
def test_tax_inclusive_alias_normalization_reuses_cleaner_vocabulary(raw, expected):
    """复用 cleaner 既有词表；False 是有效业务值，不能被 truthy or 兜底成原文。"""
    assert reader._normalize_alias_value(raw, "is_tax_inclusive") is expected


def test_tax_inclusive_unknown_nonblank_marker_is_not_blank():
    assert reader._normalize_alias_value("待确认", "is_tax_inclusive") == "待确认"
    assert reader._normalize_alias_value("   ", "is_tax_inclusive") is None


@pytest.mark.parametrize(
    ("canonical", "alias", "expected"),
    [("未税", "不含税", "未税"), ("不含", "未税", "不含")],
)
def test_tax_inclusive_alias_equivalence_keeps_valid_false(
    tmp_path, canonical, alias, expected,
):
    """不含税/未税在 cleaner 既有词表里都是 False，不能误报同义列冲突。"""
    path = _write_workbook(
        tmp_path, "flag-equivalent.xlsx", _FLAG_PURCHASE_HEADERS,
        [_flag_purchase_row(canonical=canonical, alias=alias)],
    )

    frame, file_type = reader.read_excel(path)
    result = transform(frame, file_type)

    assert frame["是否含税(必填)"].iloc[0] == expected
    assert result.orders["O1"]["is_tax_inclusive"] is False
    assert not result.errors


@pytest.mark.parametrize(
    ("canonical", "alias"),
    [("含税", "未税"), ("不含", "含税")],
)
def test_tax_inclusive_opposite_flags_conflict(tmp_path, canonical, alias):
    """相反含税口径必须是显式冲突，不能按列序静默取一个。"""
    path = _write_workbook(
        tmp_path, "flag-conflict.xlsx", _FLAG_PURCHASE_HEADERS,
        [_flag_purchase_row(canonical=canonical, alias=alias)],
    )

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)

    assert exc_info.value.code == "alias_column_conflict"
    assert canonical in str(exc_info.value) and alias in str(exc_info.value)


def test_tax_inclusive_unknown_nonblank_is_not_swallowed(tmp_path):
    """未知非空值保留非空 marker：不能当空值让合法别名静默补位。"""
    path = _write_workbook(
        tmp_path, "flag-unknown.xlsx", _FLAG_PURCHASE_HEADERS,
        [_flag_purchase_row(canonical="待确认", alias="含税")],
    )

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)

    assert exc_info.value.code == "alias_column_conflict"
    assert "待确认" in str(exc_info.value) and "含税" in str(exc_info.value)


@pytest.mark.parametrize(
    ("canonical", "alias", "expected"),
    [("   ", "未税", "未税"), (None, "不含税", "不含税")],
)
def test_tax_inclusive_blank_target_is_filled_by_legal_alias(
    tmp_path, canonical, alias, expected,
):
    """空白目标列按同一口径置空，由合法别名补位且保留有效 False。"""
    path = _write_workbook(
        tmp_path, "flag-blank.xlsx", _FLAG_PURCHASE_HEADERS,
        [_flag_purchase_row(canonical=canonical, alias=alias)],
    )

    frame, file_type = reader.read_excel(path)
    result = transform(frame, file_type)

    assert frame["是否含税(必填)"].iloc[0] == expected
    assert result.orders["O1"]["is_tax_inclusive"] is False
    assert not result.errors


_SOURCE_PAIR_KEY = "_source_head_economic_pair_rows"


def test_source_economic_pair_rows_use_pre_ffill_source_index(tmp_path):
    """空行保留源索引；只有后行显式同时提供不含税金额+税金才算同源配对。"""
    headers = ["采购单号(必填)", "数据ID(不可修改)", "不含税金额", "税金",
               "是否含税(必填)"]
    path = _write_workbook(
        tmp_path, "source-pair-positive.xlsx", headers,
        [
            ["CGDD-1", "O1", 100, None, "不含"],
            [None, None, None, None, None],
            ["CGDD-1", "O1", 100, 13, "含税"],
        ],
    )

    frame, file_type = reader.read_excel(path)

    assert file_type == mapping.PURCHASE
    assert frame.index.tolist() == [0, 2]
    assert frame.attrs[_SOURCE_PAIR_KEY] == frozenset({2})


def test_source_economic_pair_rows_are_not_created_by_ffill(tmp_path):
    """早行 100 only、后行仅税 13：ffill 后看似双值，源集合必须为空。"""
    headers = ["采购单号(必填)", "数据ID(不可修改)", "不含税金额", "税金"]
    path = _write_workbook(
        tmp_path, "source-pair-negative.xlsx", headers,
        [
            ["CGDD-1", "O1", 100, None],
            ["CGDD-1", "O1", None, 13],
        ],
    )

    frame, file_type = reader.read_excel(path)

    assert file_type == mapping.PURCHASE
    assert frame.attrs[_SOURCE_PAIR_KEY] == frozenset()
    # ffill 之后后行两项金额都非空，但该行不是源行同源证据。
    assert frame[["不含税金额", "税金"]].notna().all(axis=1).tolist() == [
        False, True,
    ]


def test_source_economic_pair_rows_attached_for_sales(tmp_path):
    headers = ["订单编号(必填)", "数据ID(不可修改)", "业务类型#",
               "不含税金额", "税金"]
    path = _write_workbook(
        tmp_path, "source-pair-sales.xlsx", headers,
        [["XSDD-1", "S1", "备件销售", 100, 13]],
    )

    frame, file_type = reader.read_excel(path)

    assert file_type == mapping.SALES
    assert frame.attrs[_SOURCE_PAIR_KEY] == frozenset({0})


def test_source_economic_pair_rows_key_absent_for_other_types(tmp_path):
    headers = ["需求单号", "数据ID(不可修改)", "需求类型", "不含税金额", "税金"]
    path = _write_workbook(
        tmp_path, "source-pair-wbdd.xlsx", headers,
        [["WBDD-1", "M1", "报修供货", 100, 13]],
    )

    frame, file_type = reader.read_excel(path)

    assert file_type == mapping.MAINTENANCE
    assert _SOURCE_PAIR_KEY not in frame.attrs


def test_blank_rows_keep_source_line_for_alias_conflict(tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.采购数量",
               "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "blank-alias-conflict.xlsx",
        headers,
        [
            ["" for _ in headers],
            ["CGDD-1", "O1", "L1", "PN-A", 2, 99, 100],
        ],
        double_header=False,
    )

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)
    assert exc_info.value.code == "alias_column_conflict"
    assert "数据第 2 行" in str(exc_info.value)


def test_blank_rows_keep_source_line_for_transform_errors(tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "blank-transform-error.xlsx",
        headers,
        [
            ["" for _ in headers],
            ["CGDD-1", "O1", "L1", "PN-A", "abc", 100],
        ],
        double_header=False,
    )

    transformed = pipeline.transform_workbook(pipeline.load_workbook(path))

    assert [error.row_no for error in transformed.result.errors] == [2]
    assert transformed.result.errors[0].error_type == "bad_number"


def test_blank_rows_keep_source_lines_for_duplicate_identity(tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "blank-duplicate-conflict.xlsx",
        headers,
        [
            ["" for _ in headers],
            ["CGDD-1", "O1", "L1", "PN-A", 1, 100],
            ["" for _ in headers],
            ["CGDD-1", "O1", "L1", "PN-A", 2, 100],
        ],
        double_header=False,
    )

    transformed = pipeline.transform_workbook(pipeline.load_workbook(path))

    conflict = next(
        error for error in transformed.result.errors
        if error.error_type == "duplicate_raw_line_id_conflict")
    assert conflict.row_no == 4
    assert conflict.identity["source_rows"] == [2, 4]
    assert "数据行 2、4" in conflict.error_detail


def test_import_gate_allows_soft_only_without_facts():
    result = TransformResult(
        file_type=mapping.SALES,
        errors=[ErrorRec(1, "empty_pn_inactive", "草稿可忽略", {})],
        rows_total=1,
    )

    pipeline._ensure_importable_result(result)   # 不抛：0 硬错误、无事实也成功


def test_import_gate_rejects_mixed_soft_and_hard_without_facts():
    result = TransformResult(
        file_type=mapping.PURCHASE,
        errors=[
            ErrorRec(1, "empty_pn_inactive", "草稿可忽略", {}),
            ErrorRec(2, "bad_number", "数量非法", {}),
        ],
        rows_total=2,
    )

    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline._ensure_importable_result(result)
    assert exc_info.value.code == "no_importable_rows"


def test_import_gate_duplicate_conflict_beats_soft_errors():
    result = TransformResult(
        file_type=mapping.SALES,
        errors=[
            ErrorRec(1, "empty_pn_inactive", "草稿可忽略", {}),
            ErrorRec(2, "duplicate_raw_line_id_conflict", "重复冲突", {}),
        ],
        rows_total=2,
    )

    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline._ensure_importable_result(result)
    assert exc_info.value.code == "duplicate_raw_line_id_conflict"


def test_import_gate_keeps_headless_wbdd_and_all_skipped_lines():
    headless = TransformResult(
        file_type=mapping.MAINTENANCE,
        orders={"M1": {"raw_order_id": "M1"}},
        errors=[ErrorRec(1, "missing_date_in_progress", "在途", {})],
        rows_total=1,
    )
    pipeline._ensure_importable_result(headless)   # 合法单头不算全错

    skipped = TransformResult(
        file_type=mapping.SALES,
        lines=[{"raw_line_id": "L1"}],
        errors=[ErrorRec(1, "empty_pn_inactive", "草稿可忽略", {})],
        rows_total=1,
    )
    pipeline._ensure_importable_result(skipped)    # 已存在 skip 的明细仍保留


class _FakeResult:
    def all(self):
        return []


class _FakeSession:
    def __init__(self):
        self.added = []

    def execute(self, *args, **kwargs):
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


def _fake_import(monkeypatch, tmp_path, path):
    monkeypatch.setattr(
        pipeline, "get_settings",
        lambda: SimpleNamespace(raw_file_dir=str(tmp_path / "raw")),
    )
    monkeypatch.setattr(
        pipeline.loader, "load",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("loader.load must not run when the import gate rejects")
        ),
    )
    session = _FakeSession()
    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline.run_import(session, path, Path(path).name)
    return exc_info.value, session


@pytest.mark.parametrize("status", ["草稿", "已取消"])
def test_run_import_soft_only_empty_pn_is_success_with_zero_hard_errors(
    monkeypatch, tmp_path, status,
):
    """草稿/已取消空 PN：只有软标记、无事实也按成功批次提交，硬错误计数 0。"""
    headers = ["订单编号(必填)", "数据ID(不可修改)", "业务类型#", "数据状态",
               "订单明细.数据ID(不可修改)", "订单明细.产品名称",
               "订单明细.订单数量", "订单明细.单价", "订单明细.金额"]
    path = _write_workbook(
        tmp_path,
        "soft-only-empty-pn.xlsx",
        headers,
        [["XSDD-1", "S1", "备件销售", status, "S1-L1", None, 1, 100, 100]],
        double_header=False,
    )
    monkeypatch.setattr(
        pipeline, "get_settings",
        lambda: SimpleNamespace(raw_file_dir=str(tmp_path / "raw")))
    monkeypatch.setattr(
        pipeline, "_archive", lambda *_args, **_kwargs: str(tmp_path / "raw" / "x.xlsx"))
    monkeypatch.setattr(
        pipeline.data_quality_amount_mismatch, "detect_imported_lines",
        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        pipeline.loader, "load",
        lambda *_args, **_kwargs: {
            "source_rows_total": 1,
            "fact_rows_inserted": 0,
            "fact_rows_updated": 0,
            "fact_rows_skipped": 0,
            "fact_rows_error": 0,
            "rows_inactive": 0,
        })

    session = _FakeSession()
    batch = pipeline.run_import(session, path, "soft-only-empty-pn.xlsx")

    assert batch.status == "success"
    assert batch.rows_error == 0
    assert batch.rows_total == 1
    assert batch.report_json["fact_rows_error"] == 0
    soft_errors = [
        obj for obj in session.added if isinstance(obj, SysImportError)]
    assert [error.error_type for error in soft_errors] == ["empty_pn_inactive"]


def test_run_import_rejects_conflicting_duplicates_before_loader(monkeypatch, tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "run-duplicate-conflict.xlsx",
        headers,
        [
            ["CGDD-1", "O1", "L1", "PN-A", 1, 100],
            ["CGDD-1", "O1", "L1", "PN-A", 2, 100],
        ],
        double_header=False,
    )

    error, session = _fake_import(monkeypatch, tmp_path, path)

    assert error.code == "duplicate_raw_line_id_conflict"
    batch = next(obj for obj in session.added if isinstance(obj, SysImportBatch))
    assert batch.status == "failed"
    assert batch.rows_total == 2
    assert batch.report_json["source_rows_total"] == 2
    assert batch.report_json["fact_rows_error"] == 1
    assert any(
        isinstance(obj, SysImportError)
        and obj.error_type == "duplicate_raw_line_id_conflict"
        for obj in session.added
    )


def test_run_import_rejects_all_rejected_rows_before_loader(monkeypatch, tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "run-all-rejected.xlsx",
        headers,
        [["CGDD-1", None, None, "PN-A", 1, 100]],
        double_header=False,
    )

    error, session = _fake_import(monkeypatch, tmp_path, path)

    assert error.code == "no_importable_rows"
    batch = next(obj for obj in session.added if isinstance(obj, SysImportBatch))
    assert batch.status == "failed"
    assert batch.rows_total == 1
    assert batch.rows_error == 1
    assert batch.report_json["source_rows_total"] == 1
    assert batch.report_json["fact_rows_error"] == 1
    assert batch.report_json["error"] == str(error)
    assert any(
        isinstance(obj, SysImportError) and obj.error_type == "missing_raw_id"
        for obj in session.added
    )


def test_run_import_post_load_reader_error_becomes_integrity_failure(
    monkeypatch, tmp_path,
):
    """loader.load 之后的解析类异常不能走 ReaderError 提交失败批次的路径。"""
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path, "post-load-reader-error.xlsx", headers,
        [["CGDD-1", "O1", "L1", "PN-A", 1, 100]], double_header=False)
    monkeypatch.setattr(
        pipeline, "get_settings",
        lambda: SimpleNamespace(raw_file_dir=str(tmp_path / "raw")))
    monkeypatch.setattr(
        pipeline, "_archive", lambda *_args, **_kwargs: str(tmp_path / "raw" / "x.xlsx"))
    monkeypatch.setattr(
        pipeline.loader, "load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            reader.ReaderError("写入阶段模拟失败", code="after_load")))

    session = _FakeSession()
    with pytest.raises(pipeline.loader.ImportIntegrityError) as exc_info:
        pipeline.run_import(session, path, "post-load-reader-error.xlsx")

    assert "整批已回滚" in str(exc_info.value)
    batch = next(obj for obj in session.added if isinstance(obj, SysImportBatch))
    assert batch.status == "processing"   # 不标 failed；由入口 rollback 丢弃半事务


def test_pipeline_db_all_rejected_persists_failed_batch_without_facts(db, tmp_path):
    headers = ["采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)"]
    path = _write_workbook(
        tmp_path,
        "db-all-rejected.xlsx",
        headers,
        [["CGDD-1", None, None, "PN-A", 1, 100]],
        double_header=False,
    )

    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline.run_import(db, path, "db-all-rejected.xlsx")
    assert exc_info.value.code == "no_importable_rows"
    db.commit()

    batch = db.scalar(select(SysImportBatch))
    assert batch is not None and batch.status == "failed"
    assert "均未形成可导入事实" in batch.report_json["error"]
    assert batch.rows_total == 1
    assert batch.report_json["source_rows_total"] == 1
    assert batch.report_json["fact_rows_error"] == 1
    assert db.scalar(select(func.count()).select_from(FPurchaseOrder)) == 0
    assert db.scalar(select(func.count()).select_from(FPurchaseLine)) == 0
    assert db.scalar(select(func.count()).select_from(SysImportError)) == 1
    # ReaderError 失败批次由 API commit 留痕，但原件归档/业务事实必须都没有写过。
    assert db.scalar(select(func.count()).select_from(SysRawFile)) == 0
