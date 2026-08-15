"""上传资源安全校验（导入共享，C1 审核修复）。

- 流式限额读取（先验 Content-Length + 分块上限）；
- XLSX ZIP 安全预检：成员数、解压总量、压缩比上限，防 ZIP bomb；
- 解析放入线程池，不阻塞 async event-loop。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

MAX_ZIP_MEMBERS = 300
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_COMPRESSION_RATIO = 200


class UploadSafetyError(RuntimeError):
    """上传内容安全校验失败。"""


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """分块读取并硬性限额；超限抛 UploadSafetyError。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadSafetyError("文件超过上传安全上限")
        chunks.append(chunk)
    return b"".join(chunks)


def validate_xlsx_zip(data: bytes, *, max_bytes: int) -> None:
    """ZIP 结构预检：成员数/解压总量/压缩比，防炸弹与外链引用。"""
    if len(data) > max_bytes:
        raise UploadSafetyError("文件超过上传安全上限")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadSafetyError("不是有效的 .xlsx（ZIP）文件") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise UploadSafetyError("ZIP 成员数超过安全上限")
        total_uncompressed = 0
        total_compressed = 0
        for info in infos:
            # 路径穿越/外链成员拒绝
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts:
                raise UploadSafetyError("ZIP 包含非法路径成员")
            total_uncompressed += info.file_size
            total_compressed += max(info.compress_size, 1)
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise UploadSafetyError("ZIP 解压总量超过安全上限")
        if total_compressed > 0 and total_uncompressed / total_compressed > MAX_COMPRESSION_RATIO:
            raise UploadSafetyError("ZIP 压缩比异常，疑似压缩炸弹")


async def parse_in_threadpool(fn, *args, **kwargs):
    """把同步 openpyxl 解析移出 async event-loop。"""
    return await run_in_threadpool(fn, *args, **kwargs)


# ---------------------------------------------------------------- 流式解析
STREAM_THRESHOLD_BYTES = 8 * 1024 * 1024  # 超过 8MB 的文件走流式 XML（图片剥离）
MAX_CELL_TEXT = 4096  # 超过此长度的单元格视为内嵌图片/附件，跳过


def stream_first_sheet_rows(data: bytes):
    """流式惰性生成第一个**可见** worksheet 的行元组，剥离超长（图片）单元格。

    氚云导出把附件图片以 base64 内嵌在单元格里（57MB 文件解压后 500MB+），
    openpyxl 普通模式会整包解析。round-4 Blocker 11 修复：
    - 按 workbook.xml 的 sheet r:id → workbook.xml.rels 解析第一个可见 sheet，
      不再按 worksheet XML 文件名排序瞎猜；
    - 解析 sharedStrings（t="s" 单元格还原为真实文本）；
    - 惰性 yield 行，不整表蓄积内存（调用方单遍消费）。
    """
    import io
    import re
    import zipfile
    import xml.etree.ElementTree as ET

    NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    # workbook.xml 内 sheet 的 r:id 在 officeDocument relationships 命名空间；
    # workbook.xml.rels 的 Relationship 元素在 package relationships 命名空间。
    NS_OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    NS_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

    archive = zipfile.ZipFile(io.BytesIO(data))
    try:
        # 1) 第一个**可见** sheet 的 target 路径（跳过 hidden/veryHidden）
        target: str | None = None
        first_rid: str | None = None
        try:
            with archive.open("xl/workbook.xml") as handle:
                workbook_el = ET.fromstring(handle.read())
            for sheet in workbook_el.iter(NS_MAIN + "sheet"):
                if sheet.get("state") in ("hidden", "veryHidden"):
                    continue
                first_rid = sheet.get(NS_OFFICE_REL + "id")
                if first_rid:
                    break
            if first_rid:
                with archive.open("xl/_rels/workbook.xml.rels") as handle:
                    rels_el = ET.fromstring(handle.read())
                for rel in rels_el.iter(NS_PACKAGE_REL + "Relationship"):
                    if rel.get("Id") == first_rid and rel.get("Target"):
                        raw_target = rel.get("Target")
                        # Target 可为相对（worksheets/sheet1.xml）或绝对（/xl/...）
                        target = (
                            raw_target.lstrip("/")
                            if raw_target.startswith("/")
                            else "xl/" + raw_target
                        )
                        break
        except (KeyError, ET.ParseError):
            target = None
        if target is None or target not in archive.namelist():
            targets = sorted(
                name
                for name in archive.namelist()
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            )
            if not targets:
                raise UploadSafetyError("xlsx 缺少 worksheet")
            target = targets[0]

        # 2) sharedStrings：t="s" 单元格引用其索引
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            with archive.open("xl/sharedStrings.xml") as handle:
                for _ev, el in ET.iterparse(handle, events=("end",)):
                    if el.tag != NS_MAIN + "si":
                        continue
                    shared.append(
                        "".join(t.text or "" for t in el.iter(NS_MAIN + "t"))
                    )
                    el.clear()

        # 3) 惰性生成行元组
        with archive.open(target) as handle:
            for _ev, el in ET.iterparse(handle, events=("end",)):
                if el.tag != NS_MAIN + "row":
                    continue
                cells: dict[int, str] = {}
                for cell in el.iter(NS_MAIN + "c"):
                    ref = cell.get("r", "")
                    text = cell.find(NS_MAIN + "is/" + NS_MAIN + "t")
                    value = cell.find(NS_MAIN + "v")
                    if (
                        text is not None
                        and text.text
                        and len(text.text) <= MAX_CELL_TEXT
                    ):
                        cells[_column_index(ref)] = text.text.strip()
                    elif value is not None and value.text:
                        raw = value.text
                        if cell.get("t") == "s":
                            try:
                                raw = shared[int(raw)]
                            except (ValueError, IndexError):
                                continue  # 共享字符串索引损坏 → 跳过该单元格
                        if len(raw) <= MAX_CELL_TEXT:
                            cells[_column_index(ref)] = raw.strip()
                if cells:
                    max_col = max(cells)
                    yield tuple(cells.get(i) for i in range(1, max_col + 1))
                el.clear()
    finally:
        archive.close()


def _column_index(ref: str) -> int:
    letters = ""
    for ch in ref:
        if ch.isalpha():
            letters += ch
        else:
            break
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index


def apply_column_aliases(headers: list, column_aliases: dict | None) -> list:
    """应用列别名映射；两个源列映射同一目标列时 fail-closed（round-5 Blocker 10）。"""
    if not column_aliases:
        return headers
    mapped = [column_aliases.get(h, h) for h in headers]
    seen: dict[str, str] = {}
    for source, target in zip(headers, mapped):
        if target in seen and seen[target] != source:
            raise UploadSafetyError(
                f"多个源列映射同一目标列：{target}（{seen[target]} / {source}）"
            )
        seen.setdefault(target, source)
    return mapped
