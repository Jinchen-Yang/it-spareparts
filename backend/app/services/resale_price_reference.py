"""商城销售/采购参考均价（D-42 / D-43）。

只读聚合：最近 ``window_days`` 个上海业务日、按订单日期指数时间衰减
（半衰期 ``half_life_days``），同时返回含税与不含税两个加权均价，以及样本行数、
不同订单数、最新/最旧订单日期。

口径要点：
- 销售明细 ``unit_price`` 恒含税；有效税率 = 票面非零税率，否则 13%。
- 含税与未税都在**逐行归一税率后**聚合，最后才按 HALF_UP 保留两位。
- 销售未税侧在票面税率为 NULL/0 时是名义值（按 13% 推算），单独计数。
- 采购单价原始侧与行级名义侧遵循 D-39；只计单头所属批次行，不筛计营收。
- 默认仅已生效、qty>0、单价>0，销售另筛计营收，并排除已确认源数据错误；
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
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.schemas.resale_price_reference import (
    ReferencePrice,
    ReferenceWindow,
    ResalePriceReferenceResponse,
    SalesAverageCalculation,
)
from app.services.pool_price_analysis import _dq_exists
from app.services.purchase_analytics import (
    DEFAULT_TAX_RATE,
    _nominal_side_expr,
    _rate_expr,
)
from app.services.resale_pn_lookup import canonical_part, find_pn_candidates
from app.tax_policy import round_money

_log = logging.getLogger(__name__)

# 商城 ReferencePrice.amount 为 max_digits=18/decimal_places=4：
# 整数位最多 14 位，即绝对值必须小于 1e14。
_MAX_AMOUNT = Decimal("1e14")
_CALCULATION_PRECISION = 50
_DECAY = "exponential_half_life"


class PriceReferenceAmbiguous(Exception):
    """PN 身份无法唯一确定（pending、多目标或坏合并链）。"""


class PriceReferenceUnavailable(Exception):
    """聚合、税率或结果校验失败；调用方应返回固定 503，不输出内部细节。"""


@dataclass(frozen=True)
class _Group:
    """同一日期、有效税率、原始税基的精确聚合；每张订单只落入一组。"""

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
            func.sum(FSalesLine.qty * FSalesLine.unit_price).label("amount_raw"),
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
            amount_raw=row.amount_raw,
            qty=row.qty,
            price_sum=row.price_sum,
            line_count=row.line_count,
            order_count=row.order_count,
            nominal_ex_count=row.nominal_count,
            bad_rate_count=row.bad_rate_count,
        )
        for row in db.execute(statement).all()
    ]


def _purchase_grouped_samples(
    db: Session,
    *,
    part_id: int,
    start: date,
    end: date,
    settings: Settings,
) -> list[_Group]:
    po, pl = FPurchaseOrder, FPurchaseLine
    rate = _rate_expr(po)
    nominal_side = _nominal_side_expr(po)
    is_inc = po.is_tax_inclusive.is_(True)
    bad_rate = and_(
        po.tax_rate.is_not(None), or_(po.tax_rate < 0, po.tax_rate >= 1)
    )
    conditions = [
        pl.part_id == part_id,
        # D-39：旧批次残留行只在读侧排除，不能按行号选最新行替代。
        pl.import_batch_id == po.import_batch_id,
        po.order_date >= start,
        po.order_date <= end,
        po.data_status.in_(settings.resale_price_reference_order_statuses),
        pl.qty > 0,
        (pl.unit_price >= 0 if settings.resale_price_reference_include_zero_prices
         else pl.unit_price > 0),
    ]
    # counts_revenue 是销售行属性，对采购不适用。
    if settings.resale_price_reference_exclude_confirmed_source_error:
        conditions.append(
            ~_dq_exists("purchase", pl.id, ("confirmed_source_error",))
        )
    statement = (
        select(
            po.order_date.label("order_date"),
            rate.label("rate"),
            is_inc.label("is_tax_inclusive"),
            func.sum(pl.qty * pl.unit_price).label("amount_raw"),
            func.sum(pl.qty).label("qty"),
            func.sum(pl.unit_price).label("price_sum"),
            func.count().label("line_count"),
            func.count(func.distinct(po.id)).label("order_count"),
            func.count().filter(nominal_side == "inc").label("nominal_inc_count"),
            func.count().filter(nominal_side == "ex").label("nominal_ex_count"),
            func.count().filter(bad_rate).label("bad_rate_count"),
        )
        .select_from(pl)
        .join(po, pl.order_id == po.id)
        .where(*conditions)
        .group_by(po.order_date, rate, is_inc)
    )
    return [_Group(**row._mapping) for row in db.execute(statement).all()]


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
    """按时间衰减返回 (含税均价, 未税均价)；无样本返回 None，坏分母报错。"""
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
                weighted_raw = decay * _as_decimal(group.amount_raw)
                denominator += decay * _as_decimal(group.qty)
            else:
                weighted_raw = decay * _as_decimal(group.price_sum)
                denominator += decay * Decimal(group.line_count)
            # D-39 行级原始侧：True 为含税，False/NULL 为未税。
            # 沿用 _line_amount_exprs 的换算语义，但不逐行 round；销售仍先
            # 乘 decay 再除税，保持原 Decimal 运算顺序与最终 HALF_UP 结果。
            if group.is_tax_inclusive:
                inc_numerator += weighted_raw
                ex_numerator += weighted_raw / factor
            else:
                inc_numerator += weighted_raw * factor
                ex_numerator += weighted_raw
        if not denominator.is_finite() or denominator <= 0:
            # 有合格样本时分母在数学上必为正；出现非有限或下溢为 0 是数值/配置
            # 异常，不能伪装成“无样本”的 200 空结果。
            raise PriceReferenceUnavailable
        inc_average = inc_numerator / denominator
        ex_average = ex_numerator / denominator
        if not inc_average.is_finite() or not ex_average.is_finite():
            raise PriceReferenceUnavailable
        return (
            _validated_amount(round_money(inc_average)),
            _validated_amount(round_money(ex_average)),
        )


def _price_entries(
    groups: list[_Group],
    weighted: tuple[Decimal, Decimal] | None,
    *,
    kind: Literal["sale", "purchase"],
    window: ReferenceWindow,
) -> list[ReferencePrice]:
    if weighted is None:
        return []
    latest = max(group.order_date for group in groups)
    oldest = min(group.order_date for group in groups)
    common = {
        "kind": kind,
        "currency": "CNY",
        "occurred_at": datetime.combine(latest, time.min, tzinfo=BUSINESS_TZ),
        "source": "itsp销售均价" if kind == "sale" else "itsp采购均价",
        "sample_count": sum(group.line_count for group in groups),
        "order_count": sum(group.order_count for group in groups),
        "window": window,
        # 兼容商城既有字段名；purchase 时语义为采购订单日期。
        "latest_sale_date": latest,
        "oldest_sale_date": oldest,
    }
    return [
        ReferencePrice(
            amount=weighted[0],
            tax_basis="inc_tax",
            nominal_sample_count=sum(group.nominal_inc_count for group in groups),
            **common,
        ),
        ReferencePrice(
            amount=weighted[1],
            tax_basis="ex_tax",
            nominal_sample_count=sum(group.nominal_ex_count for group in groups),
            **common,
        ),
    ]


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
        purchase_groups = (
            _purchase_grouped_samples(
                db, part_id=part_id, start=start, end=end, settings=settings
            )
            if part_id is not None
            else []
        )
        purchase_weighted = _weighted_amounts(
            purchase_groups,
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
    latest = max((group.order_date for group in groups), default=None)
    oldest = min((group.order_date for group in groups), default=None)

    prices = _price_entries(groups, weighted, kind="sale", window=window)
    prices.extend(_price_entries(
        purchase_groups, purchase_weighted, kind="purchase", window=window
    ))

    # 已上线商城校验此处与 sale 条目一致，采购样本不得混入。
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
