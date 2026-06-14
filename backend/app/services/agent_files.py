"""智能体文件原语层。

设计思想（借鉴 Claude Code 等智能体框架）：客户文件格式千变万化——询价单、
整机配置（Word/Excel/PDF/txt/图片）——**不写死解析规则**，给模型"眼睛"
（inspect/read_document：看结构与原样内容，自己判断表头/型号列/拆件）和
"手"（write_excel 回填模板 / write_report 生成美化报表）。

安全边界：不提供任意代码执行（多用户后端 exec = RCE）；上传文件只读，写操作
一律产出新 file_id（绝不改写原上传件）；file_id 白名单正则防路径穿越；
扩展名白名单防可执行文件。
"""
import json
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter

from app.config import get_settings

_FILE_ID = re.compile(r"^[a-f0-9]{12}$")
_MAX_UPLOAD_MB = 20
_PREVIEW_ROWS = 8
_PREVIEW_COLS = 12
_MAX_READ_ROWS = 200
_MAX_WRITE_CELLS = 3000
_CELL_TRUNC = 60
_DOC_CHAR_CAP = 60_000          # read_document 文本上限，防超长文件撑爆上下文

# 上传扩展名白名单（小写，无点）
_TEXT_EXT = {"txt", "csv", "md"}
_IMG_EXT = {"jpg", "jpeg", "png", "webp", "bmp"}
_ALLOWED_EXT = {"xlsx", "docx", "pdf"} | _TEXT_EXT | _IMG_EXT


class FileError(Exception):
    """文件层业务错误（消息可直接回给模型/用户）。"""


def _dir() -> Path:
    d = Path(get_settings().raw_file_dir).parent / "agent_files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(file_id: str) -> Path:
    return _dir() / f"{file_id}.meta.json"


def _ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _data_path(file_id: str, ext: str) -> Path:
    return _dir() / f"{file_id}.{ext}"


def _check_id(file_id: str) -> str:
    fid = str(file_id or "").strip().lower()
    if not _FILE_ID.match(fid):
        raise FileError(f"非法 file_id: {file_id!r}")
    return fid


def _load_meta(file_id: str) -> dict:
    p = _meta_path(file_id)
    if not p.exists():
        raise FileError(f"文件不存在或已清理: {file_id}")
    meta = json.loads(p.read_text(encoding="utf-8"))
    if not _data_path(file_id, meta.get("ext", "")).exists():
        raise FileError(f"文件不存在或已清理: {file_id}")
    return meta


def _save_meta(file_id: str, meta: dict) -> None:
    _meta_path(file_id).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _cell_str(v) -> str:
    if v is None:
        return ""
    s = v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)
    return s[:_CELL_TRUNC]


def save_upload(content: bytes, filename: str, operated_by: str | None) -> dict:
    """保存上传文件（多格式），返回 file_id + 类型/概览（供注入对话上下文）。"""
    ext = _ext_of(filename)
    if ext == "xls":
        raise FileError("不支持旧版 .xls，请用 Excel 另存为 .xlsx")
    if ext not in _ALLOWED_EXT:
        raise FileError(f"不支持的文件类型 .{ext}（支持：Excel/Word/PDF/txt/图片）")
    if len(content) > _MAX_UPLOAD_MB * 1024 * 1024:
        raise FileError(f"文件超过 {_MAX_UPLOAD_MB}MB 上限")

    file_id = uuid.uuid4().hex[:12]
    meta = {"filename": filename, "ext": ext, "kind": "upload",
            "operated_by": operated_by,
            "created_at": datetime.now(timezone.utc).isoformat()}

    if ext == "xlsx":
        # xlsx 校验可解析并带回 sheet 概览
        try:
            wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
            meta["sheets"] = [{"name": ws.title, "n_rows": ws.max_row or 0,
                               "n_cols": ws.max_column or 0} for ws in wb.worksheets]
            wb.close()
        except Exception as exc:  # noqa: BLE001
            raise FileError(f"无法解析 xlsx（可能损坏，请用 Excel 重新另存）: {type(exc).__name__}") from exc

    _data_path(file_id, ext).write_bytes(content)
    _save_meta(file_id, meta)
    out = {"file_id": file_id, "filename": filename, "ext": ext,
           "file_kind": ("表格" if ext == "xlsx" else "Word" if ext == "docx"
                         else "PDF" if ext == "pdf" else "图片" if ext in _IMG_EXT else "文本")}
    if "sheets" in meta:
        out["sheets"] = meta["sheets"]
    return out


