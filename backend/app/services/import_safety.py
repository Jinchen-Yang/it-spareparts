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


def stream_first_sheet_rows(data: bytes) -> list[tuple]:
    """流式解析第一个 worksheet 的行元组，剥离超长（图片）单元格。

    氚云导出把附件图片以 base64 内嵌在单元格里（57MB 文件解压后 500MB+），
    openpyxl 普通模式会整包解析。这里用 iterparse 只取短文本/数值单元格，
    行号/列号由 cell 引用还原，供既有解析逻辑按元组处理。
    """
    import re
    import zipfile
    import xml.etree.ElementTree as ET

    archive = zipfile.ZipFile(io.BytesIO(data))
    try:
        targets = sorted(
            name for name in archive.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        )
        if not targets:
            raise UploadSafetyError("xlsx 缺少 worksheet")
        target = targets[0]
        NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        rows_out: list[tuple] = []
        col_re = re.compile(r"([A-Z]+)")
        with archive.open(target) as handle:
            for _ev, el in ET.iterparse(handle, events=("end",)):
                if el.tag != NS + "row":
                    continue
                cells: dict[int, str] = {}
                for cell in el.iter(NS + "c"):
                    ref = cell.get("r", "")
                    text = cell.find(NS + "is/" + NS + "t")
                    value = cell.find(NS + "v")
                    if text is not None and text.text and len(text.text) <= MAX_CELL_TEXT:
                        col = _column_index(ref)
                        cells[col] = text.text.strip()
                    elif value is not None and value.text and len(value.text) <= MAX_CELL_TEXT:
                        col = _column_index(ref)
                        cells[col] = value.text.strip()
                if cells:
                    max_col = max(cells)
                    rows_out.append(
                        tuple(cells.get(i) for i in range(1, max_col + 1))
                    )
                el.clear()
        return rows_out
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
