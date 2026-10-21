"""看板自有的双税数量加权参考引擎（R34 / D-0024）。

生产隔离红线：商城 ``resale_price_reference._Group/_weighted_amounts`` 在生产
与 main 形状不兼容（prod：amount_inc/nominal_count；main：amount_raw/
nominal_ex_count/is_tax_inclusive），不可 import。本模块自持同口径算法：

- 数量×时间衰减加权（R34-06）：decay = 0.5^(距锚点天数 / 半衰期)，半衰期 7 天；
- 逐组按原始税侧换算双税（含税 → ex = raw/(1+rate)；未税 → inc = raw×(1+rate)），
  50 位精度，逐侧 HALF_UP 两位；
- 坏税率样本/非有限结果 → 整域不可用（ReferenceCalculationError），
  不静默丢行、不伪装成无样本。

窗口（R34-01）：三域统一为含统计结束日的 7 个上海日历日 ``[anchor-6, anchor]``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from app.tax_policy import round_money

# 商城 ReferencePrice 上限口径：整数位最多 14 位。
_MAX_AMOUNT = Decimal("1e14")
_CALCULATION_PRECISION = 50

#: R34-01：含结束日的 7 个上海日历日（不取锚点后交易由调用方 WHERE 保证）。
REFERENCE_WINDOW_DAYS = 7
#: R34-06：数量×时间衰减的半衰期（天）。
DECAY_HALF_LIFE_DAYS = Decimal("7")


class ReferenceCalculationError(Exception):
    """样本税率/数值/分母异常：该域不可用，按 PN 隔离为待估，不 500。"""


@dataclass(frozen=True, slots=True)
class SampleGroup:
    """同一日期、有效税率、原始税基的精确聚合；每张订单只落入一组。

    字段沿 main 侧形状（amount_raw/nominal_ex_count/is_tax_inclusive），
    由看板自己的 SQL 构建，不与商城结构共享定义。
    """

    order_date: date
    rate: Decimal
    amount_raw: Decimal
    qty: Decimal
    price_sum: Decimal
    line_count: int
    order_count: int
    nominal_ex_count: int
    bad_rate_count: int
    nominal_inc_count: int = 0
    is_tax_inclusive: bool = True


def window_bounds(anchor: date) -> tuple[date, date]:
    """三域统一窗口：``[anchor - 6, anchor]`` 闭区间（R34-01）。"""
    return anchor - timedelta(days=REFERENCE_WINDOW_DAYS - 1), anchor


def _decay_factor(order_date: date, end: date) -> Decimal:
    age_days = Decimal((end - order_date).days)
    return Decimal("0.5") ** (age_days / DECAY_HALF_LIFE_DAYS)


def _as_decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validated_amount(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) >= _MAX_AMOUNT:
        raise ReferenceCalculationError
    return value


def weighted_average_price(
    groups: list[SampleGroup],
    *,
    end: date,
) -> tuple[Decimal, Decimal] | None:
    """数量×时间衰减的加权双税均价；无样本返回 None，坏税率/坏分母报错。

    与商城单 PN 参考同口径（数量加权 + 半衰 7 天 + 逐组税侧换算 +
    HALF_UP 两位），但实现与结构完全自持，不随商城内部形状漂移。
    """
    if not groups:
        return None
    if any(group.bad_rate_count for group in groups):
        # 不靠 WHERE 静默丢弃坏行，也不把合法行的部分结果发出去。
        raise ReferenceCalculationError
    with localcontext() as context:
        context.prec = _CALCULATION_PRECISION
        inc_numerator = Decimal(0)
        ex_numerator = Decimal(0)
        denominator = Decimal(0)
        for group in groups:
            rate = _as_decimal(group.rate)
            factor = Decimal(1) + rate
            if not rate.is_finite() or not factor.is_finite() or factor <= 0:
                raise ReferenceCalculationError
            decay = _decay_factor(group.order_date, end)
            weighted_raw = decay * _as_decimal(group.amount_raw)
            denominator += decay * _as_decimal(group.qty)
            # 原始税侧：True 为含税，False/NULL 为未税；先乘衰减再换税侧，
            # 保持 Decimal 运算顺序与最终 HALF_UP 结果稳定。
            if group.is_tax_inclusive:
                inc_numerator += weighted_raw
                ex_numerator += weighted_raw / factor
            else:
                inc_numerator += weighted_raw * factor
                ex_numerator += weighted_raw
        if not denominator.is_finite() or denominator <= 0:
            # 有合格样本时分母在数学上必为正；非有限或下溢 0 是数值异常，
            # 不能伪装成“无样本”的空结果。
            raise ReferenceCalculationError
        inc_average = inc_numerator / denominator
        ex_average = ex_numerator / denominator
        if not inc_average.is_finite() or not ex_average.is_finite():
            raise ReferenceCalculationError
        return (
            _validated_amount(round_money(inc_average)),
            _validated_amount(round_money(ex_average)),
        )
