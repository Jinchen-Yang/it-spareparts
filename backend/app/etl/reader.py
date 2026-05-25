"""文件解析：双表头探测 + 列名规整 + 重复列校验 + 文件识别 + ffill（§6.2）。"""
import re
from collections import Counter

import pandas as pd

from app.etl import mapping

_FCODE = re.compile(r"^F\d{7}$")


class ReaderError(Exception):
    """无法解析（识别失败 / 重复列名等），导致整批 failed。"""


def detect_header(raw: pd.DataFrame) -> int:
    """第 0 行命中 F\\d{7} ≥3 个 → 双表头(表头在第1行)，否则单表头(第0行)。"""
    first = raw.iloc[0].astype(str)
    return 1 if first.str.match(_FCODE).sum() >= 3 else 0


def _norm_cols(values) -> list[str]:
    return [str(c).strip() for c in values]


def read_excel(path: str) -> tuple[pd.DataFrame, str]:
    """读取 .xlsx → (规整后的明细行 DataFrame, file_type)。

    步骤：探测表头 → 取中文列名(strip) → 重复列校验 → 识别类型 → ffill 头字段。
    """
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

    dup = [c for c, n in Counter(cols).items() if n > 1 and c not in ("", "NAN", "None")]
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
