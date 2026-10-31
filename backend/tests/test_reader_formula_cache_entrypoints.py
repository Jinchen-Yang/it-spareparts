"""真实 reader/precheck 入口的公式缓存契约（D-0050 单元格技术保真）。

同一份合成数据分别以 shared-string、inline-string、mixed 三种编码写出：
- 选中页（含表头行之后）缺缓存公式必须明确失败并给工作表+单元格；
- 被忽略页任意位置（含表头行）的缺缓存公式不得阻断选中页；
- 有效缓存 0/False/字符串/空字符串照常读取；
- 表头探测只读前几行，不整页物化；执行只物化选中页。

全部合成数据，不连接数据库。
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from openpyxl import Workbook

from app.etl import mapping, precheck, reader
from app.etl.xlsx_stream import inspect_ooxml_layout

PURCHASE_HEADERS = [
    "采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
    "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.单价(必填)",
]
PURCHASE_ROW = ["CGDD-1", "O1", "L1", "PN-A", 1, 100]
ALIAS_HEADERS = [
    "采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
    "明细.产品名称(必填)", "明细.采购数量(必填)", "明细.采购数量",
    "明细.单价(必填)",
]
EXPENSE_HEADERS = [
    "报销日期", "报销人员", "支出事由", "报销金额", "单号", "序号", "销售订单",
]
EXPENSE_ROW = ["2026-05-01", "甲", "差旅", 100, "B1", 1, "XSDD-A"]

ENCODINGS = ("shared", "inline", "mixed")


class _Encoder:
    """Render the same logical rows as shared, inline, or mixed cells."""

    def __init__(self, encoding: str) -> None:
        assert encoding in ENCODINGS
        self.encoding = encoding
        self.shared: list[str] = []
        self.index: dict[str, int] = {}
        self.shared_used = False

    def text(self, ref: str, value: str) -> str:
        use_shared = self.encoding == "shared" or (
            self.encoding == "mixed" and not self.shared_used
        )
        if not use_shared:
            return (
                f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
        self.shared_used = True
        if value not in self.index:
            self.index[value] = len(self.shared)
            self.shared.append(value)
        return f'<c r="{ref}" t="s"><v>{self.index[value]}</v></c>'

    def cell(self, ref: str, cell: object) -> str | None:
        if cell is None:
            return None
        if isinstance(cell, bool):
            return f'<c r="{ref}" t="b"><v>{1 if cell else 0}</v></c>'
        if isinstance(cell, (int, float)):
            return f'<c r="{ref}"><v>{cell}</v></c>'
        if isinstance(cell, str):
            return self.text(ref, cell)
        kind = cell[0]
        if kind != "formula":
            raise AssertionError(f"unknown cell spec: {cell!r}")
        _, expression, cached_kind, cached = cell
        attrs = {"num": "", "bool": ' t="b"', "str": ' t="str"',
                 "error": ' t="e"'}[cached_kind]
        if cached is None:
            return f'<c r="{ref}"{attrs}><f>{escape(expression)}</f></c>'
        return (
            f'<c r="{ref}"{attrs}><f>{escape(expression)}</f>'
            f"<v>{escape(str(cached))}</v></c>"
        )


def _column_name(column: int) -> str:
    letters: list[str] = []
    while column:
        column, remainder = divmod(column - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def _worksheet_xml(rows: list[list[object]], encoder: _Encoder) -> str:
    body: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = [
            encoder.cell(f"{_column_name(column)}{row_index}", value)
            for column, value in enumerate(row, start=1)
        ]
        body.append(
            f'<row r="{row_index}">' + "".join(c for c in cells if c) + "</row>"
        )
    return (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def _rewrite_member(path: Path, member: str, content: str) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if name == member:
                data = content.encode("utf-8")
            archive.writestr(name, data)


def _add_member(path: Path, member: str, content: str) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
        archive.writestr(member, content)


def _has_member(path: Path, member: str) -> bool:
    with zipfile.ZipFile(path) as archive:
        return member in archive.namelist()


def _write_shared_strings(path: Path, values: list[str]) -> None:
    count = len(values)
    content = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{count}" uniqueCount="{len(set(values))}">'
        + "".join(f"<si><t>{escape(value)}</t></si>" for value in values)
        + "</sst>"
    )
    if _has_member(path, "xl/sharedStrings.xml"):
        _rewrite_member(path, "xl/sharedStrings.xml", content)
    else:
        _add_member(path, "xl/sharedStrings.xml", content)
    with zipfile.ZipFile(path) as archive:
        content_types = archive.read("[Content_Types].xml").decode("utf-8")
    if "/xl/sharedStrings.xml" not in content_types:
        content_types = content_types.replace(
            "</Types>",
            '<Override PartName="/xl/sharedStrings.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sharedStrings+xml"/></Types>',
        )
        _rewrite_member(path, "[Content_Types].xml", content_types)


def _write_book(
    path: Path,
    sheets: list[tuple[str, list[list[object]]]],
    *,
    encoding: str,
) -> None:
    workbook = Workbook()
    first = workbook.active
    first.title = sheets[0][0]
    for name, _ in sheets[1:]:
        workbook.create_sheet(name)
    workbook.save(path)

    by_name = {ref.name: ref.path for ref in inspect_ooxml_layout(str(path)).sheets}
    encoder = _Encoder(encoding)
    for name, rows in sheets:
        _rewrite_member(path, by_name[name], _worksheet_xml(rows, encoder))
    if encoder.shared:
        _write_shared_strings(path, encoder.shared)
    elif _has_member(path, "xl/sharedStrings.xml"):
        _rewrite_member(
            path,
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'count="0" uniqueCount="0"/>',
        )


def _precheck_result(path: Path) -> dict:
    return precheck.inspect_file(str(path), path.name)


def _selected_sheet(result: dict) -> dict:
    return next(sheet for sheet in result["sheets"] if sheet["action"] == "selected")


def _codes(sheet: dict) -> list[str]:
    return [issue["code"] for issue in sheet["issues"]]


def _formula_issue(sheet: dict) -> dict:
    return next(
        issue for issue in sheet["issues"]
        if issue["code"] == "formula_cache_unavailable"
    )


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_precheck_reports_selected_deep_formula_gap(encoding):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"selected-gap-{encoding}.xlsx"
        _write_book(
            path,
            [
                ("Selected", [
                    PURCHASE_HEADERS,
                    PURCHASE_ROW,
                    [], [], [],
                    [("formula", "1+2", "num", None)],
                ]),
                ("Ignored", [PURCHASE_HEADERS, PURCHASE_ROW]),
            ],
            encoding=encoding,
        )

        # 表头级探测有界：第 6 行的坏公式不能被提前整页扫描命中。
        inspected = reader.inspect_workbook(str(path), load_data=False)
        assert [sheet.sheet_name for sheet in inspected] == ["Selected", "Ignored"]

        result = _precheck_result(path)

        assert result["can_import"] is False
        assert result["severity"] == "error"
        selected = _selected_sheet(result)
        assert "formula_cache_unavailable" in _codes(selected)
        message = _formula_issue(selected)["message"]
        assert "Selected" in message
        assert "A6" in message
        assert "重新计算" in message
        ignored = next(
            sheet for sheet in result["sheets"] if sheet["sheet_name"] == "Ignored"
        )
        assert "formula_cache_unavailable" not in _codes(ignored)

        with pytest.raises(reader.ReaderError) as execute:
            reader.load_selected_workbook(str(path), ["Selected"])
        assert execute.value.code == "unsupported_cell_encoding"
        assert "A6" in str(execute.value)

        with pytest.raises(reader.ReaderError) as compat:
            reader.read_excel(str(path))
        assert compat.value.code == "unsupported_cell_encoding"


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_precheck_reports_selected_formula_error_cache(encoding):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"selected-error-{encoding}.xlsx"
        _write_book(
            path,
            [("Selected", [
                PURCHASE_HEADERS,
                PURCHASE_ROW,
                [], [], [], [],
                [("formula", "1/0", "error", "#DIV/0!")],
            ])],
            encoding=encoding,
        )

        result = _precheck_result(path)

        assert result["can_import"] is False
        selected = _selected_sheet(result)
        message = _formula_issue(selected)["message"]
        assert "Selected" in message
        assert "A7" in message
        assert "#DIV/0!" in message

        with pytest.raises(reader.ReaderError) as execute:
            reader.load_selected_workbook(str(path), ["Selected"])
        assert execute.value.code == "unsupported_cell_encoding"
        assert "A7" in str(execute.value)


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_ignored_sheet_formula_gaps_do_not_block_selected_sheet(encoding):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"ignored-gap-{encoding}.xlsx"
        ignored_header = [*PURCHASE_HEADERS, ("formula", "1+2", "num", None)]
        _write_book(
            path,
            [
                ("Selected", [PURCHASE_HEADERS, PURCHASE_ROW]),
                ("Ignored", [
                    ignored_header,
                    PURCHASE_ROW,
                    [], [], [], [], [], [],
                    ["CGDD-9", "O9", "L9", "PN-B", 1, 10,
                     ("formula", "1+2", "num", None)],
                ]),
            ],
            encoding=encoding,
        )

        # 表头探测只读前几行且不得被忽略页拖垮。
        inspected = reader.inspect_workbook(str(path), load_data=False)
        assert [sheet.sheet_name for sheet in inspected] == ["Selected", "Ignored"]

        result = _precheck_result(path)
        assert result["can_import"] is True
        selected = _selected_sheet(result)
        assert "formula_cache_unavailable" not in _codes(selected)

        parsed = reader.load_selected_workbook(str(path), ["Selected"])
        assert len(parsed) == 1
        assert parsed[0].sheet_name == "Selected"
        assert parsed[0].df.shape[0] == 1

        # 兼容单表入口同样只读取共享选表规则选中的第一页。
        frame, file_type = reader.read_excel(str(path))
        assert file_type == mapping.PURCHASE
        assert frame.iloc[0]["采购单号(必填)"] == "CGDD-1"


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_valid_formula_caches_are_not_rejected(encoding):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"valid-cache-{encoding}.xlsx"
        headers = [*PURCHASE_HEADERS, "扩展1", "扩展2", "扩展3", "扩展4", "扩展5"]
        cached_row = [
            None, None, None, None, None, None,
            ("formula", "1+2", "num", 0),
            ("formula", "FALSE()", "bool", 0),
            ("formula", '"text"', "str", "text"),
            ("formula", '""', "str", ""),
            ("formula", "1+2", "num", 3),
        ]
        _write_book(
            path,
            [("Selected", [headers, PURCHASE_ROW, [], cached_row])],
            encoding=encoding,
        )

        result = _precheck_result(path)
        assert result["can_import"] is True, result
        assert "formula_cache_unavailable" not in _codes(_selected_sheet(result))

        frame = reader.load_selected_workbook(str(path), ["Selected"])[0].df
        values = list(frame.iloc[1, 6:11])
        assert values[:3] == [0, False, "text"]
        assert values[4] == 3
        if encoding == "shared":
            # openpyxl data_only 把 <v></v> 读成 None；缓存 guard 仍按 t="str"
            # 认定它是有效空字符串而不是缺缓存，因此不会被整表拒绝。
            assert values[3] is None or values[3] != values[3]
        else:
            assert values[3] == ""


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_selected_sheet_gap_skips_alias_materialization(encoding):
    """公式缓存已判定不可导入时，预检不能再因别名歧义重新物化并抛错。"""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"alias-gap-{encoding}.xlsx"
        _write_book(
            path,
            [("Selected", [
                ALIAS_HEADERS,
                ["CGDD-1", "O1", "L1", "PN-A", 2, 99, 100],
                [], [], [], [], [], [],
                [("formula", "1+2", "num", None)],
            ])],
            encoding=encoding,
        )

        result = _precheck_result(path)

        assert result["can_import"] is False
        selected = _selected_sheet(result)
        assert "formula_cache_unavailable" in _codes(selected)
        assert "A9" in _formula_issue(selected)["message"]


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_multiple_selected_expense_sheets_report_the_bad_sheet(encoding):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"expense-multi-{encoding}.xlsx"
        _write_book(
            path,
            [
                ("ExpenseA", [EXPENSE_HEADERS, EXPENSE_ROW]),
                ("ExpenseB", [
                    EXPENSE_HEADERS,
                    EXPENSE_ROW,
                    [], [], [], [], [], [], [],
                    [None, None, None, None, None, None, None,
                     ("formula", "1+2", "num", None)],
                ]),
            ],
            encoding=encoding,
        )

        result = _precheck_result(path)

        assert result["can_import"] is False
        assert result["selected_sheets"] == ["ExpenseA", "ExpenseB"]
        clean = next(s for s in result["sheets"] if s["sheet_name"] == "ExpenseA")
        bad = next(s for s in result["sheets"] if s["sheet_name"] == "ExpenseB")
        assert "formula_cache_unavailable" not in _codes(clean)
        assert "formula_cache_unavailable" in _codes(bad)
        assert "H10" in _formula_issue(bad)["message"]

        with pytest.raises(reader.ReaderError) as execute:
            reader.load_selected_workbook(str(path), ["ExpenseA", "ExpenseB"])
        assert execute.value.code == "unsupported_cell_encoding"
        assert "H10" in str(execute.value)
