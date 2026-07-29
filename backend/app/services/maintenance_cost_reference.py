"""维保缺失成本的历史参考深模块。

公开接口只有 :func:`build_reference_index` 与不可变的 :class:`CostReference`。
模块一次性批量读取有效互通池、采购和销售事实，在内存按自然月建立索引；逐条维保
行解析不再访问数据库，避免重算随行数产生 N+1。

这里严格只处理 ``direct -> window -> month_avg`` 最终仍未命中的行，并按
``pool_purchase -> pool_sales -> purchase_history -> sales_history`` 解析。
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Literal

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app import config, tax_policy
from app.models.data_quality import FactDataQualityIssue
from app.models.inventory import PartPool, PartPoolMember
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.query_filters import active_orders

ReferenceSide = Literal["purchase", "sales"]
ReferenceSource = Literal[
    "pool_purchase",
    "pool_sales",
    "purchase_history",
    "sales_history",
]

_ZERO = Decimal("0")
_MONEY_MAX = Decimal(10) ** 12


@dataclass(frozen=True, slots=True)
class NormalizedCost:
    """一条原始价格事实归一后的双税口径；计算中不提前舍入。"""

    unit_cost_inc_tax: Decimal
    unit_cost_ex_tax: Decimal
    legacy_basis: Literal["inc", "ex"]
    tax_rate_estimated: bool
    inc_tax_estimated: bool
    ex_tax_estimated: bool


@dataclass(frozen=True, slots=True)
class CostSample:
    """历史事实最小投影；不携带客户、供应商等无关/敏感信息。"""

    side: ReferenceSide
    part_id: int
    occurred_on: date | None
    qty: Decimal
    unit_price: Decimal
    tax_rate: Decimal | None
    is_tax_inclusive: bool | None = None


@dataclass(frozen=True, slots=True)
class CostReference:
    """一次缺失成本补价的完整、可审计结果。"""

    source: ReferenceSource
    unit_cost_inc_tax: Decimal
    unit_cost_ex_tax: Decimal
    legacy_unit_cost: Decimal
    legacy_tax_basis: Literal["inc", "ex"]
    confidence: Literal["low"]
    reference_side: ReferenceSide
    pool_group_id: int | None
    pool_version: int | None
    sample_count: int
    reference_from_date: date
    reference_to_date: date
    reference_latest_date: date
    price_month: str
    trace_months: int
    anomaly_flags: tuple[str, ...]


def _effective_rate(rate: Decimal | None) -> tuple[Decimal, bool]:
    """业务计算始终使用统一 13%；原始税率只随样本保留作审计。"""
    del rate
    return tax_policy.TAX_RATE, False


def normalize_cost_sample(sample: CostSample) -> NormalizedCost:
    """按统一 13% 把原始单价归一成双口径。

    采购行单价跟随 ``is_tax_inclusive``；未知口径沿用现有维保规则视作未税。
    销售行 ``unit_price`` 的模型契约恒为含税原值。原订单税率不参与业务计算，
    但仍保留在 ``CostSample`` 供审计。
    """
    _rate, estimated = _effective_rate(sample.tax_rate)
    factor = tax_policy.TAX_FACTOR
    price = Decimal(sample.unit_price)
    if (
        not price.is_finite()
        or price <= _ZERO
        or price >= _MONEY_MAX
    ):
        raise ValueError("cost sample price must be finite and positive")
    if sample.side == "purchase":
        if sample.is_tax_inclusive is True:
            inc, ex = price, price / factor
            legacy_basis: Literal["inc", "ex"] = "inc"
            inc_estimated, ex_estimated = False, estimated
        else:
            inc, ex = price * factor, price
            legacy_basis = "ex"
            inc_estimated, ex_estimated = estimated, False
    else:
        inc, ex = price, price / factor
        inc_estimated, ex_estimated = False, estimated
        legacy_basis = "inc"
    return NormalizedCost(
        unit_cost_inc_tax=inc,
        unit_cost_ex_tax=ex,
        legacy_basis=legacy_basis,
        tax_rate_estimated=estimated,
        inc_tax_estimated=inc_estimated,
        ex_tax_estimated=ex_estimated,
    )


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _month_distance(later: date, earlier: date) -> int:
    return (later.year - earlier.year) * 12 + later.month - earlier.month


def _shift_months(value: date, months: int) -> date:
    """按自然月移动并钳制月末；如 5 月 31 日往前 3 月得到 2 月 28/29 日。"""
    absolute_month = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(absolute_month, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _pick_legacy(
    totals: dict[str, tuple[Decimal, Decimal]],
) -> tuple[Decimal, Literal["inc", "ex"]]:
    preference = (
        ("inc", "ex")
        if config.MAINT_TAX_PREFERENCE == "inc_first"
        else ("ex", "inc")
    )
    for basis in preference:
        amount, qty = totals.get(basis, (_ZERO, _ZERO))
        if qty > _ZERO:
            return tax_policy.round_money(amount / qty), basis
    raise ValueError("reference month has no positive legacy sample")


def summarize_samples(
    samples: Iterable[CostSample],
) -> tuple[
    Decimal,
    Decimal,
    Decimal,
    Literal["inc", "ex"],
    int,
    date | None,
    date | None,
    bool,
    bool,
    bool,
    bool,
]:
    """把同一参考月样本汇总成双口径数量加权均价和 legacy 兼容值。"""
    inc_amount = ex_amount = total_qty = _ZERO
    legacy_amount = {"inc": _ZERO, "ex": _ZERO}
    legacy_qty = {"inc": _ZERO, "ex": _ZERO}
    count = 0
    first: date | None = None
    latest: date | None = None
    estimated = False
    inc_estimated = False
    ex_estimated = False
    reference_date_missing = False
    for sample in samples:
        qty = Decimal(sample.qty)
        if (
            not qty.is_finite()
            or qty <= _ZERO
            or qty >= _MONEY_MAX
        ):
            continue
        try:
            normalized = normalize_cost_sample(sample)
        except (ArithmeticError, ValueError):
            continue
        inc_amount += normalized.unit_cost_inc_tax * qty
        ex_amount += normalized.unit_cost_ex_tax * qty
        total_qty += qty
        legacy_amount[normalized.legacy_basis] += Decimal(sample.unit_price) * qty
        legacy_qty[normalized.legacy_basis] += qty
        count += 1
        if sample.occurred_on is not None:
            first = (
                sample.occurred_on
                if first is None
                else min(first, sample.occurred_on)
            )
            latest = (
                sample.occurred_on
                if latest is None
                else max(latest, sample.occurred_on)
            )
        else:
            reference_date_missing = True
        estimated = estimated or normalized.tax_rate_estimated
        inc_estimated = inc_estimated or normalized.inc_tax_estimated
        ex_estimated = ex_estimated or normalized.ex_tax_estimated
    if total_qty <= _ZERO:
        raise ValueError("reference month has no positive sample")
    legacy, legacy_basis = _pick_legacy(
        {
            basis: (legacy_amount[basis], legacy_qty[basis])
            for basis in ("inc", "ex")
        }
    )
    return (
        tax_policy.round_money(inc_amount / total_qty),
        tax_policy.round_money(ex_amount / total_qty),
        legacy,
        legacy_basis,
        count,
        first,
        latest,
        estimated,
        inc_estimated,
        ex_estimated,
        reference_date_missing,
    )


class CostReferenceIndex:
    """数据库无关的只读月份索引；resolve 调用恒不发 SQL。"""

    def __init__(
        self,
        *,
        target_pool: dict[int, tuple[int, int]],
        pool_members: dict[int, frozenset[int]],
        purchases: dict[int, dict[str, tuple[CostSample, ...]]],
        sales: dict[int, dict[str, tuple[CostSample, ...]]],
    ) -> None:
        self._target_pool = target_pool
        self._pool_members = pool_members
        self._purchases = purchases
        self._sales = sales
        self._cache: dict[tuple[int, date], CostReference | None] = {}
        self._monthly_view_cache: dict[
            tuple[ReferenceSide, frozenset[int]],
            dict[str, tuple[CostSample, ...]],
        ] = {}
        self._month_order_cache: dict[
            tuple[ReferenceSide, frozenset[int]],
            tuple[str, ...],
        ] = {}
        self._reference_cache: dict[
            tuple[
                ReferenceSource,
                ReferenceSide,
                frozenset[int],
                date,
                int | None,
                int | None,
            ],
            CostReference | None,
        ] = {}

    @staticmethod
    def _eligible_month(
        monthly: dict[str, tuple[CostSample, ...]],
        *,
        as_of: date,
        allowed_parts: frozenset[int],
        ordered_months: tuple[str, ...] | None = None,
    ) -> tuple[str, tuple[CostSample, ...]] | None:
        as_of_month = _month_key(as_of)
        earliest = _shift_months(as_of, -config.MAINT_TRACE_MAX_MONTHS)
        for month in ordered_months or tuple(sorted(monthly, reverse=True)):
            if month > as_of_month:
                continue
            eligible = tuple(
                sample
                for sample in monthly[month]
                if (
                    sample.part_id in allowed_parts
                    and earliest <= sample.occurred_on <= as_of
                )
            )
            if eligible:
                return month, eligible
        return None

    def _reference(
        self,
        *,
        source: ReferenceSource,
        side: ReferenceSide,
        part_ids: frozenset[int],
        as_of: date,
        pool_group_id: int | None,
        pool_version: int | None,
    ) -> CostReference | None:
        reference_key = (
            source,
            side,
            part_ids,
            as_of,
            pool_group_id,
            pool_version,
        )
        if reference_key in self._reference_cache:
            return self._reference_cache[reference_key]
        source_data = self._purchases if side == "purchase" else self._sales
        view_key = (side, part_ids)
        frozen_monthly = self._monthly_view_cache.get(view_key)
        if frozen_monthly is None:
            # 同一池可能被多条维保行、多个目标 PN 反复解析；候选月份视图只组装一次。
            monthly: dict[str, list[CostSample]] = defaultdict(list)
            for part_id in part_ids:
                for month, samples in source_data.get(part_id, {}).items():
                    monthly[month].extend(samples)
            frozen_monthly = {
                month: tuple(samples)
                for month, samples in monthly.items()
            }
            self._monthly_view_cache[view_key] = frozen_monthly
            self._month_order_cache[view_key] = tuple(
                sorted(frozen_monthly, reverse=True)
            )
        picked = self._eligible_month(
            frozen_monthly,
            as_of=as_of,
            allowed_parts=part_ids,
            ordered_months=self._month_order_cache.get(view_key),
        )
        if picked is None:
            self._reference_cache[reference_key] = None
            return None
        month, samples = picked
        (
            inc,
            ex,
            legacy,
            basis,
            count,
            first,
            latest,
            estimated,
            inc_estimated,
            ex_estimated,
            reference_date_missing,
        ) = summarize_samples(samples)
        if reference_date_missing or first is None or latest is None:
            raise ValueError("historical reference sample must have a date")
        trace = _month_distance(as_of, latest)
        # 统一 13% 是确定性业务政策，不属于“缺税率估算”。
        assert not estimated and not inc_estimated and not ex_estimated
        result = CostReference(
            source=source,
            unit_cost_inc_tax=inc,
            unit_cost_ex_tax=ex,
            legacy_unit_cost=legacy,
            legacy_tax_basis=basis,
            confidence="low",
            reference_side=side,
            pool_group_id=pool_group_id,
            pool_version=pool_version,
            sample_count=count,
            reference_from_date=first,
            reference_to_date=latest,
            reference_latest_date=latest,
            price_month=month,
            trace_months=trace,
            anomaly_flags=(),
        )
        self._reference_cache[reference_key] = result
        return result

    def resolve(self, part_id: int, as_of: date) -> CostReference | None:
        """按 pool_purchase→pool_sales→purchase_history→sales_history 解析。"""
        cache_key = (part_id, as_of)
        if cache_key in self._cache:
            return self._cache[cache_key]
        pool = self._target_pool.get(part_id)
        if pool is not None:
            group_id, version = pool
            # 冻结口径：池均价统计有效池全体成员，目标 PN 本身也是成员。
            members = self._pool_members[group_id]
            for source, side in (
                ("pool_purchase", "purchase"),
                ("pool_sales", "sales"),
            ):
                result = self._reference(
                    source=source,
                    side=side,
                    part_ids=members,
                    as_of=as_of,
                    pool_group_id=group_id,
                    pool_version=version,
                )
                if result is not None:
                    self._cache[cache_key] = result
                    return result
        own = frozenset({part_id})
        for source, side in (
            ("purchase_history", "purchase"),
            ("sales_history", "sales"),
        ):
            result = self._reference(
                source=source,
                side=side,
                part_ids=own,
                as_of=as_of,
                pool_group_id=None,
                pool_version=None,
            )
            if result is not None:
                self._cache[cache_key] = result
                return result
        self._cache[cache_key] = None
        return None


def _confirmed_source_error(side: ReferenceSide, line_id):
    return exists(
        select(1).select_from(FactDataQualityIssue).where(
            FactDataQualityIssue.side == side,
            FactDataQualityIssue.line_id == line_id,
            FactDataQualityIssue.status == "confirmed_source_error",
        )
    )


def _freeze_samples(
    rows: Iterable[CostSample],
) -> dict[int, dict[str, tuple[CostSample, ...]]]:
    index: dict[int, dict[str, list[CostSample]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for sample in rows:
        if sample.occurred_on is None:
            continue
        index[sample.part_id][_month_key(sample.occurred_on)].append(sample)
    return {
        part_id: {
            month: tuple(samples)
            for month, samples in monthly.items()
        }
        for part_id, monthly in index.items()
    }


def build_reference_index(
    db: Session,
    *,
    target_part_ids: Iterable[int],
    max_as_of: date,
) -> CostReferenceIndex:
    """固定三条查询批量构建历史参考索引。

    查询 1 同时取得目标 PN 所属有效池及池全体成员；查询 2/3 分别读取采购/销售。
    ``confirmed_source_error`` 在 SQL 层剔除，未来日期以本批最晚出库日截断，逐行更早
    的出库日再由内存解析二次截断。
    """
    targets = frozenset(int(part_id) for part_id in target_part_ids)
    if not targets:
        return CostReferenceIndex(
            target_pool={},
            pool_members={},
            purchases={},
            sales={},
        )

    target_groups = (
        select(PartPoolMember.group_id)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .where(
            PartPool.status == "active",
            PartPoolMember.part_id.in_(targets),
        )
    )
    pool_rows = db.execute(
        select(
            PartPool.group_id,
            PartPool.version,
            PartPoolMember.part_id,
        )
        .join(PartPoolMember, PartPoolMember.group_id == PartPool.group_id)
        .where(
            PartPool.status == "active",
            PartPool.group_id.in_(target_groups),
        )
        .order_by(PartPool.group_id, PartPoolMember.part_id)
    ).all()
    pool_members_mut: dict[int, set[int]] = defaultdict(set)
    target_groups_mut: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for group_id, version, member_part_id in pool_rows:
        pool_members_mut[group_id].add(member_part_id)
        if member_part_id in targets:
            target_groups_mut[member_part_id].append((group_id, version))
    unique_target_groups = {
        part_id: tuple(sorted(set(groups)))
        for part_id, groups in target_groups_mut.items()
    }
    # 写路径保证唯一；历史脏数据让同一 PN 同属多个 active 池时只关闭池级参考，
    # 不擅自选择任一池，随后按既定瀑布继续尝试本 PN 历史采购/销售。
    target_pool = {
        part_id: groups[0]
        for part_id, groups in unique_target_groups.items()
        if len(groups) == 1
    }
    pool_members = {
        group_id: frozenset(members)
        for group_id, members in pool_members_mut.items()
    }
    all_parts = frozenset(targets).union(
        *(members for members in pool_members.values())
    )

    purchase_stmt = (
        select(
            FPurchaseLine.part_id,
            FPurchaseOrder.order_date,
            FPurchaseLine.qty,
            FPurchaseLine.unit_price,
            FPurchaseOrder.tax_rate,
            FPurchaseOrder.is_tax_inclusive,
        )
        .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
        .where(
            FPurchaseLine.part_id.in_(all_parts),
            FPurchaseOrder.order_date.is_not(None),
            FPurchaseOrder.order_date <= max_as_of,
            FPurchaseOrder.source_type.in_(tuple(config.COST_PURCHASE_TYPES)),
            FPurchaseLine.pn_std.notin_(tuple(config.MAINT_POOL_EXCLUDE_PNS)),
            FPurchaseLine.qty.is_not(None),
            FPurchaseLine.qty > 0,
            FPurchaseLine.qty < _MONEY_MAX,
            FPurchaseLine.unit_price.is_not(None),
            FPurchaseLine.unit_price > 0,
            FPurchaseLine.unit_price < _MONEY_MAX,
            ~_confirmed_source_error("purchase", FPurchaseLine.id),
        )
    )
    purchase_stmt = active_orders(purchase_stmt, FPurchaseOrder)
    purchase_samples = (
        CostSample(
            side="purchase",
            part_id=part_id,
            occurred_on=occurred_on,
            qty=qty,
            unit_price=unit_price,
            tax_rate=tax_rate,
            is_tax_inclusive=is_tax_inclusive,
        )
        for part_id, occurred_on, qty, unit_price, tax_rate, is_tax_inclusive
        in db.execute(purchase_stmt)
    )

    sales_stmt = (
        select(
            FSalesLine.part_id,
            FSalesOrder.order_date,
            FSalesLine.qty,
            FSalesLine.unit_price,
            FSalesOrder.tax_rate,
        )
        .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
        .where(
            FSalesLine.part_id.in_(all_parts),
            FSalesOrder.order_date.is_not(None),
            FSalesOrder.order_date <= max_as_of,
            FSalesOrder.business_type.in_(
                tuple(config.MAINT_SALES_REF_BUSINESS_TYPES)
            ),
            FSalesLine.pn_std.notin_(tuple(config.MAINT_POOL_EXCLUDE_PNS)),
            FSalesLine.counts_revenue.is_(True),
            FSalesLine.qty.is_not(None),
            FSalesLine.qty > 0,
            FSalesLine.qty < _MONEY_MAX,
            FSalesLine.unit_price.is_not(None),
            FSalesLine.unit_price > 0,
            FSalesLine.unit_price < _MONEY_MAX,
            ~_confirmed_source_error("sales", FSalesLine.id),
        )
    )
    sales_stmt = active_orders(sales_stmt, FSalesOrder)
    sales_samples = (
        CostSample(
            side="sales",
            part_id=part_id,
            occurred_on=occurred_on,
            qty=qty,
            unit_price=unit_price,
            tax_rate=tax_rate,
        )
        for part_id, occurred_on, qty, unit_price, tax_rate in db.execute(sales_stmt)
    )
    return CostReferenceIndex(
        target_pool=target_pool,
        pool_members=pool_members,
        purchases=_freeze_samples(purchase_samples),
        sales=_freeze_samples(sales_samples),
    )
