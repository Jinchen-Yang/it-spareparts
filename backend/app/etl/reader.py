"""文件解析：双表头探测 + 列名规整 + 重复列校验 + 文件识别 + ffill（§6.2）；
多 sheet 工作簿逐页探测（§17.5）。"""
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from itertools import repeat
from typing import NamedTuple

import pandas as pd
from openpyxl import load_workbook as openpyxl_load_workbook

from app import config
from app.etl import cleaner, mapping, sheet_selection

_FCODE = re.compile(r"^F\d{7}$")
# 空表头单元格归一：pandas 以 dtype=object 读空单元格为 float('nan')→str 后是小写 'nan'，
# openpyxl 流式读则是 None；统一折叠为 ""，避免被误判为「重复列名」（审计 2026-06-28 I-1）。
_BLANK_HEADER = ("", "nan", "none", "<na>")
_XML_DEPTH_LIMIT = 64
_XML_NAME_LIMIT = 256
_XML_NAME_LENGTH_LIMIT = 512
_XML_MARKUP_LENGTH_LIMIT = 64 * 1024
_XML_DOCTYPE_PREFIX = b"<!DOCTYPE"
_XML_SPECIAL_PREFIXES = (b"<!--", b"<![CDATA[", b"<?", _XML_DOCTYPE_PREFIX)
_XML_SPECIAL_START = re.compile(rb"<!--|<!\[CDATA\[|<\?|<!DOCTYPE")
_XML_TAG_DELIMITER = re.compile(rb"['\">]")
_XML_COMPLETE_TAG = re.compile(rb"""<(?:[^'">]|"[^"]*"|'[^']*')*>""")
ROUNDTRIP_PROTOCOL_ID = "ITDATA_MAINT_ROUNDTRIP"
_ROUNDTRIP_METADATA_SHEET = "99_元数据"
_ROUNDTRIP_PROTOCOL_SCAN_ROWS = 256


class ReaderError(Exception):
    """无法解析（识别失败 / 重复列名 / 超限等），导致整批 failed。"""

    def __init__(self, message: str, *, code: str = "reader_error"):
        self.code = code
        super().__init__(message)


class _WorkbookSheetRef(NamedTuple):
    name: str
    rel_id: str


def _invalid_workbook_error() -> ReaderError:
    return ReaderError(
        "文件无法按 .xlsx 解析：可能是旧版 .xls 格式、非 Excel 文件或文件已损坏。"
        "请在 Excel 中打开后「另存为 → Excel 工作簿 (.xlsx)」再上传。",
        code="invalid_workbook",
    )


def _worksheet_limit_error() -> ReaderError:
    worksheet_limit = config.IMPORT_XLSX_MAX_WORKSHEETS
    return ReaderError(
        f"工作表数量超过 {worksheet_limit} 个安全上限，请精简工作簿后重试。",
        code="worksheet_limit_exceeded",
    )


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _MarkupLimitedReader:
    def __init__(self, source):
        self._source = source
        self._markup_length = 0
        self._quote = None
        self._prefix = bytearray()
        self._terminator = None
        self._tail = bytearray()

    def _consume_markup(self, length: int) -> None:
        self._markup_length += length
        if self._markup_length > _XML_MARKUP_LENGTH_LIMIT:
            raise _invalid_workbook_error()

    def _finish_markup(self) -> None:
        self._markup_length = 0
        self._quote = None
        self._prefix.clear()
        self._terminator = None
        self._tail.clear()

    def read(self, size: int = -1) -> bytes:
        read_size = (
            _XML_MARKUP_LENGTH_LIMIT
            if size < 0
            else min(size, _XML_MARKUP_LENGTH_LIMIT)
        )
        data = self._source.read(read_size)
        if b"\0" in data:
            raise _invalid_workbook_error()
        index = 0
        while index < len(data):
            if self._markup_length == 0:
                special = _XML_SPECIAL_START.search(data, index)
                special_start = special.start() if special is not None else len(data)
                markup_start = data.rfind(b"<", index, special_start)
                if markup_start >= 0:
                    complete_tag = _XML_COMPLETE_TAG.match(data, markup_start)
                    if complete_tag is None or complete_tag.end() > special_start:
                        remaining = data[markup_start:]
                        self._markup_length = 1
                        index = markup_start + 1
                        if any(
                            candidate.startswith(remaining)
                            for candidate in _XML_SPECIAL_PREFIXES
                        ):
                            self._prefix = bytearray(b"<")
                        else:
                            self._prefix.clear()
                        continue
                if special is None:
                    break
                prefix = special.group()
                if prefix == _XML_DOCTYPE_PREFIX:
                    raise _invalid_workbook_error()
                self._terminator = {
                    b"<!--": b"-->",
                    b"<![CDATA[": b"]]>",
                    b"<?": b"?>",
                }[prefix]
                self._markup_length = len(prefix)
                self._tail = bytearray(prefix[-1:])
                index = special.end()
                continue

            if self._terminator is not None:
                tail_length = len(self._tail)
                combined = bytes(self._tail) + data[index:]
                terminator_at = combined.find(self._terminator)
                if terminator_at < 0:
                    retained = len(self._terminator) - 1
                    self._tail = bytearray(combined[-retained:])
                    break
                consumed = terminator_at + len(self._terminator) - tail_length
                index += consumed
                self._finish_markup()
                continue

            if self._prefix:
                byte = data[index]
                index += 1
                self._consume_markup(1)
                self._prefix.append(byte)
                prefix = bytes(self._prefix)
                if prefix == _XML_DOCTYPE_PREFIX:
                    raise _invalid_workbook_error()
                if prefix == b"<!--":
                    self._terminator = b"-->"
                elif prefix == b"<![CDATA[":
                    self._terminator = b"]]>"
                elif prefix == b"<?":
                    self._terminator = b"?>"
                if self._terminator is not None:
                    self._tail = bytearray(prefix[-1:])
                    self._prefix.clear()
                elif not any(
                    candidate.startswith(prefix) for candidate in _XML_SPECIAL_PREFIXES
                ):
                    self._prefix.clear()
                continue

            if self._quote is not None:
                quote_at = data.find(bytes((self._quote,)), index)
                if quote_at < 0:
                    self._consume_markup(len(data) - index)
                    break
                self._consume_markup(quote_at - index + 1)
                index = quote_at + 1
                self._quote = None
                continue

            delimiter = _XML_TAG_DELIMITER.search(data, index)
            if delimiter is None:
                self._consume_markup(len(data) - index)
                break
            delimiter_at = delimiter.start()
            self._consume_markup(delimiter_at - index + 1)
            index = delimiter_at + 1
            byte = data[delimiter_at]
            if byte == ord(">"):
                self._finish_markup()
            else:
                self._quote = byte
        return data


