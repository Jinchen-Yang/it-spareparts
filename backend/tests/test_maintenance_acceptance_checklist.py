"""验收需求清单 Excel 导入（2026-08-21 客户反馈）。

服务层：解析归一化/示例行防呆/幂等重放/整表替换/失败关闭；
API 层：权限门（无 action 403）、GET 清单、模板下载。
"""

import io
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.auth import hash_password
from sqlalchemy import select

from app.main import app
from app.models.maintenance_acceptance_checklist import (
    MaintenanceAcceptanceChecklistBatch,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.services import maintenance_acceptance_checklist as svc

_PASSWORD = "synthetic-checklist-1"


def _xlsx(rows: list[tuple], *, sheet: str = "验收清单", header_row: int = 1) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for _ in range(header_row - 1):
        ws.append([])
    ws.append(["验收需求", "是否完成"])
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _project(db, name="清单测试项目") -> MaintenanceProject:
    proj = MaintenanceProject(project_id=str(uuid.uuid4()),
                              project_code=name, display_name=name,
                              lifecycle_status="ongoing")
    db.add(proj)
    db.commit()
    return proj


# ---------------------------------------------------------------- 服务层

def test_parse_normalizes_done_values_and_skips_examples(db):
    data = _xlsx([
        ("（示例）会被自动忽略", "是"),
        ("设备巡检报告归档", "是"),
        ("备件损耗清单签字", "否"),
        ("培训完成", "已完成"),
        ("文档移交", "未完成"),
        ("旧设备处置", "YES"),
        ("标签清理", "n"),
        ("完成为空视为待验收", ""),
    ], header_row=2)  # 表头不在第一行也要能定位
    parsed = svc.parse_checklist_workbook(data, "checklist.xlsx")
    assert parsed["item_rows"] == 7
    assert parsed["issue_rows"] == 0
    done = {it["requirement"]: it["done"] for it in parsed["items"]}
    assert done["设备巡检报告归档"] is True
    assert done["培训完成"] is True
    assert done["旧设备处置"] is True
    assert done["备件损耗清单签字"] is False
    assert done["文档移交"] is False
    assert done["标签清理"] is False
    assert done["完成为空视为待验收"] is False


def test_parse_marks_unrecognized_done_as_issue(db):
    data = _xlsx([("正常条目", "是"), ("异常条目", "大概吧")])
    parsed = svc.parse_checklist_workbook(data, "checklist.xlsx")
    assert parsed["issue_rows"] == 1
    assert any("大概吧" in msg for it in parsed["items"] for msg in it["issues"])


def test_parse_rejects_missing_header(db):
    wb = Workbook()
    wb.active.append(["别的列", "又一列"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(svc.ChecklistParseError):
        svc.parse_checklist_workbook(buf.getvalue(), "bad.xlsx")


def _custom_xlsx(headers: list, rows: list[tuple]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "验收清单"
    ws.append(headers)
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def _raw_xlsx(
    sheet_data: str,
    *,
    dimension: str = "A1:B2",
    shared_items: list[str] | None = None,
    sheet_xml: str | None = None,
) -> bytes:
    """构造最小 OOXML：可精确控制 dimension、sharedStrings 与 worksheet XML。"""
    worksheet = sheet_xml or (
        f'<worksheet xmlns="{_MAIN_NS}">'
        f'<dimension ref="{dimension}"/>'
        f"<sheetData>{sheet_data}</sheetData>"
        "</worksheet>"
    )
    relationships = [
        f'<Relationship Id="rId1" Type="{_DOC_REL_NS}/worksheet" '
        'Target="worksheets/sheet1.xml"/>',
    ]
    overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>',
    ]
    members = {
        "_rels/.rels": (
            f'<Relationships xmlns="{_PKG_REL_NS}">'
            f'<Relationship Id="rId1" Type="{_DOC_REL_NS}/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_DOC_REL_NS}">'
            '<sheets><sheet name="验收清单" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ),
        "xl/worksheets/sheet1.xml": worksheet,
    }
    if shared_items is not None:
        relationships.append(
            f'<Relationship Id="rId2" Type="{_DOC_REL_NS}/sharedStrings" '
            'Target="sharedStrings.xml"/>'
        )
        overrides.append(
            '<Override PartName="/xl/sharedStrings.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.'
            'sharedStrings+xml"/>'
        )
        members["xl/sharedStrings.xml"] = (
            f'<sst xmlns="{_MAIN_NS}" count="{len(shared_items)}" '
            f'uniqueCount="{len(shared_items)}">'
            + "".join(shared_items) + "</sst>"
        )
    members["[Content_Types].xml"] = (
        f'<Types xmlns="{_CT_NS}">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(overrides) + "</Types>"
    )
    members["xl/_rels/workbook.xml.rels"] = (
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        + "".join(relationships) + "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buf.getvalue()


def test_parse_accepts_exactly_the_row_limit():
    rows = [(f"任务{i}", "是") for i in range(1, svc.MAX_CHECKLIST_ROWS + 1)]
    parsed = svc.parse_checklist_workbook(
        _custom_xlsx(["验收需求", "是否完成"], rows), "max.xlsx")
    assert parsed["item_rows"] == svc.MAX_CHECKLIST_ROWS
    assert parsed["items"][-1]["requirement"] == f"任务{svc.MAX_CHECKLIST_ROWS}"


def test_parse_rejects_one_row_over_limit_instead_of_truncating():
    rows = [(f"任务{i}", "是") for i in range(1, svc.MAX_CHECKLIST_ROWS + 2)]
    with pytest.raises(svc.ChecklistParseError, match="超过"):
        svc.parse_checklist_workbook(
            _custom_xlsx(["验收需求", "是否完成"], rows), "over.xlsx")


def test_parse_rejects_far_tail_data_after_blank_and_example_rows():
    """第 2001 条数据不在扫描截断点上也必须被看到并整本拒绝。"""
    rows = [(f"任务{i}", "是") for i in range(1, svc.MAX_CHECKLIST_ROWS + 1)]
    rows += [("", ""), ("", ""), ("（示例）忽略", "是"), ("尾端真实需求", "否")]
    with pytest.raises(svc.ChecklistParseError, match="超过"):
        svc.parse_checklist_workbook(
            _custom_xlsx(["验收需求", "是否完成"], rows), "tail.xlsx")


def test_parse_does_not_count_blank_or_example_rows_toward_limit():
    rows = [(f"任务{i}", "是") for i in range(1, svc.MAX_CHECKLIST_ROWS + 1)]
    rows += [("", ""), ("（示例）忽略一", "是"), ("（示例）忽略二", "否"), ("", "")]
    parsed = svc.parse_checklist_workbook(
        _custom_xlsx(["验收需求", "是否完成"], rows), "tail-ok.xlsx")
    assert parsed["item_rows"] == svc.MAX_CHECKLIST_ROWS


def test_parse_reads_key_headers_moved_beyond_first_eight_columns():
    parsed = svc.parse_checklist_workbook(
        _custom_xlsx(
            [None] * 8 + ["验收需求", "是否完成"],
            [[None] * 8 + ["右移需求", "否"]],
        ),
        "shifted.xlsx",
    )
    assert parsed["items"][0]["requirement"] == "右移需求"
    assert parsed["items"][0]["done"] is False


def test_parse_maps_reordered_key_headers_by_name():
    parsed = svc.parse_checklist_workbook(
        _custom_xlsx(["是否完成", "验收需求"], [["是", "完成列在前"]]),
        "reordered.xlsx",
    )
    assert parsed["items"][0]["requirement"] == "完成列在前"
    assert parsed["items"][0]["done"] is True


def test_parse_rejects_duplicate_key_header():
    with pytest.raises(svc.ChecklistParseError, match="重复列"):
        svc.parse_checklist_workbook(
            _custom_xlsx(["验收需求", "验收需求", "是否完成"], [["a", "b", "是"]]),
            "duplicate.xlsx",
        )


def test_over_limit_message_does_not_suggest_split_uploads():
    """整表替换语义下，分批上传会删掉上一批；拒绝文案不能误导用户拆分。"""
    rows = [(f"任务{i}", "是") for i in range(1, svc.MAX_CHECKLIST_ROWS + 2)]
    with pytest.raises(svc.ChecklistParseError) as excinfo:
        svc.parse_checklist_workbook(
            _custom_xlsx(["验收需求", "是否完成"], rows), "over.xlsx")
    message = str(excinfo.value)
    assert "拆分" not in message
    assert "整表替换" in message
    assert "删减" in message


def test_parse_rejects_key_header_beyond_column_scan_limit():
    """关键列可以正常右移，但不能右移到极右列放大整表扫描面积。"""
    headers = [None] * svc.MAX_SCAN_COLUMNS + ["验收需求", "是否完成"]
    row = [None] * svc.MAX_SCAN_COLUMNS + ["右移过头", "是"]
    with pytest.raises(svc.ChecklistParseError, match="可扫描上限"):
        svc.parse_checklist_workbook(
            _custom_xlsx(headers, [row]), "too-wide.xlsx")


def test_parse_rejects_total_scanned_cells_beyond_limit():
    """宽表 + 长 dimension 不能因为删掉 max_col=8 就无限物化单元格。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "验收清单"
    ws.cell(row=1, column=32, value="验收需求")
    ws.cell(row=1, column=33, value="是否完成")
    ws.cell(row=svc.MAX_SCAN_ROWS, column=33, value="尾端占位")
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(svc.ChecklistParseError, match="单元格"):
        svc.parse_checklist_workbook(buf.getvalue(), "too-large-scan.xlsx")


def test_parse_shared_string_index_zero_empty_and_rich_text():
    shared = [
        "<si><t>索引零需求</t></si>",
        "<si><t>验收需求</t></si>",
        "<si><t>是否完成</t></si>",
        "<si><t></t></si>",
        "<si><r><t>富文本</t></r><r><t>需求</t></r></si>",
        "<si><t>是</t></si>",
    ]
    sheet_data = (
        '<row r="1"><c r="A1" t="s"><v>1</v></c>'
        '<c r="B1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>0</v></c>'
        '<c r="B2" t="s"><v>5</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>4</v></c>'
        '<c r="B3" t="s"><v>5</v></c></row>'
        '<row r="4"><c r="A4" t="s"><v>3</v></c>'
        '<c r="B4" t="s"><v>3</v></c></row>'
    )
    parsed = svc.parse_checklist_workbook(
        _raw_xlsx(sheet_data, dimension="A1:B4", shared_items=shared),
        "shared.xlsx",
    )
    assert [(it["requirement"], it["done"]) for it in parsed["items"]] == [
        ("索引零需求", True),
        ("富文本需求", True),
    ]


@pytest.mark.parametrize("bad_index", ["99", "abc", ""])
def test_parse_rejects_broken_shared_string_index(bad_index):
    shared = ["<si><t>验收需求</t></si>", "<si><t>是否完成</t></si>"]
    sheet_data = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c></row>'
        f'<row r="2"><c r="A2" t="s"><v>{bad_index}</v></c></row>'
    )
    with pytest.raises(svc.ChecklistParseError, match="共享字符串"):
        svc.parse_checklist_workbook(
            _raw_xlsx(sheet_data, shared_items=shared), "bad-index.xlsx")


def test_parse_empty_shared_string_does_not_inflate_tail_bounds():
    shared = [
        "<si><t>验收需求</t></si>",
        "<si><t>是否完成</t></si>",
        "<si><t></t></si>",
        "<si><t>真实条目</t></si>",
        "<si><t>是</t></si>",
    ]
    reserved_row = svc.HEADER_SCAN_ROWS + svc.MAX_SCAN_ROWS + 1
    sheet_data = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>3</v></c>'
        '<c r="B2" t="s"><v>4</v></c></row>'
        f'<row r="{reserved_row}"><c r="A{reserved_row}" t="s"><v>2</v></c></row>'
    )
    parsed = svc.parse_checklist_workbook(
        _raw_xlsx(sheet_data, shared_items=shared), "empty-shared.xlsx")
    assert parsed["item_rows"] == 1
    assert parsed["items"][0]["requirement"] == "真实条目"


def test_parse_reads_header_beyond_underreported_dimension():
    shared = [
        "<si><t>验收需求</t></si>",
        "<si><t>是否完成</t></si>",
        "<si><t>右移需求</t></si>",
        "<si><t>否</t></si>",
    ]
    sheet_data = (
        '<row r="1"><c r="J1" t="s"><v>0</v></c>'
        '<c r="K1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="J2" t="s"><v>2</v></c>'
        '<c r="K2" t="s"><v>3</v></c></row>'
    )
    parsed = svc.parse_checklist_workbook(
        _raw_xlsx(sheet_data, dimension="A1:B2", shared_items=shared),
        "header-low-dimension.xlsx",
    )
    assert parsed["items"][0]["requirement"] == "右移需求"
    assert parsed["items"][0]["done"] is False


def test_parse_ignores_style_only_dimension_tail():
    shared = [
        "<si><t>验收需求</t></si>",
        "<si><t>是否完成</t></si>",
        "<si><t>是</t></si>",
    ]
    rows = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c></row>'
    ]
    for row_no in range(2, 7):
        rows.append(
            f'<row r="{row_no}"><c r="A{row_no}" t="inlineStr">'
            f"<is><t>任务{row_no}</t></is></c>"
            f'<c r="B{row_no}" t="s"><v>2</v></c></row>'
        )
    rows.append('<row r="1048576"><c r="A1048576"/></row>')
    parsed = svc.parse_checklist_workbook(
        _raw_xlsx("".join(rows), dimension="A1:B1048576",
                  shared_items=shared),
        "style-tail.xlsx",
    )
    assert parsed["item_rows"] == 5


def test_parse_reads_real_tail_when_dimension_is_underreported():
    shared = [
        "<si><t>验收需求</t></si>",
        "<si><t>是否完成</t></si>",
        "<si><t>尾端需求</t></si>",
        "<si><t>否</t></si>",
    ]
    sheet_data = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c></row>'
        '<row r="5000"><c r="A5000" t="s"><v>2</v></c>'
        '<c r="B5000" t="s"><v>3</v></c></row>'
    )
    parsed = svc.parse_checklist_workbook(
        _raw_xlsx(sheet_data, dimension="A1:B2", shared_items=shared),
        "far-tail.xlsx",
    )
    assert parsed["item_rows"] == 1
    assert parsed["items"][0]["row_no"] == 5000
    assert parsed["items"][0]["requirement"] == "尾端需求"


def test_parse_rejects_far_tail_2001st_item_beyond_old_scan_window():
    shared = [
        "<si><t>验收需求</t></si>",
        "<si><t>是否完成</t></si>",
        "<si><t>是</t></si>",
    ]
    rows = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c></row>'
    ]
    for row_no in range(2, svc.MAX_CHECKLIST_ROWS + 2):
        rows.append(
            f'<row r="{row_no}"><c r="A{row_no}" t="inlineStr">'
            f"<is><t>任务{row_no}</t></is></c>"
            f'<c r="B{row_no}" t="s"><v>2</v></c></row>'
        )
    rows.append(
        '<row r="5000"><c r="A5000" t="inlineStr">'
        "<is><t>第2001条</t></is></c>"
        '<c r="B5000" t="s"><v>2</v></c></row>'
    )
    with pytest.raises(svc.ChecklistParseError, match="整本拒绝"):
        svc.parse_checklist_workbook(
            _raw_xlsx("".join(rows), dimension="A1:B2",
                      shared_items=shared),
            "far-2001.xlsx",
        )


def test_parse_rejects_real_tail_beyond_scan_limit():
    tail_row = svc.HEADER_SCAN_ROWS + svc.MAX_SCAN_ROWS + 1
    sheet_data = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>验收需求</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>是否完成</t></is></c></row>'
        f'<row r="{tail_row}"><c r="A{tail_row}" t="inlineStr">'
        "<is><t>远尾真实值</t></is></c></row>"
    )
    with pytest.raises(svc.ChecklistParseError, match="可扫描上限"):
        svc.parse_checklist_workbook(
            _raw_xlsx(sheet_data), "real-far-tail.xlsx")


def test_parse_counts_uncached_formula_as_occupied_boundary():
    tail_row = svc.HEADER_SCAN_ROWS + svc.MAX_SCAN_ROWS + 1
    sheet_data = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>验收需求</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>是否完成</t></is></c></row>'
        f'<row r="{tail_row}"><c r="A{tail_row}"><f>1+1</f></c></row>'
    )
    with pytest.raises(svc.ChecklistParseError, match="可扫描上限"):
        svc.parse_checklist_workbook(
            _raw_xlsx(sheet_data), "formula-tail.xlsx")


def test_parse_cached_formula_zero_and_false_are_values():
    sheet_data = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>验收需求</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>是否完成</t></is></c></row>'
        '<row r="2"><c r="A2"><f>1+1</f><v>0</v></c>'
        '<c r="B2" t="b"><v>0</v></c></row>'
    )
    parsed = svc.parse_checklist_workbook(
        _raw_xlsx(sheet_data), "zero-false.xlsx")
    assert parsed["item_rows"] == 1
    assert parsed["items"][0]["requirement"] == "0"
    assert parsed["items"][0]["done"] is False


def test_parse_rejects_actual_cell_nodes_beyond_limit(monkeypatch):
    monkeypatch.setattr(svc, "MAX_SCAN_CELLS", 10)
    sheet_data = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>验收需求</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>是否完成</t></is></c></row>'
        "<row r=\"2\">"
        + "".join(
            f'<c r="{column}2"/>'
            for column in "ABCDEFGHIJK"
        )
        + "</row>"
    )
    with pytest.raises(svc.ChecklistParseError, match="实际单元格数量"):
        svc.parse_checklist_workbook(
            _raw_xlsx(sheet_data), "too-many-cells.xlsx")


def test_parse_rejects_xml_element_budget(monkeypatch):
    monkeypatch.setattr(svc, "MAX_CHECKLIST_XML_ELEMENTS", 10)
    shared = ["<si><t>验收需求</t></si>", "<si><t>是否完成</t></si>"]
    sheet_data = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>条目</t></is></c></row>'
    )
    with pytest.raises(svc.ChecklistParseError, match="XML 元素"):
        svc.parse_checklist_workbook(
            _raw_xlsx(sheet_data, shared_items=shared), "too-many-elements.xlsx")


def test_parse_rejects_worksheet_xml_depth():
    nested = "<x>" * 70 + "</x>" * 70
    sheet_xml = (
        f'<worksheet xmlns="{_MAIN_NS}">'
        f"<sheetData>{nested}</sheetData></worksheet>"
    )
    with pytest.raises(svc.ChecklistParseError, match="层级"):
        svc.parse_checklist_workbook(
            _raw_xlsx("", sheet_xml=sheet_xml), "too-deep.xlsx")


def _preview(db, project, data, *, key=None, by="tester"):
    parsed = svc.parse_checklist_workbook(data, "checklist.xlsx")
    batch_id = svc.store_preview(db, parsed, project_id=project.project_id,
                                 uploaded_by=by,
                                 idempotency_key=key or f"key-{uuid.uuid4()}")
    # replay 分支对幂等冲突会 db.rollback()（API 层已 commit 的生产路径语义）——
    # 测试直连服务层必须先提交，否则回滚撤掉刚 flush 的批次
    db.commit()
    return batch_id


def test_store_preview_idempotent_replay(db):
    project = _project(db)
    key = "replay-key-123456"
    data = _xlsx([("条目一", "是")])
    bid1 = _preview(db, project, data, key=key)
    bid2 = _preview(db, project, data, key=key)
    assert bid1 == bid2
    # 同 key 不同文件 → 冲突
    other = _xlsx([("条目二", "否")])
    with pytest.raises(svc.ChecklistBatchError):
        _preview(db, project, other, key=key)


def test_apply_replaces_current_and_keeps_history(db):
    project = _project(db)
    bid1 = _preview(db, project, _xlsx([("旧条目", "是")]))
    result1 = svc.apply_batch(db, bid1, operated_by="tester")
    assert result1["item_rows"] == 1
    assert result1["replaced_batch_id"] is None

    bid2 = _preview(db, project, _xlsx([("新条目A", "是"), ("新条目B", "否")]))
    result2 = svc.apply_batch(db, bid2, operated_by="tester")
    assert result2["replaced_batch_id"] == bid1

    payload = svc.project_checklist(db, project.project_id)
    assert payload["current"]["batch_id"] == bid2
    assert [it["requirement"] for it in payload["current"]["items"]] \
        == ["新条目A", "新条目B"]
    assert payload["current"]["done_rows"] == 1
    assert payload["current"]["todo_rows"] == 1
    assert [h["batch_id"] for h in payload["history"]] == [bid2, bid1]


def test_apply_rejects_issue_rows_fail_closed(db):
    project = _project(db)
    good = _preview(db, project, _xlsx([("好条目", "是")]))
    svc.apply_batch(db, good, operated_by="tester")
    bad = _preview(db, project, _xlsx([("坏条目", "也许")]))
    with pytest.raises(svc.ChecklistBatchError, match="问题行"):
        svc.apply_batch(db, bad, operated_by="tester")
    batch = db.get(MaintenanceAcceptanceChecklistBatch, bad)
    assert batch.status == "failed"
    # 当前清单未被污染
    payload = svc.project_checklist(db, project.project_id)
    assert payload["current"]["batch_id"] == good


def test_apply_rejects_foreign_batch(db):
    project = _project(db)
    bid = _preview(db, project, _xlsx([("条目", "是")]), by="someone-else")
    with pytest.raises(svc.ChecklistBatchError, match="本人"):
        svc.apply_batch(db, bid, operated_by="tester")


def test_template_examples_are_ignored_on_reimport(db):
    data = svc.build_template()
    # 模板原样上传：示例行全被防呆跳过 → 空清单报错，不产生垃圾行
    with pytest.raises(svc.ChecklistParseError, match="没有任何数据行"):
        svc.parse_checklist_workbook(data, "template.xlsx")


# ---------------------------------------------------------------- API 层

def _client(db, *, username: str, permissions: dict, role: str = "readonly"):
    db.add(SysUser(username=username, role=role, display_name=username,
                   password_hash=hash_password(_PASSWORD), is_active=True,
                   permissions=permissions))
    db.commit()
    client = TestClient(app)
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def test_checklist_api_permission_gate_and_roundtrip(db):
    project = _project(db)
    base = f"/api/maintenance/projects/stable/{project.project_id}/acceptance/checklist"
    allowed = _client(db, username="cl-allowed", permissions={
        "page_maintenance": True,
        "action_maintenance_acceptance_checklist_import": True,
    })
    denied = _client(db, username="cl-denied", permissions={
        "page_maintenance": True,
    })

    # 无 action：preview 403；GET 也 403（readonly 未挂靠任何项目——fail-closed）
    assert denied.post(
        f"{base}/preview",
        files={"file": ("c.xlsx", _xlsx([("条目", "是")]),
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")},
        headers={"Idempotency-Key": "denied-key-12345"},
    ).status_code == 403

    resp = allowed.get(base)
    assert resp.status_code == 403, resp.text  # readonly 无 FULL_SCOPE/挂靠

    # admin 全链路：模板 → preview → apply → GET
    admin = _client(db, username="cl-admin", role="admin", permissions={})
    template = admin.get(f"{base}/template")
    assert template.status_code == 200
    assert template.content[:2] == b"PK"  # xlsx 是 zip

    preview = admin.post(
        f"{base}/preview",
        files={"file": ("c.xlsx", _xlsx([("条目一", "是"), ("条目二", "否")]),
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")},
        headers={"Idempotency-Key": "admin-key-12345"},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["item_rows"] == 2
    assert body["will_replace_rows"] == 0

    apply_resp = admin.post(
        f"/api/maintenance/acceptance-checklist/{body['batch_id']}/apply")
    assert apply_resp.status_code == 200, apply_resp.text
    assert apply_resp.json()["item_rows"] == 2

    got = admin.get(base)
    assert got.status_code == 200, got.text
    current = got.json()["current"]
    assert current["item_rows"] == 2
    assert current["done_rows"] == 1

    # 2026-08-22 开放语义：维保页面 + 项目挂靠即可导入（不再要 action 键）。
    # 行键账号（own_maintenance_projects_only）+ primary_manager 挂靠 → 全链 200
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from app.models.system import SysUser
    from tests.boss_board_helpers import client_for

    scoped = client_for(db, username="cl-scoped-manager", role="readonly", overrides={
        "page_maintenance": True,
        "own_maintenance_projects_only": True,
    })
    scoped_user = db.scalar(
        select(SysUser).where(SysUser.username == "cl-scoped-manager"))
    assert scoped_user is not None
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        responsibility_type="primary_manager", user_id=scoped_user.id,
        source_manager_text="cl-scoped-manager", version=1, assigned_by="tester",
        assignment_reason="test"))
    db.commit()
    resp = scoped.post(
        f"{base}/preview",
        files={"file": ("c.xlsx", _xlsx([("开放导入条目", "是")]),
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")},
        headers={"Idempotency-Key": "scoped-open-key-1"},
    )
    assert resp.status_code == 200, resp.text
    applied = scoped.post(
        f"/api/maintenance/acceptance-checklist/{resp.json()['batch_id']}/apply")
    assert applied.status_code == 200, applied.text

    # 非 .xlsx 拒绝
    bad = admin.post(
        f"{base}/preview",
        files={"file": ("c.csv", b"a,b", "text/csv")},
        headers={"Idempotency-Key": "admin-key-67890"},
    )
    assert bad.status_code == 415


def test_checklist_apply_enforces_project_row_scope(db):
    """2026-08-25 行级补齐：apply 此前只校验「本人批次」，不校验项目
    可见性——越权账号拿到 batch_id 即可整表替换他人项目清单。现在加载
    批次后、整表替换前先 enforce 项目行级范围（403），且批次不被污染。"""
    project = _project(db)
    parsed = svc.parse_checklist_workbook(_xlsx([("条目", "是")]), "c.xlsx")
    batch_id = svc.store_preview(
        db, parsed, project_id=project.project_id,
        uploaded_by="cl-apply-outsider", idempotency_key="apply-scope-key-1")
    db.commit()

    # 本人批次但毫无项目挂靠（readonly + 仅 page_maintenance）→ 行级 403
    outsider = _client(db, username="cl-apply-outsider",
                       permissions={"page_maintenance": True})
    resp = outsider.post(f"/api/maintenance/acceptance-checklist/{batch_id}/apply")
    assert resp.status_code == 403

    db.expire_all()
    batch = db.get(MaintenanceAcceptanceChecklistBatch, batch_id)
    assert batch.status == "pending"
    assert svc.project_checklist(db, project.project_id)["current"] is None
