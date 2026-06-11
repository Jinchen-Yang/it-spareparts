"""智能体文件原语层。

设计思想（借鉴 Claude Code 等智能体框架）：客户询价单格式千变万化，
**不写死解析规则**——给模型"眼睛"（inspect/read：看 sheet 结构与原样数据，
自己判断表头在哪行、哪列是 PN）和"手"（write：模型决定写什么列、写到哪、
回填原表还是新建），格式适配由模型决策完成。

安全边界：不提供任意代码执行（多用户后端 exec = RCE）；上传文件只读，
写操作一律产出新 file_id（绝不改写原上传件）；file_id 白名单正则防路径穿越。
"""
import json
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

from app.config import get_settings

_FILE_ID = re.compile(r"^[a-f0-9]{12}$")
_MAX_UPLOAD_MB = 10
_PREVIEW_ROWS = 8
_PREVIEW_COLS = 12
_MAX_READ_ROWS = 200
_MAX_WRITE_CELLS = 3000
_CELL_TRUNC = 60


class FileError(Exception):
    """文件层业务错误（消息可直接回给模型/用户）。"""


def _dir() -> Path:
    d = Path(get_settings().raw_file_dir).parent / "agent_files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(file_id: str) -> Path:
    return _dir() / f"{file_id}.meta.json"


def _xlsx_path(file_id: str) -> Path:
    return _dir() / f"{file_id}.xlsx"


def _check_id(file_id: str) -> str:
    fid = str(file_id or "").strip().lower()
    if not _FILE_ID.match(fid):
        raise FileError(f"非法 file_id: {file_id!r}")
    return fid


def _load_meta(file_id: str) -> dict:
    p = _meta_path(file_id)
    if not p.exists() or not _xlsx_path(file_id).exists():
        raise FileError(f"文件不存在或已清理: {file_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_meta(file_id: str, meta: dict) -> None:
    _meta_path(file_id).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _cell_str(v) -> str:
    if v is None:
        return ""
    s = v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)
    return s[:_CELL_TRUNC]


def save_upload(content: bytes, filename: str, operated_by: str | None) -> dict:
    """保存上传的 xlsx，返回 file_id + 结构概览（供注入对话上下文）。"""
    if not filename.lower().endswith(".xlsx"):
        raise FileError("仅支持 .xlsx 文件（旧版 .xls 请用 Excel 另存为 .xlsx）")
    if len(content) > _MAX_UPLOAD_MB * 1024 * 1024:
        raise FileError(f"文件超过 {_MAX_UPLOAD_MB}MB 上限")
    try:
        wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
        sheets = [{"name": ws.title, "n_rows": ws.max_row or 0, "n_cols": ws.max_column or 0}
                  for ws in wb.worksheets]
        wb.close()
    except Exception as exc:  # noqa: BLE001 —— 坏文件给可读提示
        raise FileError(f"无法解析 xlsx（文件可能损坏，请用 Excel 重新另存）: {type(exc).__name__}") from exc

    file_id = uuid.uuid4().hex[:12]
    _xlsx_path(file_id).write_bytes(content)
    _save_meta(file_id, {
        "filename": filename, "kind": "upload", "operated_by": operated_by,
        "created_at": datetime.now(timezone.utc).isoformat(), "sheets": sheets,
    })
    return {"file_id": file_id, "filename": filename, "sheets": sheets}


