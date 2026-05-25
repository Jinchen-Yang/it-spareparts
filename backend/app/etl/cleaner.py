"""逐字段清洗与标准化（§7.1）。

约定：
- 空值（None/空串/'nan'）→ 返回 None，不报错。
- 非空但不合法 → 抛 ValueError，由 validate 层捕获并记 sys_import_error。
- 金额/数量/税率分别按 0.01 / 0.001 / 0.0001 精度 quantize。
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd

# 氚云内部 V 码 token：V + ≥10 位大写字母数字（实测含字母，如 V0230LD000000000）
_VCODE_TOKEN = re.compile(r"^V[0-9A-Z]{10,}$")
_THOUSANDS = re.compile(r"[,\s¥￥$]")
_CATEGORY_PLACEHOLDER = "原始系统"          # 占位垃圾（采购/部分销售）
_SUPPLIER_SUFFIX = re.compile(r"（[^（）]*）")  # 全角括号后缀，如（质保三个月）


def _is_blank(x) -> bool:
    if x is None:
        return True
    if isinstance(x, float) and pd.isna(x):
        return True
    s = str(x).strip()
    return s == "" or s.lower() == "nan"


def clean_str(x) -> str | None:
    if _is_blank(x):
        return None
    return str(x).strip()


def clean_category(x) -> str | None:
    """产品大类/小类：占位 '原始系统*' 视为空（§4.3 实测 64% 销售大类为占位）。"""
    s = clean_str(x)
    if s is None or s.startswith(_CATEGORY_PLACEHOLDER):
        return None
    return s


def standardize_pn(raw) -> tuple[str | None, str | None, bool]:
    """策略 B：去 V 码 token 及其后内容。返回 (pn_std, pn_raw, needs_review)。

    - 命中 V 码 token：截断该 token 及其后，取前段为 pn_std，needs_review=False。
    - 含空格但无 V 码（真正歧义）：pn_std = 原始大写值(保留空格)，needs_review=True。
    - 空值：(None, None, False)。
    """
    if _is_blank(raw):
        return None, None, False
    pn_raw = str(raw).strip()
    s = pn_raw.upper()
    tokens = s.split()
    kept: list[str] = []
    for t in tokens:
        if _VCODE_TOKEN.match(t):
            break
        kept.append(t)
    if kept and len(kept) < len(tokens):       # 命中 V 码并截断
        std = " ".join(kept)
        # 截断后通常单 token；若仍含空格仍视为干净（真实多段在 V 码前）
        return std, pn_raw, False
    if " " in s:                                # 含空格但无 V 码 → 歧义，保留原值待人工
        return s, pn_raw, True
    return s, pn_raw, False


def parse_rate(x) -> Decimal | None:
    """'13.0%'→0.1300，'0.0%'→0，0.13→0.1300。"""
    if _is_blank(x):
        return None
    s = str(x).strip()
    try:
        if s.endswith("%"):
            val = Decimal(s[:-1].strip()) / Decimal(100)
        else:
            val = Decimal(s)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"税率非法: {x!r}") from exc
    return val.quantize(Decimal("0.0001"))


def _parse_decimal(x, places: str, label: str) -> Decimal | None:
    if _is_blank(x):
        return None
    s = _THOUSANDS.sub("", str(x).strip())
    try:
        return Decimal(s).quantize(Decimal(places))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label}非数字: {x!r}") from exc


def parse_money(x) -> Decimal | None:
    return _parse_decimal(x, "0.01", "金额")


def parse_qty(x) -> Decimal | None:
    return _parse_decimal(x, "0.001", "数量")


def parse_int(x) -> int | None:
    """序号等：非法返回 None（不阻断，§4.3）。"""
    if _is_blank(x):
        return None
    try:
        return int(float(str(x).strip()))
    except (ValueError, TypeError):
        return None


def parse_date(x) -> date | None:
    if _is_blank(x):
        return None
    ts = pd.to_datetime(x, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"日期非法: {x!r}")
    return ts.date()


def normalize_supplier_name(raw) -> tuple[str | None, str | None]:
    """返回 (name_raw, name_normalized)。规范名去全角括号后缀（质保/采购等）。"""
    name_raw = clean_str(raw)
    if name_raw is None:
        return None, None
    normalized = _SUPPLIER_SUFFIX.sub("", name_raw).strip()
    return name_raw, (normalized or name_raw)


_SOURCE_TYPE_MAP = {
    "销售订单": "销售订单", "指定采购": "指定采购", "维保需求": "维保需求",
    # 兼容旧模版/其它写法
    "销售": "销售订单", "备货": "指定采购", "维保": "维保需求", "回收": "回收",
}


def normalize_source_type(raw) -> str | None:
    """采购类型标准化（§7.1，实测真实值：销售订单/维保需求/指定采购）。"""
    s = clean_str(raw)
    if s is None:
        return None
    for k, v in _SOURCE_TYPE_MAP.items():
        if k in s:
            return v
    return s
