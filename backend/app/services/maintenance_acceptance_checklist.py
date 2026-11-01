"""验收需求清单 Excel 导入服务（2026-08-21 客户反馈）。

两阶段（复刻 maintenance_doc_import 范式）：
1. ``parse_checklist_workbook`` + ``store_preview``——openpyxl 解析「验收需求 /
   是否完成」两列，raw 全量落库，零 canonical 写入；
2. ``apply_batch``——无问题行才生效，**整表替换**当前清单（当前 = 该项目
   applied_at 最新的批次；旧批次留档为历史，replaced_batch_id 串链）。

行级校验（fail-closed）：需求为空、是否完成无法识别 → issue 行，apply 整批拒绝。
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.etl import reader
from app.models.maintenance_acceptance_checklist import (
    MaintenanceAcceptanceChecklistBatch,
    MaintenanceAcceptanceChecklistItem,
)
from app.models.maintenance_project import MaintenanceProject
from app.services.import_safety import UploadSafetyError, validate_xlsx_zip

MAX_CHECKLIST_BYTES = 8 * 1024 * 1024
MAX_CHECKLIST_ROWS = 2000
# 资源上限：超过后明确拒绝，而不是把文件当成 2000 行截取成功/按列截断。
# 允许关键列正常右移，但拒绝把表头放到极右列后整表放大扫描面积。
MAX_SCAN_ROWS = 100_000
MAX_SCAN_COLUMNS = 256
MAX_SCAN_CELLS = 2_000_000
# 预扫是逐元素流式的；元素总数兜底防“每个单元格塞大量空白节点”。
MAX_CHECKLIST_XML_ELEMENTS = 4 * MAX_SCAN_CELLS

SHEET_CANDIDATES = ("验收清单", "验收需求")
HEADER_SCAN_ROWS = 10
COL_REQUIREMENT = "验收需求"
COL_DONE = "是否完成"

_DONE_TRUE = {"是", "已完成", "完成", "已完", "yes", "y", "true", "1"}
_DONE_FALSE = {"否", "未完成", "未完", "no", "n", "false", "0"}
EXAMPLE_PREFIX = "（示例）"

# OOXML 硬边界：行 1–1,048,576，列 1–16,384（XFD）。
_EXCEL_MAX_ROW = 1_048_576
_EXCEL_MAX_COLUMN = 16_384
# openpyxl 只用 [Content_Types].xml 的 Override 定位 sharedStrings，且取第一个；
# 这里按同一口径选部件，重复声明由我们显式拒绝。
_SHARED_STRINGS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
)


class ChecklistParseError(Exception):
    """文件不是可识别的验收清单。"""


class ChecklistBatchError(Exception):
    """批次状态/幂等冲突。"""


def _cell_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


class _ScanBudget:
    """一次导入预扫共享的资源预算（选中 sheet + 内容类型 + 共享字符串）。"""

    __slots__ = ("elements", "decompressed_bytes", "formula_cache_error")

    def __init__(self) -> None:
        self.elements = 0
        self.decompressed_bytes = 0
        # 预扫先完整走到 EOF（保留尾行/预算判定），缓存问题延后到行/列上限
        # 之后报出，避免掩盖既有的“真实尾行超限”护栏。
        self.formula_cache_error: str | None = None

    def count_element(self) -> None:
        self.elements += 1
        if self.elements > MAX_CHECKLIST_XML_ELEMENTS:
            raise ChecklistParseError(
                f"清单 XML 元素数量超过 {MAX_CHECKLIST_XML_ELEMENTS} 个安全上限，"
                "拒绝导入")

    def count_bytes(self, length: int) -> None:
        self.decompressed_bytes += length
        if self.decompressed_bytes > config.IMPORT_XLSX_MAX_UNCOMPRESSED_BYTES:
            raise ChecklistParseError(
                "清单解压后数据量超过 XLSX 解压安全上限，拒绝导入")


class _BoundedReader:
    """按实际解压字节计数，不信任 ZIP 中央目录里的声明大小。"""

    __slots__ = ("_source", "_budget")

    def __init__(self, source, budget: _ScanBudget) -> None:
        self._source = source
        self._budget = budget

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        self._budget.count_bytes(len(data))
        return data


@contextmanager
def _open_bounded_member(archive: zipfile.ZipFile, path: str,
                         budget: _ScanBudget):
    try:
        source = archive.open(path)
    except KeyError as exc:
        raise ChecklistParseError(f"Excel 文件缺少部件：{path}") from exc
    with source:
        yield _BoundedReader(source, budget)


def _check_archive_budget(archive: zipfile.ZipFile) -> None:
    total = 0
    for info in archive.infolist():
        total += info.file_size
        if total > config.IMPORT_XLSX_MAX_UNCOMPRESSED_BYTES:
            raise ChecklistParseError("清单解压后总大小超过安全上限，拒绝导入")
        if (info.file_size > 0
                and info.file_size / max(info.compress_size, 1)
                > config.IMPORT_XLSX_MAX_COMPRESSION_RATIO):
            raise ChecklistParseError("清单压缩比异常，拒绝导入")


def _selected_worksheet_part(archive: zipfile.ZipFile) -> tuple[str, str]:
    """按现有候选顺序选中 worksheet 部件，排除 chartsheet。"""
    try:
        parts = reader._workbook_sheet_parts(archive)
    except reader.ReaderError as exc:
        raise ChecklistParseError(f"无法读取 Excel 文件：{exc}") from exc
    if not parts:
        raise ChecklistParseError("工作簿没有任何工作表")
    by_name = {name: path for name, path in parts}
    for candidate in SHEET_CANDIDATES:
        if candidate in by_name:
            return candidate, by_name[candidate]
    return parts[0]


def _shared_strings_part(archive: zipfile.ZipFile,
                         budget: _ScanBudget) -> str | None:
    """按 [Content_Types].xml 的 Override 定位 sharedStrings。

    与 openpyxl 的 ``Manifest.find(SHARED_STRINGS)`` 完全同口径：只看 Override、
    忽略 workbook 关系，且不使用“标准路径优先”的猜测。openpyxl 取第一个匹配；
    重复声明属于歧义，这里显式拒绝而不是任选一个。
    """
    member_names = set(archive.namelist())
    found: str | None = None
    saw_root = False
    open_elements: list = []
    open_tags: list[str] = []
    with _open_bounded_member(archive, "[Content_Types].xml", budget) as source:
        for event, element in reader._safe_xml_iterparse(source):
            tag = reader._xml_local_name(element.tag)
            if event == "start":
                budget.count_element()
                if len(open_tags) + 1 > reader._XML_DEPTH_LIMIT:
                    raise ChecklistParseError("工作簿 XML 层级超过安全上限")
                if not open_tags:
                    if tag != "Types":
                        raise ChecklistParseError("工作簿内容类型结构无效")
                    saw_root = True
                elif tag == "Override" and open_tags[-1] == "Types":
                    if (element.attrib.get("ContentType", "")
                            == _SHARED_STRINGS_CONTENT_TYPE):
                        if found is not None:
                            raise ChecklistParseError(
                                "工作簿包含多个 sharedStrings 部件")
                        part_name = element.attrib.get("PartName", "")
                        if not part_name.startswith("/"):
                            raise ChecklistParseError(
                                "sharedStrings 部件路径无效")
                        target = part_name[1:]
                        if ("\\" in target or target in {"", ".", ".."}
                                or target.startswith("../")
                                or "/../" in target):
                            raise ChecklistParseError(
                                "sharedStrings 部件路径无效")
                        if target not in member_names:
                            raise ChecklistParseError("sharedStrings 部件不存在")
                        found = target
                open_elements.append(element)
                open_tags.append(tag)
                continue
            if not open_tags or open_tags[-1] != tag:
                raise ChecklistParseError("工作簿 XML 结构无效")
            parent = open_elements[-2] if len(open_elements) > 1 else None
            open_elements.pop()
            open_tags.pop()
            element.clear()
            if parent is not None:
                parent.remove(element)
    if not saw_root:
        raise ChecklistParseError("工作簿内容类型结构无效")
    return found


def _scan_shared_strings(archive: zipfile.ZipFile,
                         budget: _ScanBudget) -> bytearray | None:
    """只保留每条共享字符串的“空/非空”标记，不复制文本。"""
    part = _shared_strings_part(archive, budget)
    if part is None:
        return None
    flags = bytearray()
    open_elements: list = []
    open_tags: list[str] = []
    saw_root = False
    in_item = False
    item_has_text = False
    with _open_bounded_member(archive, part, budget) as source:
        for event, element in reader._safe_xml_iterparse(source):
            tag = reader._xml_local_name(element.tag)
            if event == "start":
                budget.count_element()
                if len(open_tags) + 1 > reader._XML_DEPTH_LIMIT:
                    raise ChecklistParseError("sharedStrings XML 层级超过安全上限")
                parent = open_tags[-1] if open_tags else None
                if not open_tags:
                    if tag != "sst":
                        raise ChecklistParseError("sharedStrings 结构无效")
                    saw_root = True
                elif tag == "si":
                    if parent != "sst" or in_item:
                        raise ChecklistParseError("sharedStrings 结构无效")
                    in_item = True
                    item_has_text = False
                open_elements.append(element)
                open_tags.append(tag)
                continue
            if not open_tags or open_tags[-1] != tag:
                raise ChecklistParseError("sharedStrings 结构无效")
            if (tag == "t" and in_item and "rPh" not in open_tags
                    and (element.text or "").strip()):
                item_has_text = True
            elif tag == "si":
                if not in_item:
                    raise ChecklistParseError("sharedStrings 结构无效")
                flags.append(1 if item_has_text else 0)
                in_item = False
            parent = open_elements[-2] if len(open_elements) > 1 else None
            open_elements.pop()
            open_tags.pop()
            element.clear()
            if parent is not None:
                parent.remove(element)
    if not saw_root or in_item:
        raise ChecklistParseError("sharedStrings 结构无效")
    return flags


def _formula_cache_error(
    cell_type: str,
    has_value_node: bool,
    value_text: str,
    shared_strings: bytearray | None,
) -> str | None:
    """校验公式单元格的本地缓存，避免 data_only 把缺失缓存静默读成空。

    openpyxl ``data_only=True`` 直接采用 ``<v>`` 缓存，不计算 ``<f>``；缓存缺失
    时单元格值为 None，会被当成空行跳过。这里在预扫阶段拒绝这种“看起来成功
    但丢数据”的文件。节点存在与文本非空分开判断：``t="str"`` 的空字符串结果
    合法，数字 0 / 布尔 false（``<v>0</v>``）同样合法。
    """
    if not has_value_node:
        return "公式缺少缓存结果；请用 Excel 重新保存后再导入"
    if cell_type in ("", "n"):
        if not value_text:
            return "公式的数字缓存为空；请用 Excel 重新保存后再导入"
        try:
            if "." in value_text or "E" in value_text or "e" in value_text:
                float(value_text)
            else:
                int(value_text)
        except ValueError:
            return f"公式的数字缓存无效：{value_text[:32]}"
        return None
    if cell_type == "b":
        try:
            boolean = int(value_text)
        except ValueError:
            return f"公式的布尔缓存无效：{value_text[:32]}"
        if boolean not in (0, 1):
            return f"公式的布尔缓存无效：{value_text[:32]}"
        return None
    if cell_type == "s":
        if not value_text.isdigit():
            return f"公式的共享字符串缓存无效：{value_text[:32]}"
        index = int(value_text)
        if shared_strings is None or index >= len(shared_strings):
            return "公式的共享字符串缓存越界或部件缺失"
        return None
    if cell_type == "str":
        return None  # 空字符串也是合法结果
    if cell_type in ("e", "d"):
        if not value_text:
            return "公式的错误/日期缓存为空；请用 Excel 重新保存后再导入"
        return None
    return f"公式缓存类型无效：{cell_type[:16]}"


def _scan_worksheet_value_bounds(
    archive: zipfile.ZipFile,
    worksheet_path: str,
    shared_strings: bytearray | None,
    budget: _ScanBudget,
) -> tuple[int, int]:
    """返回 (真实有值最大行, 前 HEADER_SCAN_ROWS 行有值最大列)。

    只认实际 XML 值/公式占用；dimension、row 声明和纯样式空 c 都不算边界。
    仍然逐元素扫描到 EOF，并受 cell/元素/深度/解压预算约束。
    """
    real_max_row = 0
    header_max_col = 0
    current_row = 0
    current_col = 0
    in_cell = False
    cell_row = 0
    cell_col = 0
    cell_type = ""
    cell_has_value = False
    cell_has_value_node = False
    cell_value_text = ""
    cell_has_formula = False
    cell_nodes = 0
    open_elements: list = []
    open_tags: list[str] = []
    saw_root = False
    with _open_bounded_member(archive, worksheet_path, budget) as source:
        for event, element in reader._safe_xml_iterparse(source):
            tag = reader._xml_local_name(element.tag)
            if event == "start":
                budget.count_element()
                depth = len(open_tags) + 1
                if depth > reader._XML_DEPTH_LIMIT:
                    raise ChecklistParseError("工作表 XML 层级超过安全上限")
                parent = open_tags[-1] if open_tags else None
                if depth == 1:
                    if tag != "worksheet":
                        raise ChecklistParseError("工作表 XML 结构无效")
                    saw_root = True
                elif tag == "row":
                    if in_cell or parent == "row":
                        raise ChecklistParseError("工作表 XML 结构无效")
                    row_ref = element.attrib.get("r")
                    if row_ref is None:
                        next_row = current_row + 1
                    else:
                        if re.fullmatch(r"[0-9]{1,7}", row_ref) is None:
                            raise ChecklistParseError("工作表的行号无效")
                        next_row = int(row_ref)
                    if not 1 <= next_row <= _EXCEL_MAX_ROW:
                        raise ChecklistParseError(
                            "工作表的行号超出 Excel 有效范围")
                    if next_row <= current_row:
                        raise ChecklistParseError(
                            "工作表的行号必须严格递增")
                    current_row = next_row
                    current_col = 0
                elif tag == "c" and parent == "row":
                    if in_cell:
                        raise ChecklistParseError("工作表 XML 结构无效")
                    cell_type = (element.attrib.get("t") or "").lower()
                    ref = element.attrib.get("r")
                    if ref is None:
                        current_col += 1
                        cell_row = current_row
                        cell_col = current_col
                    else:
                        bounds = reader._cell_reference_bounds(ref)
                        if bounds is None:
                            raise ChecklistParseError("工作表单元格坐标无效")
                        cell_row, cell_col = bounds
                        if not 1 <= cell_row <= _EXCEL_MAX_ROW:
                            raise ChecklistParseError(
                                "工作表单元格行号超出 Excel 有效范围")
                        if cell_row != current_row:
                            raise ChecklistParseError(
                                "工作表单元格坐标与行号不一致")
                        if cell_col <= current_col:
                            raise ChecklistParseError(
                                "工作表单元格列号必须严格递增")
                        current_col = cell_col
                    if not 1 <= cell_col <= _EXCEL_MAX_COLUMN:
                        raise ChecklistParseError(
                            "工作表单元格列号超出 Excel 有效范围")
                    in_cell = True
                    cell_has_value = False
                    cell_has_value_node = False
                    cell_value_text = ""
                    cell_has_formula = False
                elif tag == "f" and in_cell:
                    cell_has_formula = True
                open_elements.append(element)
                open_tags.append(tag)
                continue
            if not open_tags or open_tags[-1] != tag:
                raise ChecklistParseError("工作表 XML 结构无效")
            if tag == "v" and in_cell:
                text = (element.text or "").strip()
                if not cell_has_value_node:
                    cell_has_value_node = True
                    cell_value_text = text
                if cell_type == "s":
                    if not text or not text.isdigit():
                        raise ChecklistParseError("共享字符串索引无效")
                    index = int(text)
                    if shared_strings is None or index >= len(shared_strings):
                        raise ChecklistParseError(
                            "共享字符串索引越界或 sharedStrings 部件缺失")
                    if shared_strings[index]:
                        cell_has_value = True
                elif text:
                    cell_has_value = True
            elif tag == "t" and in_cell:
                if ("is" in open_tags and "rPh" not in open_tags
                        and (element.text or "").strip()):
                    cell_has_value = True
            elif tag == "c" and in_cell:
                cell_nodes += 1
                if cell_nodes > MAX_SCAN_CELLS:
                    raise ChecklistParseError(
                        f"清单实际单元格数量超过 {MAX_SCAN_CELLS} 个安全上限，"
                        "拒绝导入")
                if cell_has_formula and budget.formula_cache_error is None:
                    problem = _formula_cache_error(
                        cell_type, cell_has_value_node, cell_value_text,
                        shared_strings)
                    if problem is not None:
                        budget.formula_cache_error = (
                            f"第 {cell_row} 行第 {cell_col} 列：{problem}")
                if cell_has_value or cell_has_formula:
                    if cell_row > real_max_row:
                        real_max_row = cell_row
                    if cell_row <= HEADER_SCAN_ROWS and cell_has_value:
                        if cell_col > header_max_col:
                            header_max_col = cell_col
                in_cell = False
            elif tag == "row":
                current_col = 0
            parent = open_elements[-2] if len(open_elements) > 1 else None
            open_elements.pop()
            open_tags.pop()
            element.clear()
            if parent is not None:
                parent.remove(element)
    if not saw_root:
        raise ChecklistParseError("工作表 XML 结构无效")
    return real_max_row, header_max_col


def parse_checklist_workbook(data: bytes, filename: str) -> dict:
    """解析清单：定位表头 → 逐行归一化。返回 preview 载荷（不含 batch_id）。"""
    try:
        validate_xlsx_zip(data, max_bytes=MAX_CHECKLIST_BYTES)
    except UploadSafetyError as exc:
        raise ChecklistParseError(f"无法读取 Excel 文件：{exc}") from exc

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ChecklistParseError(
            f"无法读取 Excel 文件：{type(exc).__name__}") from exc
    wb = None
    try:
        # 预扫必须先于 openpyxl：dimension 可能被纯样式抬高或低报，不能作为
        # 真实边界；这里逐元素扫描到 EOF，并执行 cell/元素/深度/解压预算。
        try:
            _check_archive_budget(archive)
            sheet_name, worksheet_path = _selected_worksheet_part(archive)
            budget = _ScanBudget()
            shared_strings = _scan_shared_strings(archive, budget)
            real_max_row, header_max_col = _scan_worksheet_value_bounds(
                archive, worksheet_path, shared_strings, budget)
        except ChecklistParseError:
            raise
        except reader.ReaderError as exc:
            raise ChecklistParseError(f"无法读取 Excel 文件：{exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ChecklistParseError(
                f"无法读取 Excel 文件：{type(exc).__name__}") from exc

        if header_max_col == 0:
            raise ChecklistParseError(
                f"未找到表头：需要「{COL_REQUIREMENT}」列（可选「{COL_DONE}」列）")
        if header_max_col > MAX_SCAN_COLUMNS:
            raise ChecklistParseError(
                f"清单表头实际列数达到第 {header_max_col} 列，超过可扫描上限 "
                f"{MAX_SCAN_COLUMNS} 列，拒绝导入")
        if real_max_row > HEADER_SCAN_ROWS + MAX_SCAN_ROWS:
            raise ChecklistParseError(
                f"清单行数超过可扫描上限 {MAX_SCAN_ROWS} 行，拒绝导入")
        # 缓存问题在行/列/区域上限之后报出：尾行超限等既有护栏优先。
        if budget.formula_cache_error is not None:
            raise ChecklistParseError(budget.formula_cache_error)

        try:
            wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        except Exception as exc:  # noqa: BLE001
            raise ChecklistParseError(
                f"无法读取 Excel 文件：{type(exc).__name__}") from exc
        if sheet_name not in wb.sheetnames:
            raise ChecklistParseError("工作簿没有任何工作表")
        ws = wb[sheet_name]

        # 表头定位：前 HEADER_SCAN_ROWS 行内按名称找「验收需求」列。只扫描
        # 预扫得到的真实有值列宽，避免信任虚报的 dimension 造成 dense 扫表头；
        # 同名列重复即拒绝，不静默取最后一列。
        header_idx = None
        req_col = done_col = None
        header_rows = ws.iter_rows(
            min_row=1, max_row=HEADER_SCAN_ROWS, max_col=header_max_col,
            values_only=True)
        for i, row in enumerate(header_rows):
            requirement_cols: list[int] = []
            done_cols: list[int] = []
            for j, value in enumerate(row or ()):
                text = _cell_text(value)
                if text == COL_REQUIREMENT:
                    requirement_cols.append(j)
                elif text == COL_DONE:
                    done_cols.append(j)
            if not requirement_cols:
                continue
            if len(requirement_cols) > 1:
                raise ChecklistParseError(
                    f"表头「{COL_REQUIREMENT}」存在重复列，无法确定取值")
            if len(done_cols) > 1:
                raise ChecklistParseError(
                    f"表头「{COL_DONE}」存在重复列，无法确定取值")
            header_idx = i
            req_col = requirement_cols[0]
            done_col = done_cols[0] if done_cols else None
            break
        if header_idx is None or req_col is None:
            raise ChecklistParseError(
                f"未找到表头：需要「{COL_REQUIREMENT}」列（可选「{COL_DONE}」列）")

        # 数据行只读取表头实际用到的列，但一直扫描到预扫得到的真实尾行：
        # 超过 2000 条数据行不能靠 break 截取成“2000 条成功”，尾端的非空行
        # 也必须被看到。真实尾行已由 XML 预扫确定，低报 dimension 不会截断。
        needed_cols = max(req_col, done_col if done_col is not None else 0) + 1
        if needed_cols > MAX_SCAN_COLUMNS:
            raise ChecklistParseError(
                f"清单关键列位于第 {needed_cols} 列，超过可扫描上限 "
                f"{MAX_SCAN_COLUMNS} 列，拒绝导入")
        if real_max_row * needed_cols > MAX_SCAN_CELLS:
            raise ChecklistParseError(
                f"清单扫描范围超过 {MAX_SCAN_CELLS} 个单元格的安全上限"
                f"（{real_max_row} 行 × {needed_cols} 列），拒绝导入")

        items: list[dict] = []
        data_rows = ws.iter_rows(min_row=header_idx + 2, max_row=real_max_row,
                                 max_col=needed_cols, values_only=True)
        for offset, row in enumerate(data_rows):
            row_no = header_idx + offset + 2  # Excel 实际行号（1 基）
            raw_req = _cell_text(row[req_col]) if req_col < len(row) else ""
            raw_done = (_cell_text(row[done_col]) if done_col is not None
                        and done_col < len(row) else "")
            if not raw_req and not raw_done:
                continue  # 整行空白跳过
            if raw_req.startswith(EXAMPLE_PREFIX):
                continue  # 模板灰底示例行——防呆跳过（同总表 V2.3.0 示例行约定）
            if len(items) >= MAX_CHECKLIST_ROWS:
                raise ChecklistParseError(
                    f"清单数据行超过 {MAX_CHECKLIST_ROWS} 行上限，已整本拒绝导入；"
                    "本功能是整表替换，不能分批上传追加，请删减内容后重试，"
                    "或联系管理员申请扩容支持")
            done: bool | None = None
            issues: list[str] = []
            if not raw_req:
                issues.append("验收需求为空")
            if done_col is None:
                issues.append("缺少「是否完成」列")
            elif raw_done:
                lowered = raw_done.lower()
                if lowered in _DONE_TRUE:
                    done = True
                elif lowered in _DONE_FALSE:
                    done = False
                else:
                    issues.append(f"无法识别「是否完成」：{raw_done[:32]}")
            elif not issues:
                # 需求有值但完成为空：视为待验收（未完成），不拦导入
                done = False
            items.append({
                "row_no": row_no,
                "requirement": raw_req,
                "done": done,
                "raw": {"row_no": row_no, "requirement": raw_req, "done": raw_done},
                "issues": issues,
            })
        if not items:
            raise ChecklistParseError("清单没有任何数据行")
        return {
            "file_hash": hashlib.sha256(data).hexdigest(),
            "filename": filename,
            "items": items,
            "item_rows": len(items),
            "issue_rows": sum(1 for it in items if it["issues"]),
        }
    except ChecklistParseError:
        raise
    except reader.ReaderError as exc:
        raise ChecklistParseError(f"无法读取 Excel 文件：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        # openpyxl 迭代阶段（坏数字/布尔缓存等）同样必须收敛成 422，
        # 不能把 ValueError 泄露成 500。
        raise ChecklistParseError(
            f"无法读取 Excel 文件：{type(exc).__name__}") from exc
    finally:
        if wb is not None:
            with suppress(Exception):
                wb.close()
        with suppress(Exception):
            archive.close()


def store_preview(
    db: Session,
    parsed: dict,
    *,
    project_id: str,
    uploaded_by: str,
    idempotency_key: str,
) -> str:
    """落 pending 批次 + raw 行。幂等重放：同 (uploaded_by, key) 且同 hash 返回原批次。"""
    existing = db.scalar(
        select(MaintenanceAcceptanceChecklistBatch).where(
            MaintenanceAcceptanceChecklistBatch.uploaded_by == uploaded_by,
            MaintenanceAcceptanceChecklistBatch.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.file_hash != parsed["file_hash"]:
            raise ChecklistBatchError("同一幂等键已用于不同文件，请换一个上传重试")
        db.rollback()
        return existing.batch_id
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise ChecklistBatchError("维保项目不存在")
    batch = MaintenanceAcceptanceChecklistBatch(
        batch_id=str(uuid.uuid4()),
        project_id=project_id,
        file_hash=parsed["file_hash"],
        filename=parsed["filename"][:255],
        idempotency_key=idempotency_key,
        uploaded_by=uploaded_by,
        item_rows=parsed["item_rows"],
        issue_rows=parsed["issue_rows"],
        status="pending",
        report_json={
            "item_rows": parsed["item_rows"],
            "issue_rows": parsed["issue_rows"],
            "done_rows": sum(1 for it in parsed["items"] if it["done"] is True),
            "todo_rows": sum(1 for it in parsed["items"] if it["done"] is False),
        },
    )
    db.add(batch)
    db.flush()
    for it in parsed["items"]:
        db.add(MaintenanceAcceptanceChecklistItem(
            item_id=str(uuid.uuid4()),
            batch_id=batch.batch_id,
            row_no=it["row_no"],
            raw_json=it["raw"],
            requirement=it["requirement"],
            done=it["done"],
            issues=it["issues"],
        ))
    db.flush()
    return batch.batch_id


def _current_batch(db: Session, project_id: str
                   ) -> MaintenanceAcceptanceChecklistBatch | None:
    return db.scalar(
        select(MaintenanceAcceptanceChecklistBatch)
        .where(
            MaintenanceAcceptanceChecklistBatch.project_id == project_id,
            MaintenanceAcceptanceChecklistBatch.status == "applied",
        )
        .order_by(MaintenanceAcceptanceChecklistBatch.applied_at.desc())
        .limit(1)
    )


def apply_batch(db: Session, batch_id: str, *, operated_by: str) -> dict:
    """应用批次：整表替换当前清单。有 issue 行 → 整批拒绝（fail-closed）。"""
    batch = db.get(MaintenanceAcceptanceChecklistBatch, batch_id)
    if batch is None:
        raise ChecklistBatchError("清单批次不存在")
    if batch.uploaded_by != operated_by:
        raise ChecklistBatchError("只能应用本人上传的清单批次")
    if batch.status == "applied":
        db.rollback()
        return _batch_summary(db, batch)
    if batch.status == "failed":
        raise ChecklistBatchError("批次已失败，请重新上传")

    items = list(db.scalars(
        select(MaintenanceAcceptanceChecklistItem)
        .where(MaintenanceAcceptanceChecklistItem.batch_id == batch_id)
        .order_by(MaintenanceAcceptanceChecklistItem.row_no)))
    issues = [(it.row_no, msg) for it in items for msg in (it.issues or [])]
    if issues:
        batch.status = "failed"
        batch.report_json = {
            **(batch.report_json or {}),
            "failures": [f"第 {row} 行：{msg}" for row, msg in issues[:20]],
        }
        db.flush()
        raise ChecklistBatchError(
            f"清单有 {len(issues)} 个问题行，整批未生效："
            + "；".join(f"第 {row} 行 {msg}" for row, msg in issues[:5]))

    previous = _current_batch(db, batch.project_id)
    batch.status = "applied"
    batch.applied_by = operated_by
    batch.applied_at = datetime.now(UTC)
    batch.replaced_batch_id = previous.batch_id if previous else None
    db.flush()
    return {
        "batch_id": batch.batch_id,
        "item_rows": batch.item_rows,
        "done_rows": sum(1 for it in items if it.done is True),
        "todo_rows": sum(1 for it in items if it.done is False),
        "replaced_batch_id": batch.replaced_batch_id,
    }


def _batch_summary(db: Session, batch: MaintenanceAcceptanceChecklistBatch) -> dict:
    items = list(db.scalars(
        select(MaintenanceAcceptanceChecklistItem)
        .where(MaintenanceAcceptanceChecklistItem.batch_id == batch.batch_id)
        .order_by(MaintenanceAcceptanceChecklistItem.row_no)))
    return {
        "batch_id": batch.batch_id,
        "item_rows": batch.item_rows,
        "done_rows": sum(1 for it in items if it.done is True),
        "todo_rows": sum(1 for it in items if it.done is False),
        "replaced_batch_id": batch.replaced_batch_id,
    }


def project_checklist(db: Session, project_id: str) -> dict:
    """当前生效清单 + 历史批次列表（面板「验收」页签数据）。"""
    current = _current_batch(db, project_id)
    history = list(db.scalars(
        select(MaintenanceAcceptanceChecklistBatch)
        .where(
            MaintenanceAcceptanceChecklistBatch.project_id == project_id,
            MaintenanceAcceptanceChecklistBatch.status == "applied",
        )
        .order_by(MaintenanceAcceptanceChecklistBatch.applied_at.desc())
        .limit(20)))
    current_payload = None
    if current is not None:
        items = list(db.scalars(
            select(MaintenanceAcceptanceChecklistItem)
            .where(MaintenanceAcceptanceChecklistItem.batch_id == current.batch_id)
            .order_by(MaintenanceAcceptanceChecklistItem.row_no)))
        current_payload = {
            "batch_id": current.batch_id,
            "filename": current.filename,
            "uploaded_by": current.uploaded_by,
            "applied_by": current.applied_by,
            "applied_at": current.applied_at.isoformat() if current.applied_at else None,
            "item_rows": current.item_rows,
            "done_rows": sum(1 for it in items if it.done is True),
            "todo_rows": sum(1 for it in items if it.done is False),
            "items": [
                {
                    "item_id": it.item_id,
                    "row_no": it.row_no,
                    "requirement": it.requirement,
                    "done": it.done,
                }
                for it in items
            ],
        }
    return {
        "current": current_payload,
        "history": [
            {
                "batch_id": b.batch_id,
                "filename": b.filename,
                "applied_by": b.applied_by,
                "applied_at": b.applied_at.isoformat() if b.applied_at else None,
                "item_rows": b.item_rows,
            }
            for b in history
        ],
    }


def build_template() -> bytes:
    """标准模板：验收需求 / 是否完成 两列 + 两行灰底示例行。

    示例行以「（示例）」开头，解析器自动跳过（防呆：模板原样上传不会产生垃圾行）。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_CANDIDATES[0]
    ws.append([COL_REQUIREMENT, COL_DONE])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="FFF3CD")
    ws.append([f"{EXAMPLE_PREFIX}设备巡检报告已归档（本行为示例，导入时自动忽略）", "是"])
    ws.append([f"{EXAMPLE_PREFIX}备件损耗清单双方签字确认（本行为示例）", "否"])
    for cell in ws[2] + ws[3]:
        cell.font = Font(color="999999", italic=True)
    ws.column_dimensions["A"].width = 64
    ws.column_dimensions["B"].width = 14
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
