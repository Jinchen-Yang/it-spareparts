"""看板导出纯文本防公式模块（Lane B / R34）：与商城参考实现共享同一套合成案例。

生产 api.maintenance 无 _safe_formula_only，巨大 API 不可 import——本模块是看板
自有的独立拷贝（D-0024）。案例来源：test_maintenance_row_workbench_review.py 的
CSV_UNCHANGED/CSV_ESCAPED/全角组，行为必须逐字一致，两处重复由主代理集成期统一。
"""

from decimal import Decimal

import pytest

from app.services.maintenance_dashboard_export_text import (
    MAX_CSV_CELL_CHARS,
    ExportCellLimitError,
    safe_formula_only,
)

# 符号开头的业务值原样（-48V 电源规格、电话号码、±公差等）。
CSV_UNCHANGED = [
    "-48V", "+5V", "-1.500", "-1e3", "+86 138 0000 0000", "-", "-文本",
    "-48V/-60V", "-48V(DC)", "-48V DC, 1100W", "+12V/5A", "+5V/-12V", "+/-0.5mm",
    "--", "--N/A--", "+DC48V",
    "+1E+5",
    "+/-5%", "-48V~-60V", "+3.3V", "-40~85℃", "-2U", "-1.5m", "+12V 500W",
    "-48V DC 2000W 冗余", "AC220V", "+",
    "SN-1", "-B2B", "普通文本",
]

# 真正的公式（=、@ 一律；+/- 须命中函数/引用/DDE/常量表达式/括号数字启发式）。
CSV_ESCAPED = [
    "=1+1", "=HYPERLINK(\"http://x\")", "  =A1+1", "@SUM(A1)",
    "\tplain", "\t-48V", "\r1", "\nx",
    "-2+3+cmd|' /C calc'!A0", "+cmd|' /C calc'!A0",
    "+cmd|x",
    "+ A1", "- A1", "-\tA1", "-A1", "+$B$2", "-A1:B3", " + A1",
    "+SUM(A1:A2)", "-sum (1)", "+TRUE", "-PI", "+false", "+ TRUE",
    "+A:A", "-$A:$A", "+A:B", "-XFD:XFD", "+1:1", "-1:3", "+$1:$1", "+A:A*1", "+ A:A",
    "+\"x\"&A:A", "+\"a\"&1:1", "+12:30",
    "+(1)", "-(1+2)", "+--1", "+\"a\"&\"b\"", "-1<2", "+1%", "+ 1+1",
    pytest.param("+" + "(" * 5000 + "1" + ")" * 5000, id="deep-parens"),
    "+86-138-0000-0000", "-1+1", "+1 (800) 555-1234",
    "-Dell R740", "-IBM X3650", "-A1-型号", "+A2版", "-48V(A1)",
]

# 全角 ＝＋－ 不是 Excel 公式前缀：原样。
CSV_FULLWIDTH_UNCHANGED = ["＝1+1", "＋A1", "－1", "＋SUM(A1)", "－A:A"]


@pytest.mark.parametrize("value", CSV_FULLWIDTH_UNCHANGED)
def test_formula_guard_leaves_fullwidth_prefixes(value):
    assert safe_formula_only(value) == value


@pytest.mark.parametrize("value", CSV_UNCHANGED)
def test_formula_guard_keeps_business_values(value):
    assert safe_formula_only(value) == value


@pytest.mark.parametrize("value", CSV_ESCAPED)
def test_formula_guard_escapes_real_formulas(value):
    assert safe_formula_only(value) == "'" + value


def test_formula_guard_strips_control_characters():
    # 控制字符先清理，再判公式：\x01= 描述仍以 ' 开头且不含 \x01。
    cleaned = safe_formula_only("\x01=DESCRIPTION()")
    assert cleaned == "'=DESCRIPTION()"
    assert "\x01" not in cleaned


def test_formula_guard_ignores_non_text():
    assert safe_formula_only(Decimal("-1.500")) == Decimal("-1.500")
    assert safe_formula_only(None) is None
    assert safe_formula_only(3) == 3


def test_formula_guard_parens_heuristic_is_linear_time():
    """白名单先行：32k 对抗串须在 100ms 内判定，旧正则二次回溯会卡 GIL 数秒。"""
    import time

    adversarial = "+" + "(" * 32_000 + "x"
    assert len(adversarial) <= MAX_CSV_CELL_CHARS
    t0 = time.perf_counter()
    assert safe_formula_only(adversarial) == adversarial
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1, f"ReDoS regression: {elapsed:.3f}s on 32k paren input"


def test_formula_guard_rejects_oversize_before_heuristics():
    """超 32767 须在启发式之前抛 ExportCellLimitError；不得先跑括号正则再拒绝。"""
    import time

    adversarial = "+" + "(" * MAX_CSV_CELL_CHARS + "x"
    assert len(adversarial) > MAX_CSV_CELL_CHARS
    t0 = time.perf_counter()
    with pytest.raises(ExportCellLimitError):
        safe_formula_only(adversarial)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1, f"oversize path too slow: {elapsed:.3f}s"


def test_formula_guard_rejects_escaped_cell_over_excel_limit():
    # 转义后（加撇号）仍超上限：同样 4xx 级拒绝，由 API 层转 413。
    value = "  =" + "x" * MAX_CSV_CELL_CHARS
    with pytest.raises(ExportCellLimitError):
        safe_formula_only(value)


def test_formula_guard_error_message_is_user_facing():
    with pytest.raises(ExportCellLimitError) as raised:
        safe_formula_only("=" + "x" * (MAX_CSV_CELL_CHARS + 1))
    assert str(MAX_CSV_CELL_CHARS) in str(raised.value)
