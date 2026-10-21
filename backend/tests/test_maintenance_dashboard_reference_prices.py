"""看板自有双税加权参考引擎（R34 / D-0024）：不 import 商城 resale 私有 helper。

生产 resale_price_reference._Group/_weighted_amounts 与 main 形状不兼容——
本模块必须自持算法（数量×时间衰减、半衰期 7 天、逐组税侧换算、HALF_UP）。
预期值全部手算（独立于实现的算例），不用实现复算。
"""

import inspect
from datetime import date
from decimal import Decimal

import pytest

from app.services.maintenance_dashboard_reference_prices import (
    DECAY_HALF_LIFE_DAYS,
    REFERENCE_WINDOW_DAYS,
    ReferenceCalculationError,
    SampleGroup,
    window_bounds,
    weighted_average_price,
)


def _group(
    *, order_date: date, rate: str = "0.13", amount: str = "100",
    qty: str = "10", tax_inclusive: bool = False, bad_rate_count: int = 0,
    line_count: int = 1, order_count: int = 1,
) -> SampleGroup:
    return SampleGroup(
        order_date=order_date, rate=Decimal(rate), amount_raw=Decimal(amount),
        qty=Decimal(qty), price_sum=Decimal(amount), line_count=line_count,
        order_count=order_count, nominal_ex_count=line_count,
        bad_rate_count=bad_rate_count, nominal_inc_count=0,
        is_tax_inclusive=tax_inclusive,
    )


END = date(2026, 10, 19)


def test_mixed_tax_sides_with_decay_hand_example():
    """锚点日未税 100/10 + 七天前含税 200/10（衰减 0.5）的加权双税均价。

    inc = (100×1.13×1 + 200×0.5) / (10×1 + 10×0.5) = 213/15 = 14.20
    ex  = (100×1 + 200×0.5/1.13) / 15 = 188.4955…/15 = 12.5663… → 12.57
    """
    groups = [
        _group(order_date=END, amount="100", qty="10", tax_inclusive=False),
        _group(
            order_date=END - _days(7), amount="200", qty="10",
            tax_inclusive=True,
        ),
    ]
    inc, ex = weighted_average_price(groups, end=END)
    assert (format(inc, ".2f"), format(ex, ".2f")) == ("14.20", "12.57")


def test_ex_tax_only_group_converts_to_inc():
    """未税侧：inc = ex × (1+rate)；rate=0 时两侧相等。"""
    groups = [_group(order_date=END, rate="0", amount="100", qty="10")]
    inc, ex = weighted_average_price(groups, end=END)
    assert (format(inc, ".2f"), format(ex, ".2f")) == ("10.00", "10.00")
    groups = [_group(order_date=END, rate="0.13", amount="100", qty="10")]
    inc, ex = weighted_average_price(groups, end=END)
    assert (format(inc, ".2f"), format(ex, ".2f")) == ("11.30", "10.00")


def test_inc_tax_only_group_converts_to_ex():
    groups = [_group(order_date=END, rate="0.13", amount="113", qty="10",
                     tax_inclusive=True)]
    inc, ex = weighted_average_price(groups, end=END)
    assert (format(inc, ".2f"), format(ex, ".2f")) == ("11.30", "10.00")


def test_half_life_seven_days_halves_the_weight():
    """半衰 7 天：七天前同样本权重减半 → 均价 (100+50)/(10+5)=10.00。"""
    groups = [
        _group(order_date=END, rate="0", amount="100", qty="10"),
        _group(order_date=END - _days(7), rate="0", amount="100", qty="10"),
    ]
    inc, ex = weighted_average_price(groups, end=END)
    assert (format(inc, ".2f"), format(ex, ".2f")) == ("10.00", "10.00")


def test_older_sample_decays_beyond_half_life():
    """14 天前权重 0.25：均价被拉向锚点日的低价样本。

    新样本单价 10（100/10），旧样本单价 25（250/10）：
    衰减均价 = (100×1 + 250×0.25)/(10 + 10×0.25) = 162.5/12.5 = 13.00；
    不衰减会是 (100+250)/20 = 17.50。
    """
    groups = [
        _group(order_date=END, rate="0", amount="100", qty="10"),
        _group(order_date=END - _days(14), rate="0", amount="250", qty="10"),
    ]
    inc, _ex = weighted_average_price(groups, end=END)
    assert format(inc, ".2f") == "13.00"


def test_rounding_is_half_up_at_two_decimals():
    """1.005 → 1.01（HALF_UP），不向偶数舍成 1.00。"""
    groups = [_group(order_date=END, rate="0", amount="1005", qty="1000")]
    inc, _ex = weighted_average_price(groups, end=END)
    assert format(inc, ".2f") == "1.01"


def test_no_groups_returns_none_not_zero():
    assert weighted_average_price([], end=END) is None


def test_bad_rate_group_raises_instead_of_silent_drop():
    """坏税率样本：整域不可用（异常），不得静默丢行或发部分结果。"""
    groups = [
        _group(order_date=END, rate="0", amount="100", qty="10"),
        _group(order_date=END, rate="0", amount="100", qty="10", bad_rate_count=1),
    ]
    with pytest.raises(ReferenceCalculationError):
        weighted_average_price(groups, end=END)


def test_zero_or_invalid_denominator_raises():
    with pytest.raises(ReferenceCalculationError):
        weighted_average_price(
            [_group(order_date=END, amount="100", qty="0")], end=END
        )


def test_oversize_amount_raises():
    with pytest.raises(ReferenceCalculationError):
        weighted_average_price(
            [_group(order_date=END, amount="1e14", qty="1")], end=END
        )


def test_deep_sample_mix_precision_does_not_drift():
    """50 位精度：多组含税/未税混合后与逐组两 Decimal 结果一致（手算锚定）。"""
    groups = [
        _group(order_date=END, amount="33.33", qty="3", tax_inclusive=False),
        _group(order_date=END, amount="56.50", qty="5", tax_inclusive=True),
    ]
    inc, ex = weighted_average_price(groups, end=END)
    # inc = (33.33×1.13 + 56.50)/8 = (37.6629+56.5)/8 = 94.1629/8 = 11.7703625 → 11.77
    # ex  = (33.33 + 56.5/1.13)/8 = (33.33+50)/8 = 83.33/8 = 10.416250 → 10.42
    assert (format(inc, ".2f"), format(ex, ".2f")) == ("11.77", "10.42")


def test_window_bounds_are_seven_shanghai_days_ending_at_anchor():
    """R34-01：三域窗口 = 含结束日的 7 个上海日历日 [anchor-6, anchor]。"""
    start, end = window_bounds(END)
    assert (start, end) == (date(2026, 10, 13), date(2026, 10, 19))
    assert REFERENCE_WINDOW_DAYS == 7
    assert DECAY_HALF_LIFE_DAYS == Decimal("7")
    assert (end - start).days == 6


def test_module_does_not_import_resale_price_reference_privates():
    """生产兼容红线：不得 import 商城 resale 私有 helper（形状 prod/main 不兼容）。"""
    import ast

    from app.services import maintenance_dashboard_reference_prices as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any("resale_price_reference" in name for name in names), (
            "看板参考引擎必须自持算法，不得 import 商城 resale_price_reference"
        )


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)