def _safe_xml_iterparse(source):
    names: set[str] = set()
    for event, value in ET.iterparse(
        _MarkupLimitedReader(source), events=("start", "end", "start-ns")
    ):
        if event == "start-ns":
            namespace_names = value
            if any(len(name) > _XML_NAME_LENGTH_LIMIT for name in namespace_names):
                raise _invalid_workbook_error()
            names.update(namespace_names)
            if len(names) > _XML_NAME_LIMIT:
                raise _invalid_workbook_error()
            continue
        element = value
        if event == "start":
            element_names = (element.tag, *element.attrib)
            if any(len(name) > _XML_NAME_LENGTH_LIMIT for name in element_names):
                raise _invalid_workbook_error()
            names.update(element_names)
            if len(names) > _XML_NAME_LIMIT:
                raise _invalid_workbook_error()
        yield event, element


def _cell_reference_bounds(ref: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d+)", ref)
    if match is None:
        return None
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + ord(char) - ord("A") + 1
    return _row_index(match.group(2)), column


def _row_index(digits: str) -> int:
    normalized = digits.lstrip("0")
    if not normalized:
        return 0
    # Excel 最大行号为 1,048,576（7 位）；更长数字直接视为超限，
    # 避免恶意超长整数触发 Python 的转换位数限制后绕回 pandas。
    if len(normalized) > 7:
        return 10_000_000
    return int(normalized)


def _worksheet_dimension_bounds(ref: str) -> tuple[int, int] | None:
    return _cell_reference_bounds(ref.split(":", 1)[-1])


def _normalize_internal_target(*, source_path: str, target: str) -> str:
    if "\\" in target:
        raise _invalid_workbook_error()
    if target.startswith("/"):
        resolved = posixpath.normpath(target.removeprefix("/"))
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_path), target)
        )
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise _invalid_workbook_error()
    return resolved


def _worksheet_relationship_targets(
    archive: zipfile.ZipFile,
    member_names: set[str],
    sheet_refs: list[_WorkbookSheetRef],
) -> dict[str, str | None]:
    if not sheet_refs:
        return {}

    relationships_path = "xl/_rels/workbook.xml.rels"
    if relationships_path not in member_names:
        raise _invalid_workbook_error()

    rel_targets: dict[str, str | None] = {}
    required_rel_ids = {sheet.rel_id for sheet in sheet_refs}
    seen_required_rel_ids: set[str] = set()
    with archive.open(relationships_path) as relationships:
        open_elements: list[ET.Element] = []
        open_tags: list[str] = []
        saw_root = False
        for event, element in _safe_xml_iterparse(relationships):
            local_name = _xml_local_name(element.tag)
            if event == "start":
                depth = len(open_elements) + 1
                if depth == 1:
                    if local_name != "Relationships":
                        raise _invalid_workbook_error()
                    saw_root = True
                elif depth == 2:
                    if local_name != "Relationship":
                        raise _invalid_workbook_error()
                    rel_id = element.attrib.get("Id")
                    if rel_id in required_rel_ids:
                        if rel_id in seen_required_rel_ids:
                            raise _invalid_workbook_error()
                        if element.attrib.get("TargetMode", "").lower() == "external":
                            raise _invalid_workbook_error()
                        rel_type = element.attrib.get("Type", "")
                        if rel_type.endswith("/chartsheet"):
                            rel_targets[rel_id] = None
                        elif rel_type.endswith("/worksheet"):
                            target = _normalize_internal_target(
                                source_path="xl/workbook.xml",
                                target=element.attrib.get("Target", ""),
                            )
                            if target not in member_names:
                                raise _invalid_workbook_error()
                            rel_targets[rel_id] = target
                        else:
                            raise _invalid_workbook_error()
                        seen_required_rel_ids.add(rel_id)
                else:
                    raise _invalid_workbook_error()
                open_elements.append(element)
                open_tags.append(local_name)
                continue

            if not open_tags or open_tags[-1] != local_name:
                raise _invalid_workbook_error()
            parent = open_elements[-2] if len(open_elements) > 1 else None
            open_elements.pop()
            open_tags.pop()
            element.clear()
            if parent is not None:
                parent.remove(element)
    if not saw_root or seen_required_rel_ids != required_rel_ids:
        raise _invalid_workbook_error()
    return rel_targets


