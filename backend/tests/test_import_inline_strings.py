"""纯 OOXML 回归：不依赖数据库，覆盖氚云 inline/mixed 字符串读取。"""

import tempfile
import zipfile
from pathlib import Path

import pandas as pd

from app.etl.xlsx_stream import (
    inspect_ooxml_layout,
    iter_worksheet_rows,
    load_selected_frames,
)


def _write_workbook(path: Path) -> None:
    parts = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/xl/workbook.xml" ContentType="x"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="x"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="x"/>'
            "</Types>"
        ),
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
            "</Relationships>"
        ),
        "xl/sharedStrings.xml": (
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">'
            '<si><t>shared-value</t></si></sst>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>"
            '<row r="1"><c r="A1" t="inlineStr"><is><t>PN</t></is></c>'
            '<c r="B1" t="inlineStr"><is><r><t>描述</t></r><r><t>（富文本）</t></r></is></c>'
            '<c r="C1" t="s"><v>0</v></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>8TB</t></is></c>'
            '<c r="B2" t="n"><v>2</v></c><c r="D2" t="b"><v>1</v></c></row>'
            "</sheetData></worksheet>"
        ),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in parts.items():
            archive.writestr(name, value)


def test_inline_and_mixed_cells_are_read_with_sparse_coordinates():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "mixed.xlsx"
        _write_workbook(path)

        layout = inspect_ooxml_layout(str(path))
        assert layout.sheets[0].name == "Data"
        assert layout.has_inline_strings
        assert layout.has_shared_strings
        assert list(iter_worksheet_rows(str(path), layout.sheets[0])) == [
            ["PN", "描述（富文本）", "shared-value"],
            ["8TB", 2, None, True],
        ]

        frame = load_selected_frames(str(path), ["Data"])["Data"]
        assert frame.shape == (2, 4)
        assert frame.iloc[1, 0] == "8TB"
        assert pd.isna(frame.iloc[1, 2])
        assert frame.iloc[1, 3] is True
