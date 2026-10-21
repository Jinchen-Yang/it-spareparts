"""sales_export_text 纯函数单测（D-33 提取副本，2026-10-22 销售/采购看板轮次）。

不依赖 FastAPI/HTTP/数据库——只测文本进出语义：
- 公式形态（= / @ / +/- 函数调用、DDE、单元格引用、裸名、常量表达式、括号数字）转义；
- 负数与负 PN / 电话号码不误伤；
- 控制字符剔除；32,767 上限（转义前后的两道闸门）；超深括号不抛 RecursionError。
"""
from decimal import Decimal

import pytest

from app.services.sales_export_text import ExportTextTooLong, safe_formula_text


@pytest.mark.parametrize("raw,expected", [
    # = / @ 开头一律转义（忽略前导空白）
    ("=SUM(A1:A9)", "'=SUM(A1:A9)"),
    ("  =1+1", "'  =1+1"),
    ("@cmd", "'@cmd"),
    ("@SUM(A1)", "'@SUM(A1)"),
    # Tab/CR/LF 开头
    ("\t=1", "'\t=1"),
    ("\rA1", "'\rA1"),
    ("\nfoo", "'\nfoo"),
    # + / - 函数调用与 DDE
    ("+HYPERLINK(\"http://x\")", "'+HYPERLINK(\"http://x\")"),
    ("-2+3+cmd|' /C x'", "'-2+3+cmd|' /C x'"),
    ("-cmd!DDE", "'-cmd!DDE"),
    # + / - 单元格引用 / 整列 / 整行
    ("+A1", "'+A1"),
    ("- $B$2", "'- $B$2"),
    ("+A1:B3", "'+A1:B3"),
    ("-A:A", "'-A:A"),
    ("+1:1", "'+1:1"),
    # 裸名与常量表达式（单个数字除外）
    ("+TRUE", "'+TRUE"),
    ("-FALSE and more", "'-FALSE and more"),
    ("+1+1", "'+1+1"),
    ("-2^10", "'-2^10"),
    ("- (1+2)*3", "'- (1+2)*3"),
    # 括号数字启发式（整段数字/运算符/括号且含括号）
    ("+1 (2)", "'+1 (2)"),
])
def test_formula_shapes_escaped(raw, expected):
    assert safe_formula_text(raw) == expected


@pytest.mark.parametrize("raw", [
    # 负数 / 数值形态不误伤
    "-1.500", "-1e3", "+1E+5", "-48V", "+86 13800138000", "-0.5", "+123",
    # 普通文本与不含公式要素的 +/-
    "+hello", "-备用件", "A1 备件", "正常文本 =SUM 不在开头",
    "PN-48V", "3-5 天", "100%",
    # 单个数字不是常量表达式
    "+1", "- 42",
    # 引用后缀不误伤（后面跟字母/数字/点/括号时不是单元格引用）
    "+A1B", "-XFD42X",
])
def test_non_formula_untouched(raw):
    assert safe_formula_text(raw) == raw


def test_control_characters_stripped():
    # 剔除后不再命中公式判定（\x0b 在 = 前被剔除）
    assert safe_formula_text("=\x0bSUM(A1)") == "'=SUM(A1)"
    assert safe_formula_text("a\x00b\x7f￾c") == "abc"
    # Tab 在开头是转义信号；中间的 Tab 不剔除（CSV 字段内合法）且不触发
    out = safe_formula_text("a\tb")
    assert out == "a\tb"


def test_non_string_passthrough():
    assert safe_formula_text(None) is None
    assert safe_formula_text(123) == 123
    assert safe_formula_text(-500.0) == -500.0
    assert safe_formula_text(Decimal("1.5")) == Decimal("1.5")


def test_length_gate_before_and_after_escape():
    max_ok = "x" * 32_767
    assert safe_formula_text(max_ok) == max_ok
    with pytest.raises(ExportTextTooLong):
        safe_formula_text("x" * 32_768)
    # 转义后超限同样 413 语义：恰好 32,767 的公式文本加撇号后超限
    with pytest.raises(ExportTextTooLong):
        safe_formula_text("=A1" + "x" * 32_764)


def test_deep_parentheses_no_crash():
    # 超深括号：RecursionError 兜底按公式转义，绝不 500 / 不挂起
    deep = "+" + "(" * 5_000 + "1" + ")" * 5_000
    out = safe_formula_text(deep)
    assert out.startswith("'+(((")


def test_long_adversarial_input_fast():
    # 长度闸门在启发式之前：32,768 长度的对抗串立刻 ExportTextTooLong，
    # 不进入正则回溯（ReDoS 回归，见 _CSV_NUMERIC_CHARS 注释）
    with pytest.raises(ExportTextTooLong):
        safe_formula_text("+" + "1 (2" * 8_200)   # 32,801 > 32,767
