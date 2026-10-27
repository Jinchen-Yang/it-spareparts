"""验证签名与行身份后解析单元格；任何业务写入留给两步提交层。"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import asdict, dataclass, field
from io import BytesIO

from openpyxl import load_workbook
from openpyxl.cell.read_only import EMPTY_CELL
from openpyxl.utils.exceptions import InvalidFileException

from app.services.excel_text_safety import has_illegal_excel_upload_text
from app.services.workbook.protocol import (
    MAX_FILE_BYTES, MAX_SHEET_COLUMNS, MAX_SHEET_ROWS, MAX_UNCOMPRESSED_BYTES,
    MAX_ZIP_ENTRIES, TOKEN_HEADER, read_metadata, row_token,
)
from app.services.workbook.schema import WorkbookError, column_allowed, json_value
from app.services.workbook.values import clean_text, normalize


@dataclass
class CellResult:
    value: object
    raw: object
    error: str | None = None
    warning: str | None = None
    header: str = ""


@dataclass
class ParsedRow:
    sheet: str
    row_number: int
    kind: str
    entity_id: object = None
    base_version: int | None = None
    token: str | None = None
    cells: dict[str, CellResult] = field(default_factory=dict)
    custom_fields: dict = field(default_factory=dict)
    editable_keys: list[str] = field(default_factory=list)
    baseline: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    schema_name: str
    schema_version: str
    export_id: str
    file_sha256: str
    rows: list[ParsedRow]
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return json_value(asdict(self))


def _cell(ws, row, column):
    # 稀疏表不能按矩形补齐数百万个空单元格；与现有往返服务一样只读已存在的格子。
    return ws._cells.get((row, column), EMPTY_CELL)


def _check_archive(file_bytes):
    if len(file_bytes) > MAX_FILE_BYTES:
        raise WorkbookError("文件不能超过 10 MB")
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES or sum(i.file_size for i in entries) > MAX_UNCOMPRESSED_BYTES:
                raise WorkbookError("工作簿解压大小或 ZIP 条目数超出安全上限")
            if len({i.filename for i in entries}) != len(entries) or any(i.flag_bits & 1 for i in entries):
                raise WorkbookError("不支持重复 ZIP 条目或加密工作簿")
            if "[Content_Types].xml" not in archive.namelist():
                raise WorkbookError("请上传 XLSX 工作簿")
            if any("externallinks/" in i.filename.lower() for i in entries):
                raise WorkbookError("工作簿含外部链接，请移除外部链接后重试")
            if any(i.filename.lower().endswith("vbaproject.bin") for i in entries):
                raise WorkbookError("不支持含宏的工作簿，请使用 XLSX")
            # 限制实际读取量，并在 XML 解析之前拒绝实体声明和非法控制字符。
            total = 0
            for entry in entries:
                with archive.open(entry) as stream:
                    content = stream.read(MAX_UNCOMPRESSED_BYTES + 1 - total)
                total += len(content)
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise WorkbookError("工作簿解压大小超出安全上限")
                if entry.filename.lower().endswith((".xml", ".rels")):
                    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                        raise WorkbookError("工作簿 XML 含不支持的实体声明")
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError):
        raise WorkbookError("请上传有效的 XLSX 工作簿") from None


def parse_workbook(schema, file_bytes: bytes, *, ctx) -> ParseResult:
    _check_archive(file_bytes)
    try:
        formulas = load_workbook(BytesIO(file_bytes), data_only=False, keep_links=False)
        cached = load_workbook(BytesIO(file_bytes), data_only=True, keep_links=False)
    except (ValueError, KeyError, OSError, InvalidFileException, zipfile.BadZipFile, SyntaxError):
        raise WorkbookError("工作簿内容无效，请使用系统导出的模板") from None
    metadata, manifest = read_metadata(formulas)
    if metadata["schema_name"] != schema.name or metadata["schema_version"] != str(schema.version):
        raise WorkbookError("模板名称或版本不匹配，请重新下载当前模板")
    result = ParseResult(schema.name, str(schema.version), metadata["export_id"], hashlib.sha256(file_bytes).hexdigest(), [], metadata=metadata)

    def message(sheet, row_number, column, text, *, warning=False):
        item = {"sheet": sheet, "row_number": row_number, "column": column, "message": text}
        issues = result.warnings if warning else result.errors
        if item not in issues:
            issues.append(item)

    sheet_keys = {sheet.title: sheet.key for sheet in schema.sheets}
    for ws in formulas:
        if ws.max_row > MAX_SHEET_ROWS or ws.max_column > MAX_SHEET_COLUMNS:
            raise WorkbookError(f"{ws.title} 行数或列数超出安全上限")
        if ws.merged_cells.ranges:
            raise WorkbookError(f"{ws.title} 含合并单元格：{', '.join(map(str, ws.merged_cells.ranges))}，请先取消合并")
        for cell in ws._cells.values():
            if has_illegal_excel_upload_text(cell.value):
                raise WorkbookError(f"{ws.title} {cell.coordinate} 含 Excel 不支持的控制字符")
            if cell.data_type == "f" and (re.search(r"\[[^\]]+\]", str(cell.value)) or re.search(r"(?:https?://|file:|\\\\)", str(cell.value), re.I)):
                raise WorkbookError(f"{ws.title} {cell.coordinate} 公式含外部链接")
            if cell.data_type == "f":
                value = _cell(cached[ws.title], cell.row, cell.column)
                if value.value is None or value.data_type == "e":
                    message(sheet_keys.get(ws.title, ws.title), cell.row,
                            str(_cell(ws, 1, cell.column).value or cell.column_letter),
                            "公式没有缓存值，请在 Excel/WPS/LibreOffice 中重新计算并保存"
                            if value.value is None else "公式缓存为错误值，请修正公式后保存")
    try:
        signed_sheets = manifest["sheets"]
        if set(signed_sheets) != {sheet.key for sheet in schema.sheets}:
            raise WorkbookError("模板工作表清单不匹配，请使用系统导出的模板")
    except (KeyError, TypeError):
        raise WorkbookError("请使用系统导出的模板（行清单缺失）") from None
    seen_tokens = set()
    for sheet in schema.sheets:
        if sheet.title not in formulas:
            raise WorkbookError(f"缺少工作表 {sheet.title}，请使用完整模板")
        ws = formulas[sheet.title]
        data_ws = cached[sheet.title]
        signed = signed_sheets[sheet.key]
        exported_columns = set(signed["columns"])
        columns = {c.key: c for c in sheet.columns}
        headers = {c.header: c for c in sheet.columns}
        indices = {}
        custom_indices = {}
        token_index = None
        seen_headers = set()
        for index, cell in enumerate(ws[1], 1):
            header = clean_text(str(cell.value)) if cell.value is not None else ""
            if not header:
                continue
            if header in seen_headers:
                raise WorkbookError(f"{sheet.title} 表头重复：{header}")
            seen_headers.add(header)
            if header == TOKEN_HEADER:
                token_index = index
            elif header in headers:
                column = headers[header]
                if not column_allowed(column, ctx) or column.key not in exported_columns:
                    message(sheet.key, 1, header, "该列未获授权或不在导出范围内，已忽略", warning=True)
                else:
                    indices[column.key] = index
            elif header.startswith("+") and sheet.custom_columns:
                name = clean_text(header[1:])
                if not name or name in custom_indices:
                    raise WorkbookError(f"{sheet.title} 自定义字段名称为空或重复")
                custom_indices[name] = index
            else:
                message(sheet.key, 1, header, "未知列已忽略", warning=True)
        if token_index is None:
            raise WorkbookError(f"{sheet.title} 缺少行令牌列，请使用系统导出的模板")
        for column in sheet.columns:
            if column.mandatory and column_allowed(column, ctx) and column.key not in indices:
                raise WorkbookError(f"{sheet.title} 缺少身份编码列 {column.header}")
        original = {r["token"]: r for r in signed["rows"]}
        for record in original.values():
            if record["token"] != row_token(schema, result.export_id, sheet.key, record["entity_id"], record["base_version"]):
                raise WorkbookError("行令牌签名无效，请使用系统导出的模板")
        populated_rows = sorted({row for (row, _), cell in ws._cells.items() if row > 1 and cell.value is not None})
        for row_number in populated_rows:
            token = _cell(ws, row_number, token_index).value
            if token is not None and token != "":
                if not isinstance(token, str) or token not in original:
                    raise WorkbookError(f"{sheet.title} 第 {row_number} 行令牌伪造或来自其他导出")
                if token in seen_tokens:
                    raise WorkbookError(f"{sheet.title} 第 {row_number} 行令牌重复")
                seen_tokens.add(token)
                base = original[token]
                kind = "exported"
            else:
                base = {"values": {}, "custom_fields": {}}
                def nonempty(value):
                    return (clean_text(value) if isinstance(value, str) else value) not in (None, "")
                has_editable = any(nonempty(_cell(ws, row_number, idx).value) for key, idx in indices.items() if columns[key].editable)
                has_custom = any(nonempty(_cell(ws, row_number, idx).value) for idx in custom_indices.values())
                if not has_editable and not has_custom:
                    continue
                kind = "added"
                if not sheet.allow_insert:
                    message(sheet.key, row_number, "", "该工作表不允许新增行")
            row = ParsedRow(sheet.key, row_number, kind, base.get("entity_id"), base.get("base_version"), token or None)
            row.baseline = {key: value for key, value in base["values"].items() if key in columns and column_allowed(columns[key], ctx)}
            for key, index in indices.items():
                column = columns[key]
                source = _cell(ws, row_number, index)
                raw = _cell(data_ws, row_number, index).value
                cell = CellResult(None, raw, header=column.header)
                if source.data_type == "f" and raw is None:
                    cell.error = "公式没有缓存值，请在 Excel/WPS/LibreOffice 中重新计算并保存"
                else:
                    try:
                        cell.value = normalize(column, raw, date_system=metadata["date_system"])
                    except ValueError as exc:
                        cell.error = str(exc)
                if not column.editable and kind == "exported":
                    baseline = row.baseline.get(key)
                    if json_value(cell.value) != baseline or cell.error:
                        cell.warning = "锁定或参考列已改动，已忽略并保留导出值"
                    # 编码数字化与无缓存公式必须报错，其余锁定字段只给警告。
                    if cell.error and not (column.type == "code" or source.data_type == "f"):
                        cell.error = None
                    try:
                        cell.value = normalize(column, baseline, date_system=metadata["date_system"])
                    except ValueError:
                        cell.value = baseline
                if cell.error:
                    message(sheet.key, row_number, column.header, cell.error)
                if cell.warning:
                    message(sheet.key, row_number, column.header, cell.warning, warning=True)
                row.cells[key] = cell
                if column.editable:
                    row.editable_keys.append(key)
            if kind == "added":
                for column in sheet.columns:
                    if column.edit == "required" and column_allowed(column, ctx) and column.key not in indices:
                        message(sheet.key, row_number, column.header, "新增行缺少必填列，请重新导出该列")
            for name, index in custom_indices.items():
                source = _cell(ws, row_number, index)
                raw = _cell(data_ws, row_number, index).value
                if source.data_type == "f" and raw is None:
                    message(sheet.key, row_number, "+" + name, "公式没有缓存值，请重新计算并保存")
                row.custom_fields[name] = clean_text(str(raw)) or None if raw is not None else None
            result.rows.append(row)
        for offset, (token, base) in enumerate(original.items(), 2):
            if token in seen_tokens:
                continue
            baseline = {key: value for key, value in base["values"].items() if key in columns and column_allowed(columns[key], ctx)}
            row = ParsedRow(sheet.key, offset, "deleted", base["entity_id"], base["base_version"], token, baseline=baseline)
            row.cells = {key: CellResult(value, value, header=columns[key].header) for key, value in baseline.items()}
            row.custom_fields = base.get("custom_fields", {})
            result.rows.append(row)
            if not sheet.allow_delete:
                message(sheet.key, offset, "", "该工作表不允许删除导出行，请恢复缺失行")
    return result
