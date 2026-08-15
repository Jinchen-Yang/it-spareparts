"""台账日期/金额/项目名称归一化工具测试（B4 共享工具）。"""

from datetime import date, datetime

from app.services.date_loose import (
    parse_amount_loose,
    parse_date_loose,
    parse_project_name,
)


def test_parse_date_iso():
    value, precision = parse_date_loose("2026-10-01")
    assert value == date(2026, 10, 1)
    assert precision == "day"


def test_parse_date_year_month():
    value, precision = parse_date_loose("2026-10")
    assert value == date(2026, 10, 1)
    assert precision == "month"


def test_parse_date_chinese_full():
    value, precision = parse_date_loose("2026年10月1日")
    assert value == date(2026, 10, 1)
    assert precision == "day"


def test_parse_date_chinese_month_only():
    value, precision = parse_date_loose("2026年10月")
    assert value == date(2026, 10, 1)
    assert precision == "month"


def test_parse_date_slashes_and_dots():
    assert parse_date_loose("2026/10/01")[0] == date(2026, 10, 1)
    assert parse_date_loose("2026.10.01")[0] == date(2026, 10, 1)
    assert parse_date_loose("2026/10") == (date(2026, 10, 1), "month")


def test_parse_date_compact():
    assert parse_date_loose("20261001") == (date(2026, 10, 1), "day")
    assert parse_date_loose("202610") == (date(2026, 10, 1), "month")


def test_parse_date_full_width():
    value, precision = parse_date_loose("２０２６年１０月０１日")
    assert value == date(2026, 10, 1)
    assert precision == "day"


def test_parse_date_empty_placeholders():
    assert parse_date_loose("、") == (None, None)
    assert parse_date_loose("/") == (None, None)
    assert parse_date_loose("") == (None, None)
    assert parse_date_loose(None) == (None, None)


def test_parse_date_native_types():
    assert parse_date_loose(date(2026, 10, 1)) == (date(2026, 10, 1), "day")
    assert parse_date_loose(datetime(2026, 10, 1, 8, 30)) == (date(2026, 10, 1), "day")


def test_parse_date_invalid():
    assert parse_date_loose("2026-13-40") == (None, None)
    assert parse_date_loose("明年") == (None, None)


def test_parse_amount_plain():
    from decimal import Decimal

    assert parse_amount_loose("2986.57") == Decimal("2986.57")
    assert parse_amount_loose(2986.57) == Decimal("2986.57")


def test_parse_amount_chinese_and_currency():
    from decimal import Decimal

    assert parse_amount_loose("¥1,068.5") == Decimal("1068.5")
    assert parse_amount_loose("1，068.5元") == Decimal("1068.5")


def test_parse_amount_empty():
    assert parse_amount_loose("、") is None
    assert parse_amount_loose("/") is None
    assert parse_amount_loose(None) is None


def test_parse_amount_negative_rejected():
    assert parse_amount_loose("-5") is None


def test_parse_project_name_with_period():
    parsed = parse_project_name("阿里专有云20260608-20291205")
    assert parsed["period_from"] == date(2026, 6, 8)
    assert parsed["period_to"] == date(2029, 12, 5)
    assert parsed["identity"] == "阿里专有云20260608-20291205"


def test_parse_project_name_with_prefix():
    parsed = parse_project_name("预交付-大疆20260201-20261231新华三整体维保")
    assert parsed["identity"] == "大疆20260201-20261231新华三整体维保"
    assert parsed["period_from"] == date(2026, 2, 1)
    assert parsed["period_to"] == date(2026, 12, 31)


def test_parse_project_name_truncated():
    parsed = parse_project_name("正大天晴20260801-20270531因")
    assert parsed["identity"] == "正大天晴20260801-20270531因"
    assert parsed["period_from"] == date(2026, 8, 1)
    assert parsed["period_to"] == date(2027, 5, 31)


def test_parse_project_name_no_period():
    parsed = parse_project_name("GPU供货")
    assert parsed["identity"] == "GPU供货"
    assert parsed["period_from"] is None
    assert parsed["period_to"] is None