def _workbook_sheet_refs(
    archive: zipfile.ZipFile,
) -> list[_WorkbookSheetRef]:
    workbook_path = "xl/workbook.xml"
    member_names = set(archive.namelist())
    if workbook_path not in member_names:
        raise _invalid_workbook_error()

    sheet_refs: list[_WorkbookSheetRef] = []
    worksheet_limit = config.IMPORT_XLSX_MAX_WORKSHEETS
    with archive.open(workbook_path) as workbook:
        open_elements: list[ET.Element] = []
        open_tags: list[str] = []
        saw_root = False
        for event, element in _safe_xml_iterparse(workbook):
            local_name = _xml_local_name(element.tag)
            if event == "start":
                depth = len(open_elements) + 1
                if depth > _XML_DEPTH_LIMIT:
                    raise _invalid_workbook_error()
                parent_name = open_tags[-1] if open_tags else None
                if depth == 1:
                    if local_name != "workbook":
                        raise _invalid_workbook_error()
                    saw_root = True
                elif local_name == "sheets" and parent_name != "workbook":
                    raise _invalid_workbook_error()
                elif parent_name == "sheet":
                    raise _invalid_workbook_error()
                elif parent_name == "sheets" and local_name != "sheet":
                    raise _invalid_workbook_error()

                if local_name == "sheet":
                    if parent_name != "sheets":
                        raise _invalid_workbook_error()
                    if len(sheet_refs) >= worksheet_limit:
                        raise _worksheet_limit_error()
                    rel_id = next(
                        (
                            value
                            for name, value in element.attrib.items()
                            if _xml_local_name(name) == "id"
                        ),
                        None,
                    )
                    if rel_id is None:
                        raise _invalid_workbook_error()
                    sheet_refs.append(
                        _WorkbookSheetRef(element.attrib.get("name", ""), rel_id)
                    )

                open_elements.append(element)
                open_tags.append(local_name)
                continue

            if not open_tags or open_tags[-1] != local_name:
                raise _invalid_workbook_error()
            parent = open_elements[-2] if len(open_elements) > 1 else None
            open_elements.pop()
            open_tags.pop()
            element.clear()
            if parent is not None:
                parent.remove(element)
    if not saw_root:
        raise _invalid_workbook_error()
    return sheet_refs