def inspect_file(file_id: str) -> dict:
    """看结构：sheet 列表 + 每个 sheet 前几行原样预览。模型据此自行判断表头/数据列。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    wb = load_workbook(_xlsx_path(fid), read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets[:5]:
        preview = []
        for row in ws.iter_rows(min_row=1, max_row=_PREVIEW_ROWS,
                                max_col=min(ws.max_column or 1, _PREVIEW_COLS)):
            preview.append([_cell_str(c.value) for c in row])
        sheets.append({"name": ws.title, "n_rows": ws.max_row or 0,
                       "n_cols": ws.max_column or 0, "preview_rows_1_to_n": preview})
    wb.close()
    return {"file_id": fid, "filename": meta.get("filename"), "sheets": sheets,
            "note": "preview 为前几行原样数据(1-based 行号)；需要更多行用 read_file_rows"}


def read_rows(file_id: str, sheet: str | None, start_row: int, max_rows: int) -> dict:
    """分页读取行（1-based）。"""
    fid = _check_id(file_id)
    _load_meta(fid)
    wb = load_workbook(_xlsx_path(fid), read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
    except KeyError:
        names = [w.title for w in wb.worksheets]
        wb.close()
        raise FileError(f"sheet 不存在: {sheet!r}，可选: {names}")
    start = max(int(start_row or 1), 1)
    n = min(int(max_rows or 50), _MAX_READ_ROWS)
    rows = []
    for row in ws.iter_rows(min_row=start, max_row=start + n - 1,
                            max_col=min(ws.max_column or 1, 30)):
        rows.append([_cell_str(c.value) for c in row])
    total = ws.max_row or 0
    wb.close()
    return {"file_id": fid, "sheet": ws.title, "start_row": start,
            "rows": rows, "total_rows": total}


def _col_index(col) -> int:
    """列定位：支持 "A"/"G" 字母或 1-based 数字。"""
    if isinstance(col, int) or (isinstance(col, str) and col.isdigit()):
        idx = int(col)
        if idx < 1 or idx > 16384:
            raise FileError(f"列号超界: {col}")
        return idx
    try:
        return column_index_from_string(str(col).strip().upper())
    except Exception as exc:  # noqa: BLE001
        raise FileError(f"无法识别列: {col!r}（用字母如 'G' 或 1-based 数字）") from exc


def write_excel(base_file_id: str | None, sheet: str | None,
                cells: list[dict], output_name: str | None,
                operated_by: str | None) -> dict:
    """按模型指令写单元格，产出新文件（不动原件）。

    cells: [{"row": 3, "col": "G"|7, "value": ...}]。base_file_id 给了就在其副本上写
    （回填客户模板场景），否则新建空工作簿（自拟报价单场景）。
    """
    if not cells:
        raise FileError("cells 不能为空")
    if len(cells) > _MAX_WRITE_CELLS:
        raise FileError(f"单次最多写 {_MAX_WRITE_CELLS} 个单元格")

    if base_file_id:
        base = _check_id(base_file_id)
        _load_meta(base)
        wb = load_workbook(_xlsx_path(base))  # 保留原格式/公式
        base_name = _load_meta(base).get("filename", "")
    else:
        wb = Workbook()
        base_name = ""

    if sheet:
        ws = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)
    else:
        ws = wb.worksheets[0]

    written = 0
    for c in cells:
        try:
            row = int(c["row"])
            col = _col_index(c["col"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FileError(f"cells 项格式错: {c!r}（需 row/col/value）") from exc
        if row < 1 or row > 1_048_576:
            raise FileError(f"行号超界: {row}")
        ws.cell(row=row, column=col, value=c.get("value"))
        written += 1

    file_id = uuid.uuid4().hex[:12]
    wb.save(_xlsx_path(file_id))
    wb.close()
    name = output_name or (f"回填_{base_name}" if base_name else "报价单.xlsx")
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    _save_meta(file_id, {
        "filename": name, "kind": "generated", "operated_by": operated_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_file_id": base_file_id,
    })
    return {"file_id": file_id, "filename": name, "cells_written": written,
            "download_url": f"/api/agent/files/{file_id}",
            "max_col_letter_hint": get_column_letter(min(ws.max_column or 1, 16384))}


def get_download(file_id: str) -> tuple[Path, str]:
    """下载定位：返回 (路径, 文件名)。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    return _xlsx_path(fid), meta.get("filename", f"{fid}.xlsx")
