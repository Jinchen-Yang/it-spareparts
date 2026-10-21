"""维保数据看板三域参考与行级试算估价（R34 / D-0024）。

三域并列（R34-05）：
1. 采购参考（市场）：近 7 天合格交易，数量×时间衰减（半衰 7 天）双税均价；
2. 销售参考（市场）：同窗口同算法；主展示采购优先、缺采购回退销售并标明；
3. 领用已计成本（R34-02）：授权项目内近 7 天实际领用行的**当前已计成本**
   按数量加权（双税原样，不再按交易票面税率二次换算）；原成本证据可能晚于
   锚点，须提示；只作独立视角，不暗替市场参考。

三域不相加、不混均价。市场域过滤/税率/衰减算法由看板自有引擎
（``maintenance_dashboard_reference_prices``）自持——生产隔离红线：不 import
商城 ``resale_price_reference`` 的私有结构（prod/main 形状不兼容）。
**不修改**商城目录接口（``/integrations/resale/...``）与 ``build_price_reference``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.dimensions import DimPart
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.pool_price_analysis import _dq_exists
from app.services.purchase_analytics import _nominal_side_expr, _rate_expr
from app.services.maintenance_dashboard_reference_prices import (
    ReferenceCalculationError,
    SampleGroup,
    weighted_average_price,
    window_bounds,
)
from app.tax_policy import round_money

Domain = Literal["purchase", "sales"]

_COST_NOTE = "领用已计成本参考（可能含估算）；原成本证据可能晚于锚点，不再税转换"


# 单位归一：只做空白与大小写归一，不猜同义单位（箱≠件）。
def _norm_unit(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped.upper() or None


@dataclass(frozen=True, slots=True)
class DomainReference:
    """一个市场交易域的可引用参考（含可用性与样本元数据）。"""

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
class CostReference:
    """领用已计成本参考（R34-02）：数量加权的当前已计单位成本 + 覆盖证据。"""

    available: bool
    unit_cost_inc_tax: Decimal | None = None
    unit_cost_ex_tax: Decimal | None = None
    sample_line_count: int = 0
    issue_qty: Decimal = Decimal("0")
    costed_qty: Decimal = Decimal("0")
    uncosted_qty: Decimal = Decimal("0")
    coverage_pct: float | None = None
    sources: tuple[str, ...] = ()
    latest_issue_date: date | None = None
    oldest_issue_date: date | None = None
    unavailable_reason: str | None = None

    def payload(self) -> dict:
        if not self.available:
            return {"available": False, "unavailable_reason": self.unavailable_reason}
        return {
            "available": True,
            "unit_cost_inc_tax": format(self.unit_cost_inc_tax, ".2f"),
            "unit_cost_ex_tax": format(self.unit_cost_ex_tax, ".2f"),
            "sample_line_count": self.sample_line_count,
            "issue_qty": format(self.issue_qty, ".3f"),
            "costed_qty": format(self.costed_qty, ".3f"),
            "uncosted_qty": format(self.uncosted_qty, ".3f"),
            "coverage_pct": self.coverage_pct,
            "sources": list(self.sources),
            "latest_issue_date": (
                self.latest_issue_date.isoformat() if self.latest_issue_date else None
            ),
            "oldest_issue_date": (
                self.oldest_issue_date.isoformat() if self.oldest_issue_date else None
            ),
            "note": _COST_NOTE,
        }


@dataclass(frozen=True, slots=True)
class PartPriceReferences:
    """一个 PN 的三域参考与市场选择结果（采购优先，销售回退；成本并列）。"""

    part_id: int
    purchase: DomainReference
    sales: DomainReference
    cost: CostReference
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
) -> tuple[dict[int, list[SampleGroup]], dict[int, set[str | None]]]:
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
    grouped: dict[int, list[SampleGroup]] = {}
    units: dict[int, set[str | None]] = {}
    for row in db.execute(statement).all():
        grouped.setdefault(row.part_id, []).append(SampleGroup(
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
) -> tuple[dict[int, list[SampleGroup]], dict[int, set[str | None]]]:
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
    grouped: dict[int, list[SampleGroup]] = {}
    units: dict[int, set[str | None]] = {}
    for row in db.execute(statement).all():
        grouped.setdefault(row.part_id, []).append(SampleGroup(**{
            key: row._mapping[key]
            for key in (
                "order_date", "rate", "amount_raw", "qty", "price_sum",
                "line_count", "order_count", "nominal_inc_count",
                "nominal_ex_count", "bad_rate_count", "is_tax_inclusive",
            )
        }))
        units.setdefault(row.part_id, set()).add(_norm_unit(row.unit))
    return grouped, units


def _cost_reference(
    db: Session,
    part_ids: set[int],
    *,
    anchor: date,
    scope: set[str] | None,
    master_units: dict[int, str | None],
) -> dict[int, CostReference]:
    """R34-02 成本域：授权项目内 [anchor-6, anchor] 有效领用行的已计成本。

    有效口径与看板领用事实一致：映射有效 + confirmed/corrected + 行有效。
    单位成本 = Σcost_amount / Σquantity（已计成本行，双税各侧原样）；
    未计成本行只进数量与覆盖，不进金额。scope 空集合 → 无样本（不扩全量）。
    领用行无单位列——同单位证据只有主数据单位（领用按件主单位记账）；
    主单位缺失 → 无法证明数量与参考同单位，不给成本参考（不猜单位）。
    """
    if not part_ids:
        return {}
    start, end = window_bounds(anchor)
    conditions = [
        MaintenanceSiteIssueLine.part_id.in_(part_ids),
        MaintenanceSiteIssue.status_mapping_state == "mapped",
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
        MaintenanceSiteIssueLine.is_active.is_(True),
        MaintenanceSiteIssue.issue_date >= start,
        MaintenanceSiteIssue.issue_date <= end,
    ]
    if scope is not None:
        conditions.append(
            MaintenanceSiteIssue.project_id.in_(scope) if scope else false()
        )
    costed = MaintenanceSiteIssueLine.cost_amount_inc_tax.is_not(None)
    statement = (
        select(
            MaintenanceSiteIssueLine.part_id.label("part_id"),
            func.sum(MaintenanceSiteIssueLine.quantity).label("issue_qty"),
            func.sum(case(
                (costed, MaintenanceSiteIssueLine.quantity), else_=Decimal("0"),
            )).label("costed_qty"),
            func.sum(case(
                (costed, MaintenanceSiteIssueLine.cost_amount_inc_tax),
                else_=Decimal("0"),
            )).label("amount_inc"),
            func.sum(case(
                (costed, MaintenanceSiteIssueLine.cost_amount_ex_tax),
                else_=Decimal("0"),
            )).label("amount_ex"),
            func.count().filter(costed).label("sample_line_count"),
            func.max(MaintenanceSiteIssue.issue_date).label("latest_issue_date"),
            func.min(MaintenanceSiteIssue.issue_date).label("oldest_issue_date"),
        )
        .select_from(MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(*conditions)
        .group_by(MaintenanceSiteIssueLine.part_id)
    )
    sources_statement = (
        select(
            MaintenanceSiteIssueLine.part_id.label("part_id"),
            MaintenanceSiteIssueLine.cost_source.label("source"),
        )
        .distinct()
        .select_from(MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(*conditions, costed)
        .order_by(MaintenanceSiteIssueLine.part_id)
    )
    sources: dict[int, set[str]] = {}
    for row in db.execute(sources_statement).all():
        if row.source:
            sources.setdefault(row.part_id, set()).add(row.source)
    result: dict[int, CostReference] = {}
    for row in db.execute(statement).all():
        issue_qty = Decimal(row.issue_qty or 0)
        costed_qty = Decimal(row.costed_qty or 0)
        sample_count = row.sample_line_count or 0
        if sample_count == 0 or costed_qty <= 0:
            result[row.part_id] = CostReference(
                available=False, unavailable_reason="cost_not_recorded",
            )
            continue
        if _norm_unit(master_units.get(row.part_id)) is None:
            result[row.part_id] = CostReference(
                available=False, unavailable_reason="unit_missing_master",
            )
            continue
        coverage = (
            float((costed_qty / issue_qty * 100).quantize(Decimal("0.1")))
            if issue_qty > 0 else None
        )
        result[row.part_id] = CostReference(
            available=True,
            # 已计税基原样加权，不再按交易票面税率二次换算（R34-06）。
            unit_cost_inc_tax=round_money(Decimal(row.amount_inc) / costed_qty),
            unit_cost_ex_tax=round_money(Decimal(row.amount_ex) / costed_qty),
            sample_line_count=sample_count,
            issue_qty=issue_qty,
            costed_qty=costed_qty,
            uncosted_qty=issue_qty - costed_qty,
            coverage_pct=coverage,
            sources=tuple(sorted(sources.get(row.part_id, ()))),
            latest_issue_date=row.latest_issue_date,
            oldest_issue_date=row.oldest_issue_date,
        )
    return result


def _domain_reference(
    groups: list[SampleGroup] | None,
    *,
    units: set[str | None] | None,
    master_unit: str | None,
    end: date,
) -> DomainReference:
    """一个市场域的可用性判定 + 自持加权算法（单位校验沿 R33/Q16）。"""
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
        weighted = weighted_average_price(groups, end=end)
    except ReferenceCalculationError:  # 坏税率/数值异常按 PN 隔离为待估，不 503
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
    scope: set[str] | None,
    settings: Settings,
) -> dict[int, PartPriceReferences]:
    """批量三域参考：市场窗口 = 含锚点的 7 个上海日历日（R34-01）。

    采购优先、销售回退（主展示口径）；领用已计成本并列，不参与选择、不相加。
    """
    if not part_ids:
        return {}
    start, end = window_bounds(anchor)
    parts = {
        row.id: row.unit
        for row in db.execute(
            select(DimPart.id, DimPart.unit).where(DimPart.id.in_(part_ids))
        )
    }
    sales_groups, sales_units = _sales_groups(
        db, part_ids, start=start, end=end, settings=settings
    )
    purchase_groups, purchase_units = _purchase_groups(
        db, part_ids, start=start, end=end, settings=settings
    )
    cost = _cost_reference(
        db, part_ids, anchor=anchor, scope=scope, master_units=parts,
    )

    result: dict[int, PartPriceReferences] = {}
    for part_id in part_ids:
        master_unit = parts.get(part_id)
        sales = _domain_reference(
            sales_groups.get(part_id), units=sales_units.get(part_id),
            master_unit=master_unit, end=end,
        )
        purchase = _domain_reference(
            purchase_groups.get(part_id), units=purchase_units.get(part_id),
            master_unit=master_unit, end=end,
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
            cost=cost.get(part_id, CostReference(available=False,
                                                 unavailable_reason="no_samples")),
            chosen=chosen, chosen_reason=reason,
        )
    return result


def value_line(
    qty: Decimal,
    factor: Decimal,
    price: tuple[Decimal, Decimal],
) -> tuple[Decimal, Decimal]:
    """V = qty × 参考价 × 系数（R34：系数=试算比例或成品全值）；HALF_UP 两位。"""
    from decimal import ROUND_HALF_UP

    quant = Decimal("0.01")
    return (
        (qty * price[0] * factor).quantize(quant, rounding=ROUND_HALF_UP),
        (qty * price[1] * factor).quantize(quant, rounding=ROUND_HALF_UP),
    )