# ============================================================
# 读取（xlsx 结构化 + 通用 read_document）
# ============================================================

def _require_xlsx(fid: str, meta: dict):
    if meta.get("ext") != "xlsx":
        raise FileError(f"该文件是 .{meta.get('ext')}，结构化读取仅限 Excel；请用 read_document 读取内容")


def inspect_file(file_id: str) -> dict:
    """看 Excel 结构：sheet 列表 + 每 sheet 前几行原样预览。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    _require_xlsx(fid, meta)
    wb = load_workbook(_data_path(fid, "xlsx"), read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets[:5]:
        preview = [[_cell_str(c.value) for c in row]
                   for row in ws.iter_rows(min_row=1, max_row=_PREVIEW_ROWS,
                                           max_col=min(ws.max_column or 1, _PREVIEW_COLS))]
        sheets.append({"name": ws.title, "n_rows": ws.max_row or 0,
                       "n_cols": ws.max_column or 0, "preview_rows_1_to_n": preview})
    wb.close()
    return {"file_id": fid, "filename": meta.get("filename"), "sheets": sheets,
            "note": "preview 为前几行原样数据(1-based 行号)；更多行用 read_file_rows"}


def read_rows(file_id: str, sheet: str | None, start_row: int, max_rows: int) -> dict:
    """分页读取 Excel 行（1-based）。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    _require_xlsx(fid, meta)
    wb = load_workbook(_data_path(fid, "xlsx"), read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
    except KeyError:
        names = [w.title for w in wb.worksheets]
        wb.close()
        raise FileError(f"sheet 不存在: {sheet!r}，可选: {names}")
    start = max(int(start_row or 1), 1)
    n = min(int(max_rows or 50), _MAX_READ_ROWS)
    rows = [[_cell_str(c.value) for c in row]
            for row in ws.iter_rows(min_row=start, max_row=start + n - 1,
                                    max_col=min(ws.max_column or 1, 30))]
    total = ws.max_row or 0
    wb.close()
    return {"file_id": fid, "sheet": ws.title, "start_row": start,
            "rows": rows, "total_rows": total}


def _read_docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    parts: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]
    for ti, table in enumerate(doc.tables):
        parts.append(f"[表格{ti + 1}]")
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_pdf(path: Path) -> tuple[str, bool]:
    """返回 (文本, 是否疑似扫描件)。文字层为空/极少 → 扫描件，转视觉。"""
    import pdfplumber
    parts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for pi, page in enumerate(pdf.pages[:30]):
            txt = page.extract_text() or ""
            parts.append(f"[第{pi + 1}页]\n{txt}" if txt.strip() else f"[第{pi + 1}页](无文字层)")
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [(c or "").strip() for c in row]
                    if any(cells):
                        parts.append(" | ".join(cells))
    text = "\n".join(parts)
    scanned = len(re.sub(r"\s", "", text.replace("无文字层", ""))) < 20
    return text, scanned


def _read_image_or_scanned(path: Path, hint: str) -> str:
    """图片/扫描件 → Qwen-VL 识别（无 key 优雅降级）。"""
    from app.agent import provider
    try:
        return provider.vision_extract([path], hint)
    except provider.VisionNotConfigured:
        return ("【未配置视觉模型】该文件是图片/扫描件，需配置 VISION_API_KEY（通义 Qwen-VL）"
                "后才能识别。文字版 Word/Excel/PDF/txt 不受影响。")


