"""宽松日期/金额/项目名称归一化（导入层共享工具，B4）。

规则（docs/maintenance/workbook-template-design.md §7）：
- 完整日期 YYYY-MM-DD；仅有年月的期间 YYYY-MM；中文「YYYY年MM月[DD日]」；
  `.`、`/`、`-`、顿号、全角数字全部归一；无法归一化返回 None（不猜值）；
- `、`、`/` 单独占位视为空；
- 项目名称解析：剥离「预交付-」等前缀，提取内嵌 8 位起止日期作为维保期限。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

_FULL_WIDTH_TRANS = str.maketrans(
    "０１２３４５６７８９．－／年：：",
    "0123456789.-/年::",
)
_EMPTY_TOKENS = {"", "/", "、", "-", "--", "—", "无", "否", "n/a", "N/A", "N\\A"}
_CN_DATE_RE = re.compile(
    r"(?P<y>20\d{2})\s*年\s*(?P<m>\d{1,2})\s*月(?:\s*(?P<d>\d{1,2})\s*[日号])?"
)
_COMPACT_RE = re.compile(r"^(20\d{2})(\d{2})(\d{2})$")
_YM_COMPACT_RE = re.compile(r"^(20\d{2})(\d{2})$")
_SEP_DATE_RE = re.compile(
    r"^(20\d{2})\s*[-/.]\s*(\d{1,2})(?:\s*[-/.]\s*(\d{1,2}))?$"
)
# 项目名称内嵌期限：20260608-20291205 / 2026.06.08-2029.12.05
_PROJECT_PERIOD_RE = re.compile(
    r"(20\d{2})[-/.]?(\d{2})[-/.]?(\d{2})\s*[-~—至到]\s*(20\d{2})[-/.]?(\d{2})[-/.]?(\d{2})"
)
_PROJECT_PREFIX_RE = re.compile(r"^(?:预交付|预付|预)\s*[-—–]?\s*")

EXCEL_EPOCH = datetime(1899, 12, 30)


def _strip_cn(value: str) -> str:
    value = value.translate(_FULL_WIDTH_TRANS)
    return re.sub(r"[\s\u3000]+", "", value)


def parse_date_loose(value) -> tuple[date | None, str | None]:
    """解析宽松日期，返回 (date, precision)。

    precision ∈ {"day", "month", None}；无法解析返回 (None, None)。
    """
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value.date(), "day"
    if isinstance(value, date):
        return value, "day"
    if isinstance(value, (int, float)):
        try:
            return (EXCEL_EPOCH + timedelta(days=int(value))).date(), "day"
        except (OverflowError, ValueError):
            return None, None
    text = str(value).strip()
    if text in _EMPTY_TOKENS:
        return None, None
    text = _strip_cn(text)
    if not text:
        return None, None
    cn = _CN_DATE_RE.search(text)
    if cn:
        year, month = int(cn.group("y")), int(cn.group("m"))
        day = int(cn.group("d")) if cn.group("d") else None
        try:
            if day is not None:
                return date(year, month, day), "day"
            return date(year, month, 1), "month"
        except ValueError:
            return None, None
    compact = _COMPACT_RE.match(text)
    if compact:
        try:
            return (
                date(int(compact.group(1)), int(compact.group(2)), int(compact.group(3))),
                "day",
            )
        except ValueError:
            return None, None
    ym = _YM_COMPACT_RE.match(text)
    if ym:
        try:
            return date(int(ym.group(1)), int(ym.group(2)), 1), "month"
        except ValueError:
            return None, None
    sep = _SEP_DATE_RE.match(text)
    if sep:
        year, month = int(sep.group(1)), int(sep.group(2))
        day = int(sep.group(3)) if sep.group(3) else None
        try:
            if day is not None:
                return date(year, month, day), "day"
            return date(year, month, 1), "month"
        except ValueError:
            return None, None
    return None, None


def parse_amount_loose(value) -> Decimal | None:
    """解析金额：去千分位/货币符号/「元」；空与占位返回 None。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value >= 0 else None
    if isinstance(value, (int, float)):
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            return None
        return result if result >= 0 else None
    text = str(value).strip()
    if text in _EMPTY_TOKENS:
        return None
    text = _strip_cn(text)
    text = text.replace(",", "").replace("，", "").replace("￥", "").replace("¥", "")
    text = text.replace("元", "").replace(" ", "")
    if text in _EMPTY_TOKENS or text == "":
        return None
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    return result if result >= 0 else None


def parse_project_name(raw: str) -> dict:
    """解析台账项目名称：返回 {name, period_from, period_to}。

    - name：剥离「预交付-」前缀与期限段外的截断垃圾后的可读名（保留原串主体）；
    - period_from/period_to：名称内嵌 8 位起止日期（Q12：项目名自带项目期限）。
    """
    name = (raw or "").strip()
    period_from: date | None = None
    period_to: date | None = None
    match = _PROJECT_PERIOD_RE.search(name)
    if match:
        try:
            period_from = date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
            period_to = date(
                int(match.group(4)), int(match.group(5)), int(match.group(6))
            )
        except ValueError:
            period_from = period_to = None
    # 项目身份键 = 剥离前缀后的完整名称（含期限段），同名即同项目。
    identity = _PROJECT_PREFIX_RE.sub("", name).strip()
    return {"name": name, "identity": identity, "period_from": period_from, "period_to": period_to}
