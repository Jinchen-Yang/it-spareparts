"""Excel 单元格到领域标量的无损边界。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, from_excel

from app.services.excel_text_safety import has_illegal_excel_upload_text
from app.services.workbook.schema import ColumnSpec

_INVISIBLE = re.compile("[\u200b-\u200d\u2060\ufeff\u00a0]")
_TRUE = {"是", "y", "1", "true", "对"}
_FALSE = {"否", "n", "0", "false", "错"}


def clean_text(value: str) -> str:
    if has_illegal_excel_upload_text(value):
        raise ValueError("含 Excel 不支持的控制字符，请删除后重试")
    return _INVISIBLE.sub("", value).strip()


def normalize(column: ColumnSpec, raw, *, date_system="1900"):
    value = clean_text(raw) if isinstance(raw, str) else raw
    if value is None or value == "":
        if column.edit == "required":
            raise ValueError("必填内容不能为空")
        return None
    kind = column.type
    if kind == "code":
        if not isinstance(value, str):
            raise ValueError("编码被转换为数字；请将该列设为文本后重新粘贴")
        result = value
    elif kind == "text":
        result = str(value)
    elif kind in {"int", "decimal"}:
        if isinstance(value, bool):
            raise ValueError("请填写数字，不能填写是否值")
        text = unicodedata.normalize("NFKC", str(value)).strip()
        if not isinstance(value, (int, float, Decimal)):
            text = text.removeprefix("¥").strip()
            if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
                raise ValueError("数字格式错误，请使用数字、千分位逗号及可选的人民币符号")
        try:
            number = Decimal(text.replace(",", ""))
        except InvalidOperation:
            raise ValueError("数字格式错误") from None
        if not number.is_finite():
            raise ValueError("数字必须为有限值")
        places = max(0, -number.as_tuple().exponent)
        limit = 0 if kind == "int" else column.decimals
        if limit is not None and places > limit:
            raise ValueError(f"小数位不能超过 {limit} 位，不会自动四舍五入")
        result = int(number) if kind == "int" else number
    elif kind == "date":
        if isinstance(value, datetime):
            result = value.date()
        elif isinstance(value, date):
            result = value
        elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            try:
                epoch = CALENDAR_MAC_1904 if date_system == "1904" else CALENDAR_WINDOWS_1900
                converted = from_excel(float(value), epoch)
                if isinstance(converted, time):
                    converted = datetime.combine(epoch.date(), converted)
                if not isinstance(converted, datetime):
                    raise ValueError
                result = converted.date()
            except (ValueError, OverflowError):
                raise ValueError("Excel 日期序列号无效") from None
        elif isinstance(value, str) and re.fullmatch(r"\d{4}([- /])\d{2}\1\d{2}", value) and " " not in value:
            try:
                result = date.fromisoformat(value.replace("/", "-"))
            except ValueError:
                raise ValueError("日期无效，请使用 YYYY-MM-DD 或 YYYY/MM/DD") from None
        else:
            raise ValueError("日期格式错误，请使用日期单元格、YYYY-MM-DD 或 YYYY/MM/DD")
    elif kind == "bool":
        text = str(value).casefold()
        if text in _TRUE:
            result = True
        elif text in _FALSE:
            result = False
        else:
            raise ValueError("是否值仅接受 是/否/Y/N/1/0/TRUE/FALSE/对/错")
    elif kind == "enum":
        choices = column.choices
        options = choices.items() if isinstance(choices, dict) else ((item, item) for item in choices)
        text = str(value).casefold()
        for code, label in options:
            if text in {str(code).casefold(), str(label).casefold()}:
                return code
        # 是否式枚举同时接受中文、字母和数字别名。
        if text in _TRUE | _FALSE:
            target = text in _TRUE
            options = choices.items() if isinstance(choices, dict) else ((item, item) for item in choices)
            for code, label in options:
                if any(str(v).casefold() in (_TRUE if target else _FALSE) for v in (code, label)):
                    return code
        raise ValueError("内容不在可选值范围内")
    else:
        raise ValueError("不支持的列类型")
    if isinstance(result, str) and len(result) > (column.max_len or 32767):
        raise ValueError(f"文本长度不能超过 {column.max_len or 32767} 个字符")
    return result