def read_document(file_id: str) -> dict:
    """通用读取：把任意支持格式抽成文本喂给模型（拆件/解析由模型完成）。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    ext = meta.get("ext", "")
    vision_used = False
    if ext in _TEXT_EXT:
        text = _data_path(fid, ext).read_bytes().decode("utf-8", errors="replace")
    elif ext == "xlsx":
        wb = load_workbook(_data_path(fid, "xlsx"), read_only=True, data_only=True)
        chunks = []
        for ws in wb.worksheets:
            chunks.append(f"[工作表 {ws.title}]")
            for row in ws.iter_rows(max_row=min(ws.max_row or 0, 300), max_col=min(ws.max_column or 1, 30)):
                cells = [_cell_str(c.value) for c in row]
                if any(cells):
                    chunks.append(" | ".join(cells))
        wb.close()
        text = "\n".join(chunks)
    elif ext == "docx":
        text = _read_docx(_data_path(fid, "docx"))
    elif ext == "pdf":
        text, scanned = _read_pdf(_data_path(fid, "pdf"))
        if scanned:
            vision_used = True
            text = _read_image_or_scanned(_data_path(fid, "pdf"),
                                          "这是一份扫描件，请逐字识别其中的全部文本、表格、型号与参数。")
    elif ext in _IMG_EXT:
        vision_used = True
        text = _read_image_or_scanned(
            _data_path(fid, ext),
            "请识别图片中的全部文字、表格、设备型号、品牌与参数配置，按原结构输出。")
    else:
        raise FileError(f"不支持读取 .{ext}")

    truncated = len(text) > _DOC_CHAR_CAP
    return {"file_id": fid, "filename": meta.get("filename"), "ext": ext,
            "vision_used": vision_used, "truncated": truncated,
            "content": text[:_DOC_CHAR_CAP]}


# ============================================================
# 写（模板回填 write_excel / 美化报表 write_report）
# ============================================================

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


def _new_file(meta_extra: dict, operated_by: str | None, name: str) -> tuple[str, dict]:
    file_id = uuid.uuid4().hex[:12]
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    meta = {"filename": name, "ext": "xlsx", "kind": "generated", "operated_by": operated_by,
            "created_at": datetime.now(timezone.utc).isoformat(), **meta_extra}
    return file_id, meta


def write_excel(base_file_id: str | None, sheet: str | None,
                cells: list[dict], output_name: str | None,
                operated_by: str | None) -> dict:
    """按模型指令写单元格，产出新文件（不动原件）。用于**回填客户模板**（保留原格式）。"""
    if not cells:
        raise FileError("cells 不能为空")
    if len(cells) > _MAX_WRITE_CELLS:
        raise FileError(f"单次最多写 {_MAX_WRITE_CELLS} 个单元格")

    base_name = ""
    if base_file_id:
        base = _check_id(base_file_id)
        bmeta = _load_meta(base)
        _require_xlsx(base, bmeta)
        wb = load_workbook(_data_path(base, "xlsx"))  # 保留原格式/公式
        base_name = bmeta.get("filename", "")
    else:
        wb = Workbook()

    ws = (wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)) if sheet else wb.worksheets[0]

    written = 0
    for c in cells:
        try:
            row, col = int(c["row"]), _col_index(c["col"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FileError(f"cells 项格式错: {c!r}（需 row/col/value）") from exc
        if row < 1 or row > 1_048_576:
            raise FileError(f"行号超界: {row}")
        ws.cell(row=row, column=col, value=c.get("value"))
        written += 1

    name = output_name or (f"回填_{base_name}" if base_name else "结果.xlsx")
    file_id, meta = _new_file({"base_file_id": base_file_id}, operated_by, name)
    wb.save(_data_path(file_id, "xlsx"))
    wb.close()
    _save_meta(file_id, meta)
    return {"file_id": file_id, "filename": meta["filename"], "cells_written": written,
            "download_url": f"/api/agent/files/{file_id}"}


# 美化报表样式常量
_HEADER_FILL = PatternFill("solid", fgColor="4F46E5")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_TITLE_FONT = Font(bold=True, size=14, color="111827")
_ZEBRA_FILL = PatternFill("solid", fgColor="F5F6FF")
_WARN_FILL = PatternFill("solid", fgColor="FFF3E0")    # 需确认=橙
_BAD_FILL = PatternFill("solid", fgColor="FDECEA")     # 未找到=红
_THIN = Side(style="thin", color="E5E7EB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_WARN_KW = ("需确认", "待确认", "确认")
_BAD_KW = ("未找到", "无库存", "不存在", "未匹配")


def write_report(title: str | None, headers: list[str], rows: list[list],
                 output_name: str | None, operated_by: str | None,
                 money_cols: list[int] | None = None) -> dict:
    """生成**美化报表**（BOM/报价单等）：表头配色、边框、自适应列宽、金额格式、
    冻结表头、斑马纹、状态行条件配色。headers=列名；rows=与列对齐的二维数组；
    money_cols=金额列的 0-based 下标（数字格式+右对齐）。"""
    if not headers:
        raise FileError("headers 不能为空")
    if len(rows) > 5000:
        raise FileError("报表最多 5000 行")
    money = set(money_cols or [])
    ncol = len(headers)

    wb = Workbook()
    ws = wb.worksheets[0]
    ws.title = "报表"
    r0 = 1
    if title:
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
        tc = ws.cell(row=1, column=1, value=title)
        tc.font = _TITLE_FONT
        tc.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[1].height = 26
        r0 = 2

    # 表头
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=r0, column=j, value=str(h))
        c.fill, c.font, c.border = _HEADER_FILL, _HEADER_FONT, _BORDER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[r0].height = 22

    widths = [len(str(h)) for h in headers]
    for i, row in enumerate(rows):
        rr = r0 + 1 + i
        rowtext = " ".join(str(v) for v in row if v is not None)
        hit_bad = any(k in rowtext for k in _BAD_KW)
        hit_warn = not hit_bad and any(k in rowtext for k in _WARN_KW)
        base_fill = _BAD_FILL if hit_bad else _WARN_FILL if hit_warn else (_ZEBRA_FILL if i % 2 else None)
        for j in range(ncol):
            val = row[j] if j < len(row) else None
            c = ws.cell(row=rr, column=j + 1, value=val)
            c.border = _BORDER
            if base_fill:
                c.fill = base_fill
            if j in money and isinstance(val, (int, float)):
                c.number_format = '#,##0.00'
                c.alignment = Alignment(horizontal="right")
            else:
                c.alignment = Alignment(vertical="center", wrap_text=isinstance(val, str) and len(str(val)) > 20)
            widths[j] = max(widths[j], min(len(_cell_str(val)), 50))

    for j, w in enumerate(widths, start=1):
        # 中文偏宽：粗略 ×1.6 + 余量；上限防超宽
        ws.column_dimensions[get_column_letter(j)].width = min(max(w * 1.6 + 2, 8), 60)
    ws.freeze_panes = ws.cell(row=r0 + 1, column=1)

    name = output_name or (title or "报表")
    file_id, meta = _new_file({"report": True}, operated_by, name)
    wb.save(_data_path(file_id, "xlsx"))
    wb.close()
    _save_meta(file_id, meta)
    return {"file_id": file_id, "filename": meta["filename"], "rows_written": len(rows),
            "download_url": f"/api/agent/files/{file_id}"}


def owner_of(file_id: str) -> str | None:
    """文件创建者（save_upload/_new_file 时记的 operated_by）；文件不存在抛 FileError。归属校验用。"""
    return _load_meta(_check_id(file_id)).get("operated_by")


def get_download(file_id: str) -> tuple[Path, str]:
    """下载定位：返回 (路径, 文件名)。归属校验在 API 层（见 api/agent.download）。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    return _data_path(fid, meta.get("ext", "xlsx")), meta.get("filename", f"{fid}.xlsx")
