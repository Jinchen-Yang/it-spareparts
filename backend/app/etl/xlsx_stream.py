"""Small, bounded OOXML value reader used for non-standard XLSX exports.

Some upstream systems write every text cell as ``inlineStr`` instead of using
``sharedStrings.xml``.  openpyxl's read-only path has historically been
unreliable for those files, so this module reads only the worksheet XML that
the import pipeline selected.  It deliberately does not replace the existing
ZIP/XML safety scanner in :mod:`app.etl.reader`.

The reader never evaluates formulas: only cached results are accepted, and a
formula without a usable cache raises :class:`FormulaCacheError` naming the
worksheet and cell.  Date serials follow ``workbookPr@date1904`` and openpyxl's
1900/1904 conversion rules.
"""

from __future__ import annotations

import posixpath
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, time
from typing import Iterator, Sequence
import xml.etree.ElementTree as ET

import pandas as pd
from openpyxl.utils.datetime import (
    CALENDAR_MAC_1904,
    CALENDAR_WINDOWS_1900,
    from_excel,
)


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DATE_BUILTIN_IDS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47}
_DATE_FORMAT_RE = re.compile(r"(?:yy|yyyy|dd|mm|m|d)", re.I)
_TIME_FORMAT_RE = re.compile(r"(?:h|hh|ss|s)", re.I)


class UnsupportedCellEncoding(ValueError):
    """The workbook is structurally valid but uses a value type we do not support."""


class FormulaCacheError(UnsupportedCellEncoding):
    """A formula cell has no usable cached result.

    The streaming reader never evaluates formulas.  A formula without a cached
    value must fail loudly instead of becoming ``None`` (the historical
    openpyxl ``data_only=True`` behaviour).
    """

    def __init__(self, sheet: str, cell: str, *, detail: str):
        self.sheet = sheet
        self.cell = cell
        super().__init__(f"工作表「{sheet}」的单元格 {cell} {detail}")


class MissingFormulaCacheError(FormulaCacheError):
    def __init__(self, sheet: str, cell: str):
        super().__init__(
            sheet,
            cell,
            detail=(
                "公式没有缓存值，请在 Excel/WPS/LibreOffice 中重新计算并另存后重试。"
            ),
        )


class FormulaErrorCacheError(FormulaCacheError):
    def __init__(self, sheet: str, cell: str, cached_value: str | None):
        super().__init__(
            sheet,
            cell,
            detail=(
                f"公式缓存为错误值「{cached_value or '（空）'}」，"
                "请修正公式并重新计算后保存。"
            ),
        )


@dataclass(frozen=True)
class FormulaCacheIssue:
    """A formula cell whose cached result cannot be trusted as an imported value."""

    sheet: str
    cell: str
    kind: str  # "missing" | "error"
    cached_value: str | None = None

    def as_exception(self) -> FormulaCacheError:
        if self.kind == "error":
            return FormulaErrorCacheError(self.sheet, self.cell, self.cached_value)
        return MissingFormulaCacheError(self.sheet, self.cell)


@dataclass(frozen=True)
class WorksheetRef:
    name: str
    path: str


@dataclass(frozen=True)
class WorkbookLayout:
    sheets: tuple[WorksheetRef, ...]
    has_shared_strings: bool
    worksheet_encodings: dict[str, frozenset[str]]
    date1904: bool = False

    @property
    def has_inline_strings(self) -> bool:
        return any("inlineStr" in values for values in self.worksheet_encodings.values())

    @property
    def is_mixed_or_inline(self) -> bool:
        return self.has_inline_strings


def _tag(name: str) -> str:
    return f"{{{NS_MAIN}}}{name}"


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _xml_root(archive: zipfile.ZipFile, member: str) -> ET.Element:
    try:
        with archive.open(member) as source:
            return ET.parse(source).getroot()
    except (KeyError, ET.ParseError, OSError) as exc:
        raise ValueError(f"invalid OOXML member: {member}") from exc


