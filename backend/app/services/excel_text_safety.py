"""Excel/XML 1.0 不可表示的控制字符：入口拒绝，导出兜底剔除。

openpyxl 对 C0 控制字符（除 \\t \\n \\r）及 U+FFFE/U+FFFF 赋值即抛
IllegalCharacterError；只要有一个字段含这类字符，整本工作簿导出就 500。
"""

from __future__ import annotations

import re

from openpyxl import Workbook

ILLEGAL_EXCEL_CHARS = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\ufffe\\uffff]")


# Excel 把单元格里的控制字符存成 OOXML 转义 ``_xHHHH_``；openpyxl 读取时不还原，
# 上传值里出现的是这串字面文本，Excel 打开时又会显示成控制字符。上传入口一并拒绝。
ESCAPED_ILLEGAL_EXCEL_CHARS = re.compile(
    "_x(?:000[0-8]|000[bBcC]|000[eEfF]|001[0-9a-fA-F]|[fF]{3}[eEfF])_")


def has_illegal_excel_chars(value: object) -> bool:
    return isinstance(value, str) and ILLEGAL_EXCEL_CHARS.search(value) is not None


def has_illegal_excel_upload_text(value: object) -> bool:
    """上传入口：真实控制字符或其 OOXML 转义形式。"""
    return isinstance(value, str) and (
        ILLEGAL_EXCEL_CHARS.search(value) is not None
        or ESCAPED_ILLEGAL_EXCEL_CHARS.search(value) is not None)


def excel_safe(value):
    """导出兜底：只处理字符串，剔除不可表示字符；其他类型原样返回。"""
    if isinstance(value, str) and ILLEGAL_EXCEL_CHARS.search(value):
        return ILLEGAL_EXCEL_CHARS.sub("", value)
    return value


def _harden(ws):
    original_append = ws.append
    original_cell = ws.cell

    def append(iterable):
        if isinstance(iterable, dict):
            return original_append({key: excel_safe(value) for key, value in iterable.items()})
        return original_append([excel_safe(value) for value in iterable])

    def cell(row, column, value=None):
        return original_cell(row, column, excel_safe(value))

    ws.append = append
    ws.cell = cell
    return ws


class SafeTextWorkbook(Workbook):
    """经 append / cell(value=...) 写入的字符串一律先剔除不可表示字符。

    只是导出兜底；入口仍须拒绝这些字符，避免数据库里留下与导出不一致的值。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for ws in self.worksheets:
            _harden(ws)

    def create_sheet(self, *args, **kwargs):
        return _harden(super().create_sheet(*args, **kwargs))
