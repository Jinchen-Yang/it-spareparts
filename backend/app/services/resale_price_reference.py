"""商城销售参考均价（2026-10-19 金臣口径）。

只读聚合：最近 ``window_days`` 个上海业务日、按订单日期指数时间衰减
（半衰期 ``half_life_days``），同时返回含税与不含税两个加权均价，以及样本行数、
不同订单数、最新/最旧销售日期。

口径要点：
- 销售明细 ``unit_price`` 恒含税；有效税率 = 票面非零税率，否则 13%。
- 含税与未税都在**逐行归一税率后**聚合，最后才按 HALF_UP 保留两位。
- 未税侧在票面税率为 NULL/0 时是名义值（按 13% 推算），单独计数。
- 默认仅已生效、计营收、qty>0、单价>0，并排除已确认源数据错误；
  不做离群裁剪，不使用固定 13% 的利润链金额。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.business_time import BUSINESS_TZ, business_today
from app.config import Settings
from app.models.dimensions import DimPart
from app.models.sales import FSalesLine, FSalesOrder
from app.schemas.resale_price_reference import (
    ReferencePrice,
    ReferenceWindow,
    ResalePriceReferenceResponse,
    SalesAverageCalculation,
)
from app.services.pool_price_analysis import _dq_exists
from app.services.purchase_analytics import DEFAULT_TAX_RATE, _rate_expr
from app.services.resale_pn_lookup import canonical_part, find_pn_candidates
from app.tax_policy import round_money

_log = logging.getLogger(__name__)

_MAX_AMOUNT = Decimal("1e16")  # 商城 amount 为 max_digits=18/decimal_places=4 内的金额
_CALCULATION_PRECISION = 50
_DECAY = "exponential_half_life"


class PriceReferenceAmbiguous(Exception):
    """PN 身份无法唯一确定（pending、多目标或坏合并链）。"""


class PriceReferenceUnavailable(Exception):
    """聚合、税率或结果校验失败；调用方应返回固定 503，不输出内部细节。"""


@dataclass(frozen=True)
class _Group:
    """同一订单日期、同一有效税率下的聚合行（最多 366 个日期 × 少量税率）。"""

    order_date: date
    rate: Decimal
    amount_inc: Decimal
    qty: Decimal
    price_sum: Decimal
    line_count: int
    order_count: int
    nominal_count: int
    bad_rate_count: int


def _resolve_part_id(db: Session, pn_raw: str) -> int | None:
    """查找副本先 strip；响应回显由路由保留请求原文。"""
    candidates, pending = find_pn_candidates(db, pn_raw.strip())
    if pending:
        raise PriceReferenceAmbiguous
    resolved: dict[int, DimPart] = {}
    for part, _kind in candidates.values():
        target, _merged = canonical_part(db, part)
        if target is None:
            raise PriceReferenceAmbiguous
        resolved[target.id] = target
    if len(resolved) > 1:
        raise PriceReferenceAmbiguous
    return next(iter(resolved), None)


def _grouped_samples(
    db: Session,
    *,
    part_id: int,
    start: date,
    end: date,
    settings: Settings,
) -> list[_Group]:
    rate = _rate_expr(FSalesOrder)
    nominal = func.coalesce(FSalesOrder.tax_rate, 0) == 0
    bad_rate = and_(
        FSalesOrder.tax_rate.is_not(None),
        or_(FSalesOrder.tax_rate < 0, FSalesOrder.tax_rate >= 1),
    )
    priced = (
        FSalesLine.unit_price >= 0
        if settings.resale_price_reference_include_zero_prices
        else FSalesLine.unit_price > 0
    )
    conditions = [
        FSalesLine.part_id == part_id,
        FSalesOrder.order_date >= start,
        FSalesOrder.order_date <= end,
        FSalesOrder.data_status.in_(
            settings.resale_price_reference_order_statuses
        ),
        FSalesLine.qty > 0,
        priced,
    ]
    if settings.resale_price_reference_counts_revenue_only:
        conditions.append(FSalesLine.counts_revenue.is_(True))
    if settings.resale_price_reference_exclude_confirmed_source_error:
        conditions.append(
            ~_dq_exists("sales", FSalesLine.id, ("confirmed_source_error",))
        )
    statement = (
        select(
            FSalesOrder.order_date.label("order_date"),
            rate.label("rate"),
            func.sum(FSalesLine.qty * FSalesLine.unit_price).label("amount_inc"),
            func.sum(FSalesLine.qty).label("qty"),
            func.sum(FSalesLine.unit_price).label("price_sum"),
            func.count().label("line_count"),
            func.count(func.distinct(FSalesOrder.id)).label("order_count"),
            func.count().filter(nominal).label("nominal_count"),
            func.count().filter(bad_rate).label("bad_rate_count"),
        )
        .select_from(FSalesLine)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(*conditions)
        .group_by(FSalesOrder.order_date, rate)
    )
    return [
        _Group(
            order_date=row.order_date,
            rate=row.rate,
            amount_inc=row.amount_inc,
            qty=row.qty,
            price_sum=row.price_sum,
            line_count=row.line_count,
            order_count=row.order_count,
            nominal_count=row.nominal_count,
            bad_rate_count=row.bad_rate_count,
        )
        for row in db.execute(statement).all()
    ]


def _decay_factor(order_date: date, end: date, half_life_days: Decimal) -> Decimal:
    age_days = Decimal((end - order_date).days)
    return Decimal("0.5") ** (age_days / half_life_days)


def _as_decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validated_amount(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) >= _MAX_AMOUNT:
        raise PriceReferenceUnavailable
    return value


def _weighted_amounts(
    groups: list[_Group],
    *,
    end: date,
    half_life_days: Decimal,
    weighting: Literal["qty_weighted", "line_mean"],
) -> tuple[Decimal, Decimal] | None:
    """按时间衰减返回 (含税均价, 未税均价)；无样本或分母为 0 返回 None。"""
    if not groups:
        return None
    bad_rates = sum(group.bad_rate_count for group in groups)
    if bad_rates:
        # 不靠 WHERE 静默丢弃坏行，也不把合法行的部分结果发出去。
        raise PriceReferenceUnavailable
    with localcontext() as context:
        context.prec = _CALCULATION_PRECISION
        inc_numerator = Decimal(0)
        ex_numerator = Decimal(0)
        denominator = Decimal(0)
        for group in groups:
            rate = _as_decimal(group.rate)
            factor = Decimal(1) + rate
            if not rate.is_finite() or not factor.is_finite() or factor <= 0:
                raise PriceReferenceUnavailable
            decay = _decay_factor(group.order_date, end, half_life_days)
            if weighting == "qty_weighted":
                inc_numerator += decay * _as_decimal(group.amount_inc)
                ex_numerator += decay * _as_decimal(group.amount_inc) / factor
                denominator += decay * _as_decimal(group.qty)
            else:
                inc_numerator += decay * _as_decimal(group.price_sum)
                ex_numerator += decay * _as_decimal(group.price_sum) / factor
                denominator += decay * Decimal(group.line_count)
        if not denominator.is_finite() or denominator <= 0:
            # 有合格样本时分母在数学上必为正；出现非有限或下溢为 0 是数值/配置
            # 异常，不能伪装成“无销售数据”的 200 空结果。
            raise PriceReferenceUnavailable
        inc_average = inc_numerator / denominator
        ex_average = ex_numerator / denominator
        if not inc_average.is_finite() or not ex_average.is_finite():
            raise PriceReferenceUnavailable
        return (
            _validated_amount(round_money(inc_average)),
            _validated_amount(round_money(ex_average)),
        )


def build_price_reference(
    db: Session,
    pn_raw: str,
    settings: Settings,
    *,
    today: date | None = None,
) -> ResalePriceReferenceResponse:
    """构建参考价响应；成功（含空样本）由路由统一加 no-store。"""
    end = today or business_today()
    start = end - timedelta(
        days=settings.resale_price_reference_window_days - 1
    )
    window = ReferenceWindow(start=start, end=end)
    weighting = settings.resale_price_reference_weighting
    method = (
        "time_decay_qty_weighted"
        if weighting == "qty_weighted"
        else "time_decay_line_weighted"
    )
    try:
        part_id = _resolve_part_id(db, pn_raw)
        groups = (
            _grouped_samples(
                db,
                part_id=part_id,
                start=start,
                end=end,
                settings=settings,
            )
            if part_id is not None
            else []
        )
        weighted = _weighted_amounts(
            groups,
            end=end,
            half_life_days=settings.resale_price_reference_half_life_days,
            weighting=weighting,
        )
    except (PriceReferenceAmbiguous, PriceReferenceUnavailable):
        raise
    except SQLAlchemyError:
        _log.warning("Resale price reference aggregation unavailable")
        raise PriceReferenceUnavailable from None
    except (ArithmeticError, ValueError):
        _log.warning("Resale price reference arithmetic unavailable")
        raise PriceReferenceUnavailable from None

    sample_count = sum(group.line_count for group in groups)
    order_count = sum(group.order_count for group in groups)
    nominal_count = sum(group.nominal_count for group in groups)
    latest = max((group.order_date for group in groups), default=None)
    oldest = min((group.order_date for group in groups), default=None)

    prices: list[ReferencePrice] = []
    if weighted is not None and latest is not None and oldest is not None:
        inc_average, ex_average = weighted
        occurred_at = datetime.combine(latest, time.min, tzinfo=BUSINESS_TZ)
        common = {
            "kind": "sale",
            "currency": "CNY",
            "occurred_at": occurred_at,
            "source": "itsp销售均价",
            "sample_count": sample_count,
            "order_count": order_count,
            "window": window,
            "latest_sale_date": latest,
            "oldest_sale_date": oldest,
        }
        prices = [
            ReferencePrice(
                amount=inc_average,
                tax_basis="inc_tax",
                nominal_sample_count=0,
                **common,
            ),
            ReferencePrice(
                amount=ex_average,
                tax_basis="ex_tax",
                # 只有按 13% 推算的未税侧是名义值；含税侧恒为真实值。
                nominal_sample_count=nominal_count,
                **common,
            ),
        ]

    calculation = SalesAverageCalculation(
        method=method,
        decay=_DECAY,
        window_days=settings.resale_price_reference_window_days,
        half_life_days=settings.resale_price_reference_half_life_days,
        date_basis="order_date",
        timezone="Asia/Shanghai",
        window=window,
        order_statuses=list(settings.resale_price_reference_order_statuses),
        counts_revenue_only=settings.resale_price_reference_counts_revenue_only,
        positive_qty_only=True,
        include_zero_prices=settings.resale_price_reference_include_zero_prices,
        exclude_confirmed_source_error=(
            settings.resale_price_reference_exclude_confirmed_source_error
        ),
        sales_unit_price_basis="inc_tax",
        tax_policy="order_rate_or_13pct",
        fallback_tax_rate=DEFAULT_TAX_RATE,
        currency="CNY",
        sample_count=sample_count,
        order_count=order_count,
        latest_sale_date=latest,
        oldest_sale_date=oldest,
    )
    return ResalePriceReferenceResponse(
        pn=pn_raw,
        prices=prices,
        reference_only=True,
        calculation=calculation,
    )
