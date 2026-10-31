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
from app.etl.transform import transform
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