def _relationship_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    root = _xml_root(archive, "xl/_rels/workbook.xml.rels")
    out: dict[str, str] = {}
    for rel in root:
        if _local(rel.tag) != "Relationship":
            continue
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if not rel_id or not target:
            continue
        out[rel_id] = (
            posixpath.normpath(target.lstrip("/"))
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
    return out


def _sheet_refs(
    archive: zipfile.ZipFile, workbook: ET.Element | None = None
) -> list[WorksheetRef]:
    if workbook is None:
        workbook = _xml_root(archive, "xl/workbook.xml")
    relationships = _relationship_targets(archive)
    refs: list[WorksheetRef] = []
    sheets = workbook.find(_tag("sheets"))
    if sheets is None:
        raise ValueError("workbook has no sheets")
    for sheet in sheets:
        if _local(sheet.tag) != "sheet":
            continue
        name = sheet.attrib.get("name")
        rel_id = sheet.attrib.get(f"{{{NS_REL}}}id")
        target = relationships.get(rel_id or "")
        if not name or not target:
            raise ValueError("worksheet relationship is incomplete")
        refs.append(WorksheetRef(name=name, path=target))
    if not refs:
        raise ValueError("workbook has no worksheets")
    return refs


def _workbook_date1904(workbook: ET.Element) -> bool:
    """Read ``workbookPr@date1904``; missing attribute means the 1900 system."""
    properties = next(
        (child for child in workbook if _local(child.tag) == "workbookPr"),
        None,
    )
    if properties is None:
        return False
    raw = properties.attrib.get("date1904")
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in {"1", "true"}:
        return True
    if value in {"0", "false"}:
        return False
    raise UnsupportedCellEncoding(f"workbookPr date1904 值无效：{raw!r}")


def _cell_col(ref: str) -> int:
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?\d+", ref or "")
    if not match:
        raise ValueError(f"invalid cell reference: {ref!r}")
    value = 0
    for char in match.group(1).upper():
        value = value * 26 + ord(char) - 64
    return value


def _cell_row(ref: str) -> int:
    match = re.fullmatch(r"\$?[A-Za-z]{1,3}\$?(\d+)", ref or "")
    if not match:
        raise ValueError(f"invalid cell reference: {ref!r}")
    return int(match.group(1))


def _column_name(column: int) -> str:
    letters: list[str] = []
    while column > 0:
        column, remainder = divmod(column - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def _worksheet_encodings(archive: zipfile.ZipFile, ref: WorksheetRef) -> frozenset[str]:
    encodings: set[str] = set()
    with archive.open(ref.path) as source:
        # reader._safe_xml_iterparse adds the repository's depth/name/markup
        # limits without duplicating the safety policy here.
        from app.etl.reader import _safe_xml_iterparse

        for event, element in _safe_xml_iterparse(source):
            if event == "start" and _local(element.tag) == "c":
                encodings.add(element.attrib.get("t", "number"))
            elif event == "end" and _local(element.tag) == "c":
                element.clear()
    return frozenset(encodings)


def inspect_ooxml_layout(path: str) -> WorkbookLayout:
    """Return sheet refs and cell storage encodings without materialising values."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            workbook = _xml_root(archive, "xl/workbook.xml")
            refs = _sheet_refs(archive, workbook)
            encodings = {
                ref.name: _worksheet_encodings(archive, ref) for ref in refs
            }
            return WorkbookLayout(
                sheets=tuple(refs),
                has_shared_strings="xl/sharedStrings.xml" in names,
                worksheet_encodings=encodings,
                date1904=_workbook_date1904(workbook),
            )
    except UnsupportedCellEncoding:
        raise
    except Exception as exc:
        raise ValueError("invalid OOXML workbook") from exc


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    values: list[str] = []
    with archive.open("xl/sharedStrings.xml") as source:
        from app.etl.reader import _safe_xml_iterparse

        for event, element in _safe_xml_iterparse(source):
            if event == "end" and _local(element.tag) == "si":
                text = "".join(t.text or "" for t in element.iter() if _local(t.tag) == "t")
                values.append(sys.intern(text))
                element.clear()
    return values


def _date_style_ids(archive: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = _xml_root(archive, "xl/styles.xml")
    custom_formats: dict[int, str] = {}
    num_fmts = root.find(_tag("numFmts"))
    if num_fmts is not None:
        for item in num_fmts:
            if _local(item.tag) == "numFmt" and item.attrib.get("numFmtId"):
                custom_formats[int(item.attrib["numFmtId"])] = item.attrib.get("formatCode", "")
    date_num_fmt_ids = set(_DATE_BUILTIN_IDS)
    date_num_fmt_ids.update(
        num_id for num_id, code in custom_formats.items()
        if _DATE_FORMAT_RE.search(re.sub(r"\[[^]]+\]", "", code))
        and not ("@" in code)
    )
    cell_xfs = root.find(_tag("cellXfs"))
    if cell_xfs is None:
        return set()
    return {
        index
        for index, xf in enumerate(cell_xfs)
        if int(xf.attrib.get("numFmtId", "0")) in date_num_fmt_ids
    }


def _value_text(element: ET.Element, tag: str) -> str:
    if tag == "inlineStr":
        return "".join(child.text or "" for child in element.iter() if _local(child.tag) == "t")
    value = next((child.text for child in element if _local(child.tag) == "v"), None)
    return value or ""


def _cell_child(element: ET.Element, name: str) -> ET.Element | None:
    return next(
        (child for child in element if _local(child.tag) == name),
        None,
    )


def _formula_cache_issue(
    cell: ET.Element,
    sheet_name: str,
    coordinate: str,
) -> FormulaCacheIssue | None:
    """Return why a formula cell's cached result is unusable, if it is."""
    if _cell_child(cell, "f") is None:
        return None
    cell_type = cell.attrib.get("t")
    value_element = _cell_child(cell, "v")
    if cell_type == "e":
        return FormulaCacheIssue(
            sheet_name,
            coordinate,
            "error",
            (value_element.text or "") if value_element is not None else "",
        )
    if value_element is None:
        return FormulaCacheIssue(sheet_name, coordinate, "missing")
    raw = value_element.text or ""
    # openpyxl writes an empty <v></v> for a formula it has never calculated.
    # A cached empty *string* result is distinguishable by t="str".
    if cell_type in {None, "n", "s", "b"} and not raw:
        return FormulaCacheIssue(sheet_name, coordinate, "missing")
    if cell_type == "inlineStr" and _cell_child(cell, "is") is None:
        return FormulaCacheIssue(sheet_name, coordinate, "missing")
    return None


def _formula_serial_value(numeric: float, date1904: bool) -> object:
    """Convert an Excel date serial exactly as openpyxl's reader does.

    ``from_excel`` already implements the 1900 leap-year compatibility and
    returns ``time`` for serials in ``[0, 1)`` even in the 1904 epoch.  Do not
    special-case zero or truncate datetimes to dates here, otherwise the
    inline/mixed reader diverges from the ordinary openpyxl path.
    """
    epoch = CALENDAR_MAC_1904 if date1904 else CALENDAR_WINDOWS_1900
    try:
        converted = from_excel(numeric, epoch)
    except (OverflowError, ValueError) as exc:
        raise UnsupportedCellEncoding(
            f"Excel 日期序列号无效：{numeric!r}"
        ) from exc
    if not isinstance(converted, (datetime, time)):
        raise UnsupportedCellEncoding(f"Excel 日期序列号无效：{numeric!r}")
    return converted


def _coerce_cell(
    cell: ET.Element,
    shared: list[str],
    date_styles: set[int],
    string_cache: dict[str, str],
    *,
    sheet_name: str,
    coordinate: str,
    date1904: bool,
) -> object:
    issue = _formula_cache_issue(cell, sheet_name, coordinate)
    if issue is not None:
        raise issue.as_exception()
    cell_type = cell.attrib.get("t")
    style_id = int(cell.attrib.get("s", "0") or 0)
    if cell_type == "inlineStr":
        value = _value_text(cell, "inlineStr")
        return string_cache.setdefault(value, sys.intern(value))
    if cell_type == "s":
        raw = _value_text(cell, "s")
        try:
            return shared[int(raw)]
        except (ValueError, IndexError) as exc:
            raise UnsupportedCellEncoding("shared string index is invalid") from exc
    if cell_type == "str":
        value = _value_text(cell, "str")
        return string_cache.setdefault(value, sys.intern(value))
    raw = _value_text(cell, "number")
    if cell_type == "b":
        if raw not in {"0", "1"}:
            raise UnsupportedCellEncoding("boolean cell must be 0 or 1")
        return raw == "1"
    if cell_type in {"e", "d"}:
        raise UnsupportedCellEncoding(f"unsupported cell encoding: {cell_type}")
    if not raw:
        return None
    try:
        numeric = float(raw)
    except ValueError as exc:
        raise UnsupportedCellEncoding("numeric cell is not a number") from exc
    if style_id in date_styles:
        return _formula_serial_value(numeric, date1904)
    return int(numeric) if numeric.is_integer() else numeric


def iter_worksheet_rows(
    path: str,
    sheet: WorksheetRef,
    *,
    max_rows: int | None = None,
    date1904: bool | None = None,
) -> Iterator[list[object]]:
    """Yield worksheet rows, preserving sparse row/cell gaps as ``None``."""
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        date_styles = _date_style_ids(archive)
        if date1904 is None:
            date1904 = _workbook_date1904(_xml_root(archive, "xl/workbook.xml"))
        string_cache: dict[str, str] = {}
        with archive.open(sheet.path) as source:
            from app.etl.reader import _safe_xml_iterparse

            emitted = 0
            current_row = 0
            for event, element in _safe_xml_iterparse(source):
                if event != "end" or _local(element.tag) != "row":
                    continue
                row_number = int(element.attrib.get("r", current_row + 1))
                while current_row + 1 < row_number:
                    if max_rows is not None and emitted >= max_rows:
                        return
                    current_row += 1
                    emitted += 1
                    yield []
                values: list[object] = []
                for cell in element:
                    if _local(cell.tag) != "c":
                        continue
                    ref = cell.attrib.get("r")
                    column = _cell_col(ref) if ref else len(values) + 1
                    if column <= 0:
                        raise ValueError("invalid worksheet cell column")
                    if len(values) < column:
                        values.extend([None] * (column - len(values)))
                    coordinate = ref or f"{_column_name(column)}{row_number}"
                    values[column - 1] = _coerce_cell(
                        cell,
                        shared,
                        date_styles,
                        string_cache,
                        sheet_name=sheet.name,
                        coordinate=coordinate,
                        date1904=date1904,
                    )
                current_row = row_number
                if max_rows is not None and emitted >= max_rows:
                    return
                emitted += 1
                yield values
                element.clear()


def load_selected_frames(path: str, sheet_names: Sequence[str]) -> dict[str, pd.DataFrame]:
    """Materialise only selected sheets into object DataFrames."""
    layout = inspect_ooxml_layout(path)
    by_name = {sheet.name: sheet for sheet in layout.sheets}
    missing = [name for name in sheet_names if name not in by_name]
    if missing:
        raise ValueError(f"worksheet not found: {', '.join(missing)}")
    return {
        name: pd.DataFrame(
            list(
                iter_worksheet_rows(
                    path,
                    by_name[name],
                    date1904=layout.date1904,
                )
            )
        )
        for name in sheet_names
    }


def _iter_sheet_formula_cache_issues(
    archive: zipfile.ZipFile, sheet: WorksheetRef
) -> Iterator[FormulaCacheIssue]:
    from app.etl.reader import _safe_xml_iterparse

    current_row = 0
    current_column = 0
    with archive.open(sheet.path) as source:
        for event, element in _safe_xml_iterparse(source):
            local = _local(element.tag)
            if event == "start" and local == "row":
                current_row = int(element.attrib.get("r", current_row + 1))
                current_column = 0
            elif event == "end" and local == "c":
                ref = element.attrib.get("r")
                column = _cell_col(ref) if ref else current_column + 1
                current_column = column
                coordinate = ref or f"{_column_name(column)}{current_row}"
                issue = _formula_cache_issue(element, sheet.name, coordinate)
                element.clear()
                if issue is not None:
                    yield issue
            elif event == "end" and local == "row":
                element.clear()


def iter_formula_cache_issues(
    path: str,
    sheet_names: Sequence[str] | None = None,
) -> Iterator[FormulaCacheIssue]:
    """Yield formula-cache problems one cell at a time on selected sheets.

    Only the requested worksheets are scanned, so a formula on an ignored
    non-business sheet does not block a selected one.  The generator keeps at
    most the current XML element and issue alive; callers that only need the
    first failure must use :func:`assert_formula_caches` or stop iterating
    instead of materialising every issue in a large worksheet.
    """
    with zipfile.ZipFile(path) as archive:
        refs = _sheet_refs(archive)
        by_name = {ref.name: ref for ref in refs}
        names = (
            list(sheet_names)
            if sheet_names is not None
            else [ref.name for ref in refs]
        )
        missing = [name for name in names if name not in by_name]
        if missing:
            raise ValueError(f"worksheet not found: {', '.join(missing)}")
        for name in names:
            yield from _iter_sheet_formula_cache_issues(archive, by_name[name])


def check_formula_caches(
    path: str,
    sheet_names: Sequence[str] | None = None,
) -> list[FormulaCacheIssue]:
    """Collect formula-cache problems on selected worksheets for diagnostics.

    This is the reusable guard for the ordinary shared-string path, where
    pandas/openpyxl ``data_only=True`` silently returns ``None`` for formulas
    that were never calculated.  It materialises every issue; use
    :func:`iter_formula_cache_issues` or :func:`assert_formula_caches` when
    only the first failure is needed.
    """
    return list(iter_formula_cache_issues(path, sheet_names))


def assert_formula_caches(
    path: str,
    sheet_names: Sequence[str] | None = None,
) -> None:
    """Raise the first formula-cache problem without scanning the whole sheet."""
    issues = iter_formula_cache_issues(path, sheet_names)
    try:
        for issue in issues:
            raise issue.as_exception()
    finally:
        issues.close()