def _workbook_sheet_parts(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    sheet_refs = _workbook_sheet_refs(archive)
    member_names = set(archive.namelist())
    rel_targets = _worksheet_relationship_targets(archive, member_names, sheet_refs)

    sheets: list[tuple[str, str]] = []
    for sheet in sheet_refs:
        target = rel_targets[sheet.rel_id]
        if target is not None:
            sheets.append((sheet.name, target))
    return sheets


def _enforce_worksheet_bounds(
    *,
    sheet_name: str,
    max_row: int,
    max_column: int,
    row_limit: int,
    row_total_so_far: int,
    column_limit: int,
    declared_cell_limit: int,
    declared_cells_so_far: int,
) -> None:
    if max_column > column_limit:
        raise ReaderError(
            f"工作表「{sheet_name}」声明列数超过 {column_limit} 列安全上限，"
            "请删除表格末端的异常格式或数据后重试。",
            code="column_limit_exceeded",
        )
    if row_total_so_far + max_row > row_limit:
        raise ReaderError(
            f"文件行数（全部工作表合计）超过 {row_limit} 行上限，请拆分后再导入"
            "（避免占用过多内存影响其他用户）。",
            code="row_limit_exceeded",
        )
    if declared_cells_so_far + max_row * max_column > declared_cell_limit:
        raise ReaderError(
            f"文件声明的单元格总量超过 {declared_cell_limit} 个安全上限，"
            "请删除表格末端的异常格式或数据后重试。",
            code="declared_cell_limit_exceeded",
        )


def _scan_worksheet_bounds(
    archive: zipfile.ZipFile,
    worksheet_path: str,
    *,
    sheet_name: str,
    row_limit: int,
    row_total_so_far: int,
    column_limit: int,
    declared_cell_limit: int,
    declared_cells_so_far: int,
) -> tuple[int, int]:
    max_row = 0
    max_column = 0
    current_row = 0
    current_column = 0
    with archive.open(worksheet_path) as worksheet_xml:
        open_elements: list[ET.Element] = []
        for event, element in _safe_xml_iterparse(worksheet_xml):
            if event == "start":
                depth = len(open_elements) + 1
                if depth > _XML_DEPTH_LIMIT:
                    raise _invalid_workbook_error()
                tag = _xml_local_name(element.tag)
                is_row_child = bool(
                    open_elements and _xml_local_name(open_elements[-1].tag) == "row"
                )
                if is_row_child and tag == "row":
                    raise _invalid_workbook_error()
                open_elements.append(element)
                bounds = None
                if is_row_child:
                    ref = element.attrib.get("r")
                    if ref is not None:
                        bounds = _cell_reference_bounds(ref)
                        if bounds is None:
                            raise _invalid_workbook_error()
                        row_index, column_index = bounds
                        current_row = max(current_row, row_index)
                        current_column = column_index
                    else:
                        row_index = current_row
                        current_column += 1
                        column_index = current_column
                    max_row = max(max_row, row_index)
                    max_column = max(max_column, column_index)
                elif tag == "dimension":
                    ref = element.attrib.get("ref")
                    bounds = _worksheet_dimension_bounds(ref) if ref else None
                    if bounds is not None:
                        max_row, max_column = bounds
                elif tag == "row":
                    row_ref = element.attrib.get("r")
                    if row_ref is None:
                        current_row += 1
                    elif re.fullmatch(r"[0-9]+", row_ref) is None:
                        raise _invalid_workbook_error()
                    else:
                        current_row = _row_index(row_ref)
                    current_column = 0
                    max_row = max(max_row, current_row)
                if tag in {"dimension", "row"} or is_row_child:
                    _enforce_worksheet_bounds(
                        sheet_name=sheet_name,
                        max_row=max_row,
                        max_column=max_column,
                        row_limit=row_limit,
                        row_total_so_far=row_total_so_far,
                        column_limit=column_limit,
                        declared_cell_limit=declared_cell_limit,
                        declared_cells_so_far=declared_cells_so_far,
                    )
            else:
                if _xml_local_name(element.tag) == "row":
                    current_column = 0
                parent = open_elements[-2] if len(open_elements) > 1 else None
                if open_elements:
                    open_elements.pop()
                element.clear()
                if parent is not None:
                    parent.remove(element)
    return max_row, max_column


def _check_xlsx_archive_safety(path: str) -> None:
    """只读 XLSX ZIP 中央目录并在任何解压/工作簿解析前执行资源上限。"""
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            member_limit = config.IMPORT_XLSX_MAX_MEMBERS
            if len(members) > member_limit:
                raise ReaderError(
                    f"XLSX 压缩包成员数超过 {member_limit} 个安全上限，请精简工作簿后重试。",
                    code="xlsx_too_many_members",
                )

            total_uncompressed = 0
            total_compressed = 0
            uncompressed_limit = config.IMPORT_XLSX_MAX_UNCOMPRESSED_BYTES
            ratio_limit = config.IMPORT_XLSX_MAX_COMPRESSION_RATIO
            for member in members:
                total_uncompressed += member.file_size
                total_compressed += member.compress_size
                if total_uncompressed > uncompressed_limit:
                    raise ReaderError(
                        "XLSX 解压后总大小超过安全上限，请删除无关工作表、图片或附件后重试。",
                        code="xlsx_uncompressed_size_exceeded",
                    )
                if (
                    member.file_size > 0
                    and member.file_size / max(member.compress_size, 1) > ratio_limit
                ):
                    raise ReaderError(
                        "XLSX 压缩比超过安全上限，请使用 Excel 重新保存工作簿后重试。",
                        code="xlsx_compression_ratio_exceeded",
                    )
            if (
                total_uncompressed > 0
                and total_uncompressed / max(total_compressed, 1) > ratio_limit
            ):
                raise ReaderError(
                    "XLSX 压缩比超过安全上限，请使用 Excel 重新保存工作簿后重试。",
                    code="xlsx_compression_ratio_exceeded",
                )

            _workbook_sheet_parts(archive)
    except ReaderError:
        raise
    except Exception as exc:
        raise _invalid_workbook_error() from exc


def _check_workbook_size(path: str) -> dict[str, int] | None:
    """流式探测行规模（全部 sheet 合计），超 IMPORT_MAX_ROWS 直接拒绝，
    避免 pandas 全量物化 OOM（I-2；§17.5 起多 sheet 均会被解析，按合计控）。"""
    try:
        archive = zipfile.ZipFile(path)
    except Exception:
        return None  # 损坏/非 xlsx 交给下方 pd.read_excel 给出统一「无法解析」提示
    try:
        limit = config.IMPORT_MAX_ROWS
        column_limit = config.IMPORT_XLSX_MAX_COLUMNS
        declared_cell_limit = config.IMPORT_XLSX_MAX_DECLARED_CELLS
        total = 0
        total_declared_cells = 0
        row_counts: dict[str, int] = {}
        for sheet_name, worksheet_path in _workbook_sheet_parts(archive):
            max_row, max_column = _scan_worksheet_bounds(
                archive,
                worksheet_path,
                sheet_name=sheet_name,
                row_limit=limit,
                row_total_so_far=total,
                column_limit=column_limit,
                declared_cell_limit=declared_cell_limit,
                declared_cells_so_far=total_declared_cells,
            )
            row_counts[sheet_name] = max_row
            total += max_row
            total_declared_cells += max_row * max_column
        return row_counts
    except ReaderError:
        raise
    except Exception:
        return None
    finally:
        archive.close()


def detect_header(raw: pd.DataFrame) -> int:
    """第 0 行命中 F\\d{7} ≥3 个 → 双表头(表头在第1行)，否则单表头(第0行)。"""
    first = raw.iloc[0].astype(str)
    return 1 if first.str.match(_FCODE).sum() >= 3 else 0


def _norm_cols(values) -> list[str]:
    out: list[str] = []
    for c in values:
        s = "" if c is None else str(c).strip()
        if s.lower() in _BLANK_HEADER:
            s = ""
        out.append(s)
    return out


class SheetData(NamedTuple):
    """单个可识别 sheet 的解析结果。anchor：报销页归集锚（§17.3，如 XSDD 单号），其余类型 None。

    dup_cols：表头重复列名清单。此处不抛错——只有真正要入库的页才值得整批失败，
    被跳过的页（如粘贴的副本页）带着重复列名不该拖垮整个文件；由调用方按取用情况裁决。
    """

    sheet_name: str
    df: pd.DataFrame
    file_type: str
    anchor: str | None
    dup_cols: list
    header_row: int
    data_rows: int
    columns: list[str]


class SheetInspection(NamedTuple):
    sheet_name: str
    file_type: str | None
    header_row: int | None
    data_rows: int
    columns: list[str]
    dup_cols: list[str]
    parsed: SheetData | None


_ANCHOR_LABELS = {"销售订单", "维保销售订单"}
# 表头探测扫描的行数上限：锚行(≤1) + F码行(≤1) + 表头行
_HEADER_SCAN_ROWS = 3


def _scan_anchor(raw: pd.DataFrame, upto: int) -> str | None:
    """在表头行之前的行里找归集锚：形如「销售订单 | XSDD-…」的相邻单元格对（§17.3）。"""
    for i in range(min(upto, len(raw))):
        cells = _norm_cols(raw.iloc[i].tolist())
        for j, c in enumerate(cells[:-1]):
            if mapping._strip_opt(c) in _ANCHOR_LABELS and cells[j + 1]:
                return cells[j + 1]
    return None


def _alias_value_blank(value) -> bool:
    """同义列合并层的源空白判定：None/NA 与纯空白字符串都算空。

    冲突比较（_normalize_alias_value）与实际合并（_coalesce_value_aliases）
    必须共用这一口径：只认 "" 会让「空白规范列 + 合法别名」在比较阶段放行，
    合并阶段却保留规范列空格、压掉合法别名。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _alias_nonblank_values(series: pd.Series) -> pd.Series:
    """把空白源值置 NA，供 combine_first 让合法别名补位（仅别名合并层）。"""
    blank = series.map(_alias_value_blank).astype(bool)
    return series.mask(blank)


# 非法税率在别名比较里的显式标记：必须保持非空，否则 combine_first 会让
# 合法别名悄悄补位。业务上是否拒单仍由 transform 的 D-0050 流程决定。
_INVALID_TAX_RATE_ALIAS = "invalid_tax_rate"


def _normalize_alias_value(value, internal: str | None):
    """同义列冲突比较用：空值归一为 None；身份保字符串，价格/数量按 Decimal。

    税率按 cleaner.parse_rate 的既有语义比较（13%、13.0%、0.13 等价），
    非法非空值保留非空标记而不是 None。
    """
    if _alias_value_blank(value):
        return None
    if internal in mapping.IDENTITY_INTERNALS:
        return str(value).strip()
    if internal == "is_tax_inclusive":
        parsed = cleaner.parse_tax_inclusive(value)
        if parsed is not None:
            # False 是有效业务值（不含税/未税），不能用 `or` 兜底成原文。
            return parsed
        # 非空但词表未命中：保留原文非空 marker，不能当空值让合法别名补位。
        return str(value).strip()
    if internal == "tax_rate":
        try:
            parsed = cleaner.parse_rate(value)
        except ValueError:
            return (_INVALID_TAX_RATE_ALIAS, str(value).strip())
        if parsed is None:
            # 源值非空但 parse_rate 仍返回 None（如 " nan "）：必须保留非空
            # 标记，不能当成空值让合法别名静默补位。
            return (_INVALID_TAX_RATE_ALIAS, str(value).strip())
        return parsed
    if internal in mapping.NUMERIC_ALIAS_INTERNALS:
        if isinstance(value, bool):
            return str(value).strip()
        try:
            return Decimal(str(value).strip().replace(",", ""))
        except (InvalidOperation, ValueError):
            return str(value).strip()
    return str(value).strip()


def _reject_alias_conflict(
    df: pd.DataFrame, file_type: str, target: str, aliases: list[str],
) -> None:
    """身份/价格/数量同义列同一行取值不一致 → 整批拒绝，绝不按列序悄悄取一个。"""
    internal = mapping.field_internal(file_type, target)
    if internal not in mapping.CRITICAL_ALIAS_INTERNALS:
        return
    columns = ([target] if target in df.columns else []) + aliases
    if len(columns) < 2:
        return
    combined = pd.Series(pd.NA, index=df.index, dtype=object)
    conflict = pd.Series(False, index=df.index, dtype=bool)
    for column in columns:
        values = df[column].map(lambda value: _normalize_alias_value(value, internal))
        conflict |= values.notna() & combined.notna() & (combined != values)
        combined = combined.combine_first(values)
    if not conflict.any():
        return
    first_label = conflict[conflict].index[0]
    # 去空行后索引保留源数据行相对位置，不能按位置重算行号。
    first_row = int(first_label) + 1
    pairs = "；".join(
        f"「{column}」={df.loc[[first_label], column].iloc[0]!s}"
        for column in columns
    )
    raise ReaderError(
        f"「{target}」命中多个同义列且取值不一致（数据第 {first_row} 行）：{pairs}。"
        "同义列冲突无法安全决定取哪一列，请删除重复列或统一取值后重试。",
        code="alias_column_conflict",
    )


def _coalesce_value_aliases(df: pd.DataFrame, file_type: str) -> pd.DataFrame:
    """按文档类型把同义源列合并到规范键。

    显式别名（VALUE_ALIASES，如销售裸「业务类型」）与仅注解不同的同义列
    （annotation_aliases，如「明细.采购数量」vs「明细.采购数量(必填)」）都参与：
    规范列优先、空白由别名补。身份/价格/数量同一行取值不一致时明确抛
    alias_column_conflict，不再靠 canonicalize 的「不造重复列」把别名列当未知列
    静默忽略（审计：规范数量=2、别名=99）。
    """
    alias_targets: dict[str, list[str]] = {
        target: list(aliases)
        for target, aliases in mapping.VALUE_ALIASES.get(file_type, {}).items()
    }
    for target, aliases in mapping.annotation_aliases(df.columns, file_type).items():
        alias_targets.setdefault(target, []).extend(aliases)

    drop_columns: list[str] = []
    for target, declared in alias_targets.items():
        present = []
        for alias in declared:
            if alias in df.columns and alias != target and alias not in present:
                present.append(alias)
        if target not in df.columns and not present:
            continue
        _reject_alias_conflict(df, file_type, target, present)
        values = (_alias_nonblank_values(df[target]) if target in df.columns
                  else pd.Series(pd.NA, index=df.index, dtype=object))
        for alias in present:
            values = values.combine_first(_alias_nonblank_values(df[alias]))
        df[target] = values
        drop_columns.extend(present)
    if drop_columns:
        df = df.drop(columns=drop_columns)
    return df


_SOURCE_HEAD_ECONOMIC_PAIR_ROWS = "_source_head_economic_pair_rows"


def _record_source_head_economic_pair_rows(
    df: pd.DataFrame, file_type: str,
) -> pd.DataFrame:
    """记录「不含税金额+税金」都由源行显式提供的原始行索引（ffill 之前）。

    仅采购/销售挂 attrs；空集合=该类型已检查但没有源行同时提供两项金额，
    未挂 key=该类型不参与此口径，两者必须可区分。集合按 ffill 前的源值判定，
    不允许把 ffill 补出的金额当成同源证据。不复制整表、不改任何列值。
    """
    if file_type in (mapping.PURCHASE, mapping.SALES):
        economic_columns = {
            internal: source
            for source, internal in mapping.MAPPINGS[file_type]["head"].items()
            if internal in ("amount_ex_tax", "tax_amount")
        }
        amount_column = economic_columns.get("amount_ex_tax")
        tax_column = economic_columns.get("tax_amount")
        if amount_column in df.columns and tax_column in df.columns:
            amount_blank = df[amount_column].map(_alias_value_blank)
            tax_blank = df[tax_column].map(_alias_value_blank)
            source_rows = df.index[~(amount_blank | tax_blank)]
        else:
            source_rows = df.index[:0]
        df.attrs[_SOURCE_HEAD_ECONOMIC_PAIR_ROWS] = frozenset(source_rows)
    return df


def _identity_value(value: object) -> object | None:
    if value is None or pd.isna(value) or value == "":
        return None
    return value


def _order_group_ids(
    raw_ids: Iterable[object],
    order_nos: Iterable[object],
) -> list[int]:
    current_raw = None
    current_order = None
    group_id = 0
    groups: list[int] = []

    for raw_value, order_value in zip(raw_ids, order_nos, strict=True):
        raw_id = _identity_value(raw_value)
        order_no = _identity_value(order_value)
        has_identity = raw_id is not None or order_no is not None
        raw_matches = (
            raw_id is not None
            and current_raw is not None
            and raw_id == current_raw
        )
        order_matches = (
            order_no is not None
            and current_order is not None
            and order_no == current_order
        )
        raw_conflicts = (
            raw_id is not None
            and current_raw is not None
            and raw_id != current_raw
        )
        order_conflicts = (
            order_no is not None
            and current_order is not None
            and order_no != current_order
        )
        starts_new_group = has_identity and (
            group_id == 0
            or raw_conflicts
            or order_conflicts
            or not (raw_matches or order_matches)
        )

        if starts_new_group:
            group_id += 1
            current_raw = raw_id
            current_order = order_no
        else:
            if raw_id is not None and current_raw is None:
                current_raw = raw_id
            if order_no is not None and current_order is None:
                current_order = order_no

        groups.append(group_id)

    return groups


def _ffill_head_columns(df: pd.DataFrame, file_type: str) -> pd.DataFrame:
    ffill_cols = [c for c in mapping.FFILL_COLS[file_type] if c in df.columns]
    if not ffill_cols:
        return df
    identity_cols = {
        internal: source for source, internal in mapping.MAPPINGS[file_type]["head"].items()
        if internal in ("raw_order_id", "order_no") and source in df.columns
    }
    if not identity_cols:
        return df
    raw_ids: Iterable[object] = (
        df[identity_cols["raw_order_id"]].array
        if "raw_order_id" in identity_cols else repeat(None, len(df))
    )
    order_nos: Iterable[object] = (
        df[identity_cols["order_no"]].array
        if "order_no" in identity_cols else repeat(None, len(df))
    )
    order_groups = pd.Series(
        _order_group_ids(raw_ids, order_nos),
        index=df.index,
    )
    df[ffill_cols] = (
        df[ffill_cols].replace("", pd.NA).groupby(order_groups, sort=False).ffill()
    )
    return df


def _drop_blank_data_rows(df: pd.DataFrame) -> pd.DataFrame:
    """去掉全空数据行：空白行不是业务事实，不能变成 missing_raw_id 错误。

    pandas 对空单元格给 NaN，inline-string 读法给 None/空串；两种都算空白。
    只删整行全空，任何一格有值都保留（含仅明细ID的合法续行）。
    刻意不 reset 索引：索引值即源数据行相对位置（表头后首行为 0），后续
    别名冲突/transform/重复明细的「数据第 N 行」才能落在真实源行上。
    """
    if df.empty:
        return df
    if not df.columns.is_unique:
        # 空表头单元格会产生多个 "" 列；重复列校验另有职责，这里不猜列。
        return df
    blank = df.isna()
    for column in df.columns:
        series = df[column]
        if series.dtype == object or pd.api.types.is_string_dtype(series.dtype):
            blank[column] = blank[column] | series.map(
                lambda value: isinstance(value, str) and not value.strip()
            )
    return df.loc[~blank.all(axis=1)]


def _inspect_frame(
    raw: pd.DataFrame,
    sheet_name: str,
    *,
    load_data: bool,
    row_count: int | None,
) -> SheetInspection:
    """单 sheet：前 _HEADER_SCAN_ROWS 行内找可识别表头，并保留未识别页的元数据。

    兼容既有双表头（第 0 行 F 码、第 1 行真表头：F 码行识别不出类型，自然落到第 1 行）
    与报销页锚行（第 1 行锚、第 2 行表头，同理）。重复列校验只记录不抛错（见 SheetData）。
    """
    if raw.empty:
        return SheetInspection(sheet_name, None, None, 0, [], [], None)
    for h in range(min(_HEADER_SCAN_ROWS, len(raw))):
        raw_cols = _norm_cols(raw.iloc[h].tolist())
        file_type = mapping.detect_file_type(raw_cols)
        if file_type is None:
            continue
        # 先识别文档类型，再按该类型自己的注册键归一：裸「税率」在采购归
        # 采购「税率(必填)」、在销售归销售裸键，不会被另一类型的规范键截走。
        cols = mapping.canonicalize_columns(raw_cols, file_type)
        dup = [c for c, n in Counter(cols).items() if n > 1 and c != ""]
        data_rows = (
            len(raw) - h - 1
            if load_data or row_count is None
            else max(row_count - h - 1, 0)
        )
        anchor = _scan_anchor(raw, h) if file_type == mapping.EXPENSE else None
        parsed = None
        if load_data:
            df = raw.iloc[h + 1:].reset_index(drop=True)
            df.columns = cols
            if not dup:
                df = _drop_blank_data_rows(df)
                df = _coalesce_value_aliases(df, file_type)
                df = _record_source_head_economic_pair_rows(df, file_type)
                df = _ffill_head_columns(df, file_type)
            parsed = SheetData(
                sheet_name,
                df,
                file_type,
                anchor,
                dup,
                h + 1,
                data_rows,
                cols,
            )
        return SheetInspection(
            sheet_name,
            file_type,
            h + 1,
            data_rows,
            cols,
            dup,
            parsed,
        )
    first_cols = mapping.canonicalize_columns(_norm_cols(raw.iloc[0].tolist()))
    data_rows = len(raw) if load_data or row_count is None else row_count
    return SheetInspection(sheet_name, None, None, data_rows, first_cols, [], None)


def require_clean_columns(sheet: SheetData) -> None:
    """入库前置校验：将被取用的页若有重复列名 → 整批拒绝（数据质量问题必须响）。"""
    if sheet.dup_cols:
        raise ReaderError(
            f"工作表「{sheet.sheet_name}」表头存在重复列名：{sheet.dup_cols}，请确认导出模版")


def reject_roundtrip_workbook(path: str) -> None:
    """让通用导入在任何 hash 去重或业务写入前拒绝固定回填协议。

    只扫描元数据表前 256 行两列，不信任 worksheet dimension；ZIP 与 XML 资源上限仍
    先复用正式 reader 的安全闸门。损坏文件继续交给正式解析路径给出统一错误。
    """
    _check_xlsx_archive_safety(path)
    _check_workbook_size(path)
    try:
        workbook = openpyxl_load_workbook(
            path,
            read_only=True,
            data_only=False,
            keep_links=False,
        )
    except Exception:
        return
    try:
        if _ROUNDTRIP_METADATA_SHEET not in workbook.sheetnames:
            return
        worksheet = workbook[_ROUNDTRIP_METADATA_SHEET]
        for key, value in worksheet.iter_rows(
            min_row=1,
            max_row=_ROUNDTRIP_PROTOCOL_SCAN_ROWS,
            min_col=1,
            max_col=2,
            values_only=True,
        ):
            if str(key or "").strip() != "protocol_id":
                continue
            if str(value or "").strip() == ROUNDTRIP_PROTOCOL_ID:
                raise ReaderError(
                    "这是系统固定维保回填工作簿，请使用维保项目回填入口"
                    "（维保项目 → 导入回填），"
                    "通用数据导入不会处理该协议。",
                    code="roundtrip_wrong_endpoint",
                )
            return
    finally:
        workbook.close()


def inspect_workbook(path: str, *, load_data: bool = True) -> list[SheetInspection]:
    """统一探测全部工作表。

    普通 shared-string 工作簿继续走 pandas/openpyxl；氚云等系统写出的
    inline-string 工作簿改走 ``xlsx_stream``，避免 openpyxl read-only 丢值或
    全量加载巨大 sheet。预检最多物化表头扫描行，正式导入的选中 sheet 由
    :func:`load_selected_workbook` 单独物化。
    """
    _check_xlsx_archive_safety(path)
    row_counts = _check_workbook_size(path)
    try:
        from app.etl import xlsx_stream

        layout = xlsx_stream.inspect_ooxml_layout(path)
    except Exception as exc:
        raise ReaderError(
            "文件无法按 .xlsx 解析：可能是旧版 .xls 格式、非 Excel 文件或文件已损坏。"
            "请在 Excel 中打开后「另存为 → Excel 工作簿 (.xlsx)」再上传。",
            code="invalid_ooxml_structure",
        ) from exc
    if layout.is_mixed_or_inline:
        try:
            frames = {
                name: pd.DataFrame(
                    list(
                        xlsx_stream.iter_worksheet_rows(
                            path,
                            next(sheet for sheet in layout.sheets if sheet.name == name),
                            max_rows=None if load_data else _HEADER_SCAN_ROWS,
                            # 表头探测只读前 3 行，不能让被忽略页的公式缓存缺失
                            # 拖垮整本；选中页由 precheck/load_selected 全行校验。
                            strict_formula_cache=load_data,
                        )
                    )
                )
                for name in (sheet.name for sheet in layout.sheets)
            }
        except xlsx_stream.FormulaCacheError as exc:
            raise ReaderError(
                f"工作簿包含无法安全读取的公式缓存：{exc}",
                code="unsupported_cell_encoding",
            ) from exc
        except xlsx_stream.UnsupportedCellEncoding as exc:
            raise ReaderError(
                f"工作簿包含暂不支持的单元格编码：{exc}",
                code="unsupported_cell_encoding",
            ) from exc
        except Exception as exc:
            raise ReaderError(
                "工作簿 XML 结构无法读取，请从氚云重新导出或在 Excel 中另存为 .xlsx。",
                code="invalid_ooxml_structure",
            ) from exc
        if not frames or all(raw.empty for raw in frames.values()):
            raise ReaderError("文件为空", code="empty_workbook")
        return [
            _inspect_frame(
                frames[name],
                name,
                load_data=load_data,
                row_count=row_counts.get(name) if row_counts is not None else None,
            )
            for name in frames
        ]
    if load_data:
        # 兼容入口会物化全部工作表；普通 shared-string 路径同样不能把无缓存
        # 公式静默读成 None。表头级探测（load_data=False）不做整页扫描。
        try:
            xlsx_stream.assert_formula_caches(path)
        except xlsx_stream.FormulaCacheError as exc:
            raise ReaderError(
                f"工作簿包含无法安全读取的公式缓存：{exc}",
                code="unsupported_cell_encoding",
            ) from exc
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object,
                               engine="openpyxl",
                               nrows=None if load_data else _HEADER_SCAN_ROWS)
    except Exception as exc:  # BadZipFile / openpyxl 格式错误 / 损坏
        raise ReaderError(
            "文件无法按 .xlsx 解析：可能是旧版 .xls 格式、非 Excel 文件或文件已损坏。"
            "请在 Excel 中打开后「另存为 → Excel 工作簿 (.xlsx)」再上传。",
            code="invalid_workbook",
        ) from exc
    if not sheets or all(raw.empty for raw in sheets.values()):
        raise ReaderError("文件为空", code="empty_workbook")
    return [
        _inspect_frame(
            raw,
            name,
            load_data=load_data,
            row_count=row_counts.get(name) if row_counts is not None else None,
        )
        for name, raw in sheets.items()
    ]


def load_selected_workbook(
    path: str,
    sheet_names: list[str] | tuple[str, ...],
    *,
    inspections: list[SheetInspection] | None = None,
) -> list[SheetData]:
    """Load only the sheets selected by the preview phase.

    This is the memory boundary for multi-sheet uploads: ignored sheets are
    scanned for safety and headers, but their cell values are never materialised.
    """
    if not sheet_names:
        return []
    inspected = inspections or inspect_workbook(path, load_data=False)
    by_name = {item.sheet_name: item for item in inspected}
    try:
        from app.etl import xlsx_stream

        layout = xlsx_stream.inspect_ooxml_layout(path)
    except Exception as exc:
        raise ReaderError("工作簿 XML 结构无法读取。", code="invalid_ooxml_structure") from exc
    if layout.is_mixed_or_inline:
        try:
            frames = xlsx_stream.load_selected_frames(path, sheet_names)
        except xlsx_stream.FormulaCacheError as exc:
            raise ReaderError(
                f"工作簿包含无法安全读取的公式缓存：{exc}",
                code="unsupported_cell_encoding",
            ) from exc
        except xlsx_stream.UnsupportedCellEncoding as exc:
            raise ReaderError(
                f"工作簿包含暂不支持的单元格编码：{exc}",
                code="unsupported_cell_encoding",
            ) from exc
        except Exception as exc:
            raise ReaderError("工作簿 XML 结构无法读取。", code="invalid_ooxml_structure") from exc
    else:
        try:
            # 只校验本次选中的工作表全行：被忽略页的缺缓存公式不阻断业务页，
            # 选中页即使公式在表头之后也必须明确失败，而不是 data_only=None。
            xlsx_stream.assert_formula_caches(path, sheet_names)
            frames = pd.read_excel(
                path,
                sheet_name=list(sheet_names),
                header=None,
                dtype=object,
                engine="openpyxl",
            )
            if isinstance(frames, pd.DataFrame):
                frames = {sheet_names[0]: frames}
        except xlsx_stream.FormulaCacheError as exc:
            raise ReaderError(
                f"工作簿包含无法安全读取的公式缓存：{exc}",
                code="unsupported_cell_encoding",
            ) from exc
        except Exception as exc:
            raise ReaderError(
                "文件无法按 .xlsx 解析：可能是旧版 .xls 格式、非 Excel 文件或文件已损坏。",
                code="invalid_workbook",
            ) from exc
    result: list[SheetData] = []
    for name in sheet_names:
        raw = frames.get(name, pd.DataFrame())
        item = _inspect_frame(
            raw,
            name,
            load_data=True,
            row_count=(by_name.get(name).data_rows if by_name.get(name) else None),
        )
        if item.parsed is not None:
            result.append(item.parsed)
    return result


def read_workbook(path: str) -> list[SheetData]:
    """兼容入口：返回全部可识别页，未识别页仍不进入正式导入。"""
    return [
        sheet.parsed
        for sheet in inspect_workbook(path)
        if sheet.parsed is not None
    ]


def read_excel(path: str) -> tuple[pd.DataFrame, str]:
    """单表读取（兼容入口）：按工作簿顺序取第一个可识别 sheet → (DataFrame, file_type)。

    历史语义是 ``read_workbook()[0]``，即工作簿原始顺序的第一张可识别页；
    不能换成预检/正式导入的报销优先选表规则，否则工作簿里只要附带一张报销页，
    原本可导入的业务页就会被挤掉。这里只做有界表头探测，然后只物化并校验该页，
    被忽略页的坏公式不影响本入口。
    """
    inspected = inspect_workbook(path, load_data=False)
    first = next(
        (sheet for sheet in inspected if sheet.file_type is not None), None)
    if first is None:
        raise ReaderError(
            "无法识别文件类型，请确认是采购/销售/库存/维保出库/报销明细导出文件",
            code="no_recognized_sheet",
        )
    parsed = load_selected_workbook(
        path, [first.sheet_name], inspections=inspected)
    if not parsed:
        raise ReaderError(
            "无法识别文件类型，请确认是采购/销售/库存/维保出库/报销明细导出文件",
            code="no_recognized_sheet",
        )
    require_clean_columns(parsed[0])
    return parsed[0].df, parsed[0].file_type


def peek_columns(path: str) -> tuple[list[str], str | None]:
    """兼容入口：复用正式 reader 与共享选表规则，返回首个选中页列名与文件类型。"""
    sheets = inspect_workbook(path, load_data=False)
    selection = sheet_selection.select_workbook_sheets(sheets)
    if selection.selected:
        return selection.selected[0].columns, selection.file_type
    return sheets[0].columns, None
