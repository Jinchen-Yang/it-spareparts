"""OOXML 单元格保真：1900/1904 日期纪元与公式缓存契约。

合成工作簿由 openpyxl 生成，公式缓存通过 ZIP 内 XML 补丁模拟；不依赖数据库。
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from datetime import date, datetime, time
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904

from app.etl import reader, xlsx_stream
from app.etl.xlsx_stream import (
    FormulaErrorCacheError,
    MissingFormulaCacheError,
    assert_formula_caches,
    check_formula_caches,
    inspect_ooxml_layout,
    iter_formula_cache_issues,
    iter_worksheet_rows,
    load_selected_frames,
)


def _rewrite_member(path: Path, member: str, transform) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if name == member:
                data = transform(data.decode("utf-8")).encode("utf-8")
            archive.writestr(name, data)


def _add_member(path: Path, member: str, content: str) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
        archive.writestr(member, content)


def _workbook(path: Path, *, mac_1904: bool = False) -> Workbook:
    workbook = Workbook()
    workbook.active.title = "Data"
    if mac_1904:
        workbook.epoch = CALENDAR_MAC_1904
    return workbook


def _sheet_xml(rows: str) -> str:
    return (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows}</sheetData></worksheet>"
    )


def _read_all(path: Path) -> list[list[object]]:
    layout = inspect_ooxml_layout(str(path))
    return list(iter_worksheet_rows(str(path), layout.sheets[0]))


_DATE_SERIALS = (0, 0.5, 1, 59, 60, 61)


def _date_workbook(path: Path, *, mac_1904: bool) -> None:
    workbook = _workbook(path, mac_1904=mac_1904)
    sheet = workbook["Data"]
    sheet["A1"] = date(2026, 10, 30)
    sheet["A1"].number_format = "yyyy-mm-dd"
    sheet["A2"] = datetime(2026, 10, 30, 12, 30)
    sheet["A2"].number_format = "yyyy-mm-dd hh:mm"
    for offset, serial in enumerate(_DATE_SERIALS, start=3):
        coordinate = f"A{offset}"
        sheet[coordinate] = serial
        sheet[coordinate].number_format = "yyyy-mm-dd hh:mm:ss"
    sheet["B1"] = 3.5
    workbook.save(path)


def _openpyxl_column_a(path: Path) -> list[object]:
    workbook = load_workbook(path, data_only=True)
    try:
        sheet = workbook["Data"]
        return [
            sheet.cell(row=index, column=1).value
            for index in range(1, sheet.max_row + 1)
        ]
    finally:
        workbook.close()


def test_default_1900_date_serials_match_openpyxl():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "dates-1900.xlsx"
        _date_workbook(path, mac_1904=False)

        layout = inspect_ooxml_layout(str(path))
        assert layout.date1904 is False
        rows = list(iter_worksheet_rows(str(path), layout.sheets[0]))

        assert [row[0] for row in rows] == _openpyxl_column_a(path)
        assert rows[0] == [datetime(2026, 10, 30), 3.5]
        assert rows[1] == [datetime(2026, 10, 30, 12, 30)]
        # openpyxl 对 0 <= 序列 < 1 返回 time；1900 纪元也一样。
        assert rows[2] == [time(0, 0)]
        assert rows[3] == [time(12, 0)]
        assert rows[4] == [datetime(1900, 1, 1)]
        # openpyxl 的 1900 闰年兼容：序列 59 与 60 都落到 1900-02-28。
        assert rows[5] == [datetime(1900, 2, 28)]
        assert rows[6] == [datetime(1900, 2, 28)]
        assert rows[7] == [datetime(1900, 3, 1)]


def test_1904_date_serials_match_openpyxl():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "dates-1904.xlsx"
        _date_workbook(path, mac_1904=True)

        layout = inspect_ooxml_layout(str(path))
        assert layout.date1904 is True
        rows = list(iter_worksheet_rows(str(path), layout.sheets[0]))

        assert [row[0] for row in rows] == _openpyxl_column_a(path)
        # 回归：1904 纪元下 2026-10-30 不得再被读成 2022-10-29。
        assert rows[0] == [datetime(2026, 10, 30), 3.5]
        assert rows[1] == [datetime(2026, 10, 30, 12, 30)]
        assert rows[2] == [time(0, 0)]
        assert rows[3] == [time(12, 0)]
        assert rows[4] == [datetime(1904, 1, 2)]
        assert rows[5] == [datetime(1904, 2, 29)]
        assert rows[6] == [datetime(1904, 3, 1)]
        assert rows[7] == [datetime(1904, 3, 2)]


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [('date1904="1"', True), ('date1904="true"', True),
     ('date1904="0"', False), ('date1904="false"', False), ("", False)],
)
def test_workbook_pr_date1904_spellings(raw_value, expected):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "epoch.xlsx"
        workbook = _workbook(path, mac_1904=True)
        sheet = workbook["Data"]
        sheet["A1"] = 0
        sheet["A1"].number_format = "yyyy-mm-dd"
        sheet["A2"] = 1
        sheet["A2"].number_format = "yyyy-mm-dd"
        workbook.save(path)
        _rewrite_member(
            path,
            "xl/workbook.xml",
            lambda xml: re.sub(r'date1904="1"', raw_value, xml, count=1),
        )

        assert inspect_ooxml_layout(str(path)).date1904 is expected
        rows = _read_all(path)
        assert rows[0][0] == time(0, 0)
        assert rows[1][0] == (
            datetime(1904, 1, 2) if expected else datetime(1900, 1, 1)
        )


def test_cached_formula_results_preserve_zero_false_and_strings():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "cached.xlsx"
        workbook = _workbook(path)
        workbook.save(path)
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml(
                '<row r="1">'
                '<c r="A1"><f>1+2</f><v>3</v></c>'
                '<c r="B1"><f>0</f><v>0</v></c>'
                '<c r="C1" t="b"><f>FALSE()</f><v>0</v></c>'
                '<c r="D1" t="str"><f>"text"</f><v>text</v></c>'
                '<c r="E1" t="str"><f>""</f><v></v></c>'
                "</row>"
            ),
        )

        assert _read_all(path) == [[3, 0, False, "text", ""]]
        assert check_formula_caches(path, ["Data"]) == []
        assert assert_formula_caches(path, ["Data"]) is None


def test_formula_without_cache_reports_sheet_and_cell():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "missing.xlsx"
        workbook = _workbook(path)
        workbook.save(path)
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml('<row r="1"><c r="A1"><f>1+2</f><v></v></c></row>'),
        )

        with pytest.raises(MissingFormulaCacheError) as caught:
            _read_all(path)
        message = str(caught.value)
        assert "工作表「Data」" in message
        assert "A1" in message
        assert "重新计算" in message
        assert caught.value.sheet == "Data"
        assert caught.value.cell == "A1"


def test_formula_without_value_element_is_not_silently_none():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "missing-v.xlsx"
        workbook = _workbook(path)
        workbook.save(path)
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml('<row r="1"><c r="B2"><f>1+2</f></c></row>'),
        )

        with pytest.raises(MissingFormulaCacheError, match="B2"):
            _read_all(path)


def test_formula_error_cache_is_explicit():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "error-cache.xlsx"
        workbook = _workbook(path)
        workbook.save(path)
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml(
                '<row r="2"><c r="B2" t="e"><f>1/0</f><v>#DIV/0!</v></c></row>'
            ),
        )

        with pytest.raises(FormulaErrorCacheError) as caught:
            _read_all(path)
        assert "B2" in str(caught.value)
        assert "#DIV/0!" in str(caught.value)


def test_shared_formula_dependent_without_cache_is_checked():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shared-formula.xlsx"
        workbook = _workbook(path)
        workbook.save(path)
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml(
                '<row r="1"><c r="A1"><f t="shared" ref="A1:A2" si="0">1+2</f><v>3</v></c></row>'
                '<row r="2"><c r="A2"><f t="shared" si="0"/><v></v></c></row>'
            ),
        )

        with pytest.raises(MissingFormulaCacheError) as caught:
            _read_all(path)
        assert caught.value.cell == "A2"


def test_inline_header_probe_is_bounded_but_full_read_reports_formula_error():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "inline-formula.xlsx"
        workbook = _workbook(path)
        workbook.save(path)
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml(
                '<row r="1"><c r="A1" t="inlineStr"><is><t>PN</t></is></c></row>'
                '<row r="2"><c r="A1" t="inlineStr"><is><t>8TB</t></is></c>'
                '<c r="B1"><f>1+2</f><v></v></c></row>'
            ),
        )

        # 表头探测（load_data=False）只读前几行，必须容许无缓存公式，
        # 否则被忽略页的坏公式会拖垮整本；选中页的全行校验在 precheck。
        inspected = reader.inspect_workbook(str(path), load_data=False)
        assert inspected[0].sheet_name == "Data"

        with pytest.raises(reader.ReaderError) as compat:
            reader.inspect_workbook(str(path), load_data=True)
        assert compat.value.code == "unsupported_cell_encoding"
        assert "B1" in str(compat.value)

        with pytest.raises(reader.ReaderError) as execute:
            reader.load_selected_workbook(str(path), ["Data"])
        assert execute.value.code == "unsupported_cell_encoding"
        assert "B1" in str(execute.value)


def test_pure_inline_cells_read_without_formulas():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "pure-inline.xlsx"
        workbook = _workbook(path)
        workbook.save(path)
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml(
                '<row r="1"><c r="A1" t="inlineStr"><is><t>PN</t></is></c>'
                '<c r="B1" t="inlineStr"><is><r><t>描述</t></r><r><t>（富文本）</t></r></is></c></row>'
                '<row r="2"><c r="A1" t="inlineStr"><is><t>8TB</t></is></c>'
                '<c r="B2"><v>2</v></c><c r="D2" t="b"><v>1</v></c></row>'
            ),
        )

        layout = inspect_ooxml_layout(str(path))
        assert layout.has_inline_strings
        assert _read_all(path) == [
            ["PN", "描述（富文本）"],
            ["8TB", 2, None, True],
        ]
        assert load_selected_frames(str(path), ["Data"])["Data"].shape == (2, 4)


def test_mixed_inline_and_shared_strings_with_cached_formula_still_reads():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "mixed-cached.xlsx"
        workbook = _workbook(path)
        workbook["Data"]["A1"] = "shared-value"
        workbook.save(path)
        _add_member(
            path,
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'count="1" uniqueCount="1"><si><t>shared-value</t></si></sst>',
        )
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml(
                '<row r="1"><c r="A1" t="inlineStr"><is><t>PN</t></is></c>'
                '<c r="B1" t="s"><v>0</v></c>'
                '<c r="C1"><f>1+2</f><v>3</v></c></row>'
            ),
        )

        assert inspect_ooxml_layout(str(path)).has_inline_strings
        assert _read_all(path) == [["PN", "shared-value", 3]]
        frame = load_selected_frames(str(path), ["Data"])["Data"]
        assert frame.iloc[0, 0] == "PN"
        assert frame.iloc[0, 1] == "shared-value"
        assert frame.iloc[0, 2] == 3


def test_ordinary_shared_string_formula_guard_and_cached_value():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shared-formulas.xlsx"
        workbook = _workbook(path)
        data = workbook["Data"]
        data["A1"] = "PN"
        data["A2"] = "=1+2"
        workbook.save(path)

        issues = check_formula_caches(path, ["Data"])
        assert [(issue.sheet, issue.cell, issue.kind) for issue in issues] == [
            ("Data", "A2", "missing")
        ]
        with pytest.raises(MissingFormulaCacheError, match="A2"):
            assert_formula_caches(path, ["Data"])

        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda xml: re.sub(
                r'(<c r="A2"><f>1\+2</f>)<v></v>',
                r"\1<v>3</v>",
                xml,
            ),
        )
        assert check_formula_caches(path, ["Data"]) == []
        assert assert_formula_caches(path, ["Data"]) is None


def test_formula_cache_scan_stops_after_first_issue(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "many-missing.xlsx"
        workbook = _workbook(path)
        workbook.save(path)
        rows = "".join(
            f'<row r="{index}"><c r="A{index}"><f>1+2</f><v></v></c></row>'
            for index in range(1, 101)
        )
        _rewrite_member(
            path,
            "xl/worksheets/sheet1.xml",
            lambda _xml: _sheet_xml(rows),
        )

        issues = iter_formula_cache_issues(path, ["Data"])
        try:
            first = next(issues)
        finally:
            issues.close()
        assert (first.sheet, first.cell, first.kind) == ("Data", "A1", "missing")

        calls = 0
        original = xlsx_stream._formula_cache_issue

        def counting(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(xlsx_stream, "_formula_cache_issue", counting)
        with pytest.raises(MissingFormulaCacheError, match="A1"):
            assert_formula_caches(path, ["Data"])
        assert calls == 1


def test_ignored_sheet_formula_does_not_block_selected_sheet():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ignored-formula.xlsx"
        workbook = _workbook(path)
        workbook["Data"]["A1"] = "PN"
        workbook.create_sheet("Ignored")["A10"] = "=9+9"
        workbook.save(path)

        assert check_formula_caches(path, ["Data"]) == []
        ignored = check_formula_caches(path, ["Ignored"])
        assert [(issue.sheet, issue.cell) for issue in ignored] == [("Ignored", "A10")]

        frame = load_selected_frames(str(path), ["Data"])["Data"]
        assert frame.iloc[0, 0] == "PN"
