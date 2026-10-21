"""维保数据看板内部批量交易参考与坏件行估价（R33 / 设计 §6.4）。

复用 ``resale_price_reference``（D-42/D-43）的过滤、税率、时间衰减与 Decimal
算法：分组样本结构 ``_Group`` 与加权函数 ``_weighted_amounts`` 直接复用，保证
与单 PN 参考接口同口径；**不修改**商城目录接口（``/integrations/resale/...``）
与 ``build_price_reference`` 的对外输出（由既有测试 + 本模块对照测试看护）。

新增能力只有两块（均为看板内部，无新对外契约）：
1. 批量：一次 SQL 按 ``part_id`` 分组取两域样本，不逐 PN 循环调用单 PN 函数；
2. 单位校验（R33 第 7 条 / Q16）：交易样本 ``unit`` 与 ``dim_part.unit`` 不一致、
   缺失或混合时该域不可用，两域都不可用标「待估」（``unit_mismatch`` 等），
   不做箱/件换算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.pool_price_analysis import _dq_exists
from app.services.purchase_analytics import _nominal_side_expr, _rate_expr
from app.services.resale_price_reference import _Group, _weighted_amounts

Domain = Literal["purchase", "sales"]

# 单位归一：只做空白与大小写归一，不猜同义单位（箱≠件）。
def _norm_unit(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped.upper() or None


@dataclass(frozen=True, slots=True)
class DomainReference:
    """一个交易域的可引用参考（含可用性与样本元数据）。"""

    available: bool
    inc_tax: Decimal | None = None
    ex_tax: Decimal | None = None
    sample_count: int = 0
    order_count: int = 0
    latest_order_date: date | None = None
    oldest_order_date: date | None = None
    low_confidence: bool = False
    unavailable_reason: str | None = None

    def payload(self) -> dict:
        if not self.available:
            return {"available": False, "unavailable_reason": self.unavailable_reason}
        return {
            "available": True,
            "inc_tax": format(self.inc_tax, ".2f"),
            "ex_tax": format(self.ex_tax, ".2f"),
            "sample_count": self.sample_count,
            "order_count": self.order_count,
            "latest_order_date": (
                self.latest_order_date.isoformat() if self.latest_order_date else None
            ),
            "oldest_order_date": (
                self.oldest_order_date.isoformat() if self.oldest_order_date else None
            ),
            "low_confidence": self.low_confidence,
        }


@dataclass(frozen=True, slots=True)
class PartPriceReferences:
    """一个 PN 的双域参考与选择结果（采购优先，销售回退）。"""

    part_id: int
    purchase: DomainReference
    sales: DomainReference
    chosen: Domain | None
    chosen_reason: str | None = None

    @property
    def price(self) -> tuple[Decimal, Decimal] | None:
        domain = self.purchase if self.chosen == "purchase" else (
            self.sales if self.chosen == "sales" else None
        )
        if domain is None or not domain.available:
            return None
        return domain.inc_tax, domain.ex_tax


def _unavailable(reason: str) -> DomainReference:
    return DomainReference(available=False, unavailable_reason=reason)


def _sales_groups(
    db: Session, part_ids: set[int], *, start: date, end: date, settings: Settings
) -> tuple[dict[int, list[_Group]], dict[int, set[str | None]]]:
    """按 PN 分组的销售样本 + 各 PN 的合格样本单位集合。"""
    if not part_ids:
        return {}, {}
    so, sl = FSalesOrder, FSalesLine
    rate = _rate_expr(so)
    nominal = func.coalesce(so.tax_rate, 0) == 0
    bad_rate = and_(
        so.tax_rate.is_not(None), or_(so.tax_rate < 0, so.tax_rate >= 1)
    )
    priced = (
        sl.unit_price >= 0
        if settings.resale_price_reference_include_zero_prices
        else sl.unit_price > 0
    )
    conditions = [
        sl.part_id.in_(part_ids),
        so.order_date >= start,
        so.order_date <= end,
        so.data_status.in_(settings.resale_price_reference_order_statuses),
        sl.qty > 0,
        priced,
    ]
    if settings.resale_price_reference_counts_revenue_only:
        conditions.append(sl.counts_revenue.is_(True))
    if settings.resale_price_reference_exclude_confirmed_source_error:
        conditions.append(~_dq_exists("sales", sl.id, ("confirmed_source_error",)))
    statement = (
        select(
            sl.part_id.label("part_id"),
            sl.unit.label("unit"),
            so.order_date.label("order_date"),
            rate.label("rate"),
            func.sum(sl.qty * sl.unit_price).label("amount_raw"),
            func.sum(sl.qty).label("qty"),
            func.sum(sl.unit_price).label("price_sum"),
            func.count().label("line_count"),
            func.count(func.distinct(so.id)).label("order_count"),
            func.count().filter(nominal).label("nominal_count"),
            func.count().filter(bad_rate).label("bad_rate_count"),
        )
        .select_from(sl)
        .join(so, sl.order_id == so.id)
        .where(*conditions)
        .group_by(sl.part_id, sl.unit, so.order_date, rate)
        .order_by(sl.part_id)
    )
    grouped: dict[int, list[_Group]] = {}
    units: dict[int, set[str | None]] = {}
    for row in db.execute(statement).all():
        grouped.setdefault(row.part_id, []).append(_Group(
            order_date=row.order_date,
            rate=row.rate,
            amount_raw=row.amount_raw,
            qty=row.qty,
            price_sum=row.price_sum,
            line_count=row.line_count,
            order_count=row.order_count,
            nominal_ex_count=row.nominal_count,
            bad_rate_count=row.bad_rate_count,
        ))
        units.setdefault(row.part_id, set()).add(_norm_unit(row.unit))
    return grouped, units


def _purchase_groups(
    db: Session, part_ids: set[int], *, start: date, end: date, settings: Settings
) -> tuple[dict[int, list[_Group]], dict[int, set[str | None]]]:
    if not part_ids:
        return {}, {}
    po, pl = FPurchaseOrder, FPurchaseLine
    rate = _rate_expr(po)
    nominal_side = _nominal_side_expr(po)
    is_inc = po.is_tax_inclusive.is_(True)
    bad_rate = and_(
        po.tax_rate.is_not(None), or_(po.tax_rate < 0, po.tax_rate >= 1)
    )
    priced = (
        pl.unit_price >= 0
        if settings.resale_price_reference_include_zero_prices
        else pl.unit_price > 0
    )
    conditions = [
        pl.part_id.in_(part_ids),
        # D-39：只计单头当前批次行。
        pl.import_batch_id == po.import_batch_id,
        po.order_date >= start,
        po.order_date <= end,
        po.data_status.in_(settings.resale_price_reference_order_statuses),
        pl.qty > 0,
        priced,
    ]
    if settings.resale_price_reference_exclude_confirmed_source_error:
        conditions.append(~_dq_exists("purchase", pl.id, ("confirmed_source_error",)))
    statement = (
        select(
            pl.part_id.label("part_id"),
            pl.unit.label("unit"),
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
        .group_by(pl.part_id, pl.unit, po.order_date, rate, is_inc)
        .order_by(pl.part_id)
    )
    grouped: dict[int, list[_Group]] = {}
    units: dict[int, set[str | None]] = {}
    for row in db.execute(statement).all():
        grouped.setdefault(row.part_id, []).append(_Group(**{
            key: row._mapping[key]
            for key in (
                "order_date", "rate", "amount_raw", "qty", "price_sum",
                "line_count", "order_count", "nominal_inc_count",
                "nominal_ex_count", "bad_rate_count", "is_tax_inclusive",
            )
        }))
        units.setdefault(row.part_id, set()).add(_norm_unit(row.unit))
    return grouped, units


def _domain_reference(
    groups: list[_Group] | None,
    *,
    units: set[str | None] | None,
    master_unit: str | None,
    end: date,
    settings: Settings,
) -> DomainReference:
    """一个域的可用性判定 + 复用 resale 加权算法。"""
    from app.services.maintenance_dashboard_valuation_policy import (
        LOW_SAMPLE_ORDER_COUNT,
    )

    if not groups:
        return _unavailable("no_samples")
    unit_set = units or set()
    normalized_master = _norm_unit(master_unit)
    if normalized_master is None:
        return _unavailable("unit_missing_master")
    if None in unit_set:
        return _unavailable("unit_missing_sample")
    if len(unit_set) > 1:
        return _unavailable("unit_mixed_samples")
    if next(iter(unit_set)) != normalized_master:
        return _unavailable("unit_mismatch")
    try:
        weighted = _weighted_amounts(
            groups,
            end=end,
            half_life_days=settings.resale_price_reference_half_life_days,
            weighting=settings.resale_price_reference_weighting,
        )
    except Exception:  # noqa: BLE001 —— 坏税率/数值异常按 PN 隔离为待估，不 503
        return _unavailable("price_unavailable")
    if weighted is None:
        return _unavailable("no_samples")
    sample_count = sum(group.line_count for group in groups)
    order_count = sum(group.order_count for group in groups)
    return DomainReference(
        available=True,
        inc_tax=weighted[0],
        ex_tax=weighted[1],
        sample_count=sample_count,
        order_count=order_count,
        latest_order_date=max(group.order_date for group in groups),
        oldest_order_date=min(group.order_date for group in groups),
        low_confidence=order_count < LOW_SAMPLE_ORDER_COUNT,
    )


def batch_price_references(
    db: Session,
    part_ids: set[int],
    *,
    anchor: date,
    settings: Settings,
) -> dict[int, PartPriceReferences]:
    """批量双域参考：采购优先、销售回退；窗口沿 resale 配置（默认 30 天）。

    锚点 = 统计结束日（R33 第 7 条 / Q6）：窗口 ``[anchor - window_days + 1, anchor]``
    闭区间，不取未来交易。
    """
    if not part_ids:
        return {}
    start = anchor - timedelta(
        days=settings.resale_price_reference_window_days - 1
    )
    parts = {
        row.id: row.unit
        for row in db.execute(
            select(DimPart.id, DimPart.unit).where(DimPart.id.in_(part_ids))
        )
    }
    sales_groups, sales_units = _sales_groups(
        db, part_ids, start=start, end=anchor, settings=settings
    )
    purchase_groups, purchase_units = _purchase_groups(
        db, part_ids, start=start, end=anchor, settings=settings
    )

    result: dict[int, PartPriceReferences] = {}
    for part_id in part_ids:
        master_unit = parts.get(part_id)
        sales = _domain_reference(
            sales_groups.get(part_id), units=sales_units.get(part_id),
            master_unit=master_unit, end=anchor, settings=settings,
        )
        purchase = _domain_reference(
            purchase_groups.get(part_id), units=purchase_units.get(part_id),
            master_unit=master_unit, end=anchor, settings=settings,
        )
        if purchase.available:
            chosen: Domain | None = "purchase"
            reason = None
        elif sales.available:
            chosen = "sales"
            reason = "purchase_fallback"
        else:
            chosen = None
            reasons = sorted({
                purchase.unavailable_reason, sales.unavailable_reason,
            } - {"no_samples"})
            reason = ",".join(reasons) if reasons else "no_samples"
        result[part_id] = PartPriceReferences(
            part_id=part_id, purchase=purchase, sales=sales,
            chosen=chosen, chosen_reason=reason,
        )
    return result


@dataclass(slots=True)
class LineValuation:
    """一条返件实收行的估价结果（行金额四舍五入两位后求和）。"""

    line_value_inc: Decimal | None
    line_value_ex: Decimal | None
    pending_reason: str | None


def value_line(
    qty: Decimal,
    factor: Decimal,
    price: tuple[Decimal, Decimal],
) -> tuple[Decimal, Decimal]:
    """V = qty × 参考价 × 系数；行金额统一 HALF_UP 两位（设计 §6.3）。"""
    from decimal import ROUND_HALF_UP

    quant = Decimal("0.01")
    return (
        (qty * price[0] * factor).quantize(quant, rounding=ROUND_HALF_UP),
        (qty * price[1] * factor).quantize(quant, rounding=ROUND_HALF_UP),
    )
