"""文件解析：双表头探测 + 列名规整 + 重复列校验 + 文件识别 + ffill（§6.2）。"""
import re
from collections import Counter

import pandas as pd
from openpyxl import load_workbook

from app import config
from app.etl import mapping

_FCODE = re.compile(r"^F\d{7}$")
# 空表头单元格归一：pandas 以 dtype=object 读空单元格为 float('nan')→str 后是小写 'nan'，
# openpyxl 流式读则是 None；统一折叠为 ""，避免被误判为「重复列名」（审计 2026-06-28 I-1）。
_BLANK_HEADER = ("", "nan", "none", "<na>")


class ReaderError(Exception):
    """无法解析（识别失败 / 重复列名 / 超限等），导致整批 failed。"""


def _check_workbook_size(path: str) -> None:
    """流式探测行规模，超 IMPORT_MAX_ROWS 直接拒绝，避免 pandas 全量物化 OOM（I-2）。"""
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return  # 损坏/非 xlsx 交给下方 pd.read_excel 给出统一「无法解析」提示
    try:
        ws = wb.active
        if ws is None:
            return
        limit = config.IMPORT_MAX_ROWS
        max_row = ws.max_row  # read_only 下通常来自 <dimension> 标签，O(1)；缺失时为 None
        if max_row is not None:
            if max_row > limit:
                raise ReaderError(
                    f"文件行数 {max_row} 超过 {limit} 行上限，请拆分后再导入"
                    "（避免占用过多内存影响其他用户）。")
            return
        n = 0  # 维度未知 → 流式计数封顶（仍是低内存逐行迭代）
        for _ in ws.iter_rows(values_only=True):
            n += 1
            if n > limit:
                raise ReaderError(f"文件行数超过 {limit} 行上限，请拆分后再导入。")
    finally:
        wb.close()


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


def read_excel(path: str) -> tuple[pd.DataFrame, str]:
    """读取 .xlsx → (规整后的明细行 DataFrame, file_type)。

    步骤：行数上限预检 → 探测表头 → 取中文列名(strip) → 重复列校验 → 识别类型 → ffill 头字段。
    """
    _check_workbook_size(path)
    try:
        raw = pd.read_excel(path, header=None, dtype=object, engine="openpyxl")
    except Exception as exc:  # BadZipFile / openpyxl 格式错误 / 损坏
        raise ReaderError(
            "文件无法按 .xlsx 解析：可能是旧版 .xls 格式、非 Excel 文件或文件已损坏。"
            "请在 Excel 中打开后「另存为 → Excel 工作簿 (.xlsx)」再上传。"
        ) from exc
    if raw.empty:
        raise ReaderError("文件为空")

    h = detect_header(raw)
    cols = _norm_cols(raw.iloc[h].tolist())
    # 容差归一：把 (必填) 等注解差异的列名规范到 mapping 键，避免非标导出整列读空（empty_pn）
    cols = mapping.canonicalize_columns(cols)

    dup = [c for c, n in Counter(cols).items() if n > 1 and c != ""]
    if dup:
        raise ReaderError(f"表头存在重复列名：{dup}，请确认导出模版")

    df = raw.iloc[h + 1:].reset_index(drop=True)
    df.columns = cols

    file_type = mapping.detect_file_type(cols)
    if file_type is None:
        raise ReaderError("无法识别文件类型，请确认是采购/销售/库存导出文件")

    ffill_cols = [c for c in mapping.FFILL_COLS[file_type] if c in df.columns]
    if ffill_cols:
        # 仅填 NA、不覆盖已有值：续行主表空 → 补成所属订单（前提：订单首行头字段非空，已实测）
        df[ffill_cols] = df[ffill_cols].replace("", pd.NA).ffill()

    return df, file_type
