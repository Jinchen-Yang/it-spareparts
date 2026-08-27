"""维保出库成本核算引擎 + 项目聚合（docs/维保出库成本核算-开发方案.md §4-§5）。

独立旁路：不改 cost.replay / profit.recompute 的任何语义（回归红线）。

取价瀑布（每条有效出库行）：
  A0 direct    —— 专属采购直配：采购单「维保需求单」== 本行维保单号（WBDD），同 part 加权价
  A1 window    —— 出库日 ±MAINT_PRICE_WINDOW_DAYS 天内最近采购价（同距取更早、同日加权）
  A2 month_avg —— 同 part 出库当月采购加权均价（客户口径 Q1：当月加权）
  B1 pool_purchase → pool_sales —— 有效互通 PN 池全体成员（含目标 PN）三个月内参考
  B2 purchase_history → sales_history —— 本 PN 三个月内参考
  C  manual    —— 自动瀑布仍缺失时的人工回填
  D  none      —— 无成本，待人工
trace_avg/sales_ref 仅保留历史数据兼容，新重算不再产生。
confidence：direct/window=high（校准中位偏差 0%）、month_avg=medium（6.9%）、
历史参考=low，人工回填=high。
起算日（MAINT_COST_START_DATE）前的行不计价：cost_source=NULL，区别于"算了但没算出来"的 none。
"""
import hashlib
import hmac
import logging
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.orm import Session, aliased

from app import config, security, tax_policy
from app.business_time import business_today
from app.config import get_settings
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    FProjectExpense,
    MaintenanceManualCostOverride,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services import (
    maintenance_cost_invalidation,
    maintenance_cost_quality,
    maintenance_cost_reference,
    maintenance_demands,
    maintenance_margin,
    maintenance_margin_evidence,
    maintenance_project_operations,
)
from app.services.maintenance_match_keys import exact_match_key
from app.services.query_filters import active_orders, col_matches_any, keyword_groups_or_substr

_log = logging.getLogger("maintenance_cost")
_CENT = Decimal("0.01")
_ZERO = Decimal("0")
# Money 列为 Numeric(14,2)：绝对值上限 10^12（含）会溢出，回填前守卫
_MONEY_MAX = Decimal(10) ** 12
# 导入期写入的行级 flag（recompute 重建 flags 时保留；成本派生 flag 每轮重算重挂）
_IMPORT_FLAGS = maintenance_cost_invalidation.IMPORT_ANOMALY_FLAGS
_COST_DERIVED_FLAGS = maintenance_cost_quality.COST_DERIVED_ANOMALY_FLAGS
COSTED_SOURCES = (
    "direct",
    "window",
    "month_avg",
    "trace_avg",
    "sales_ref",
    "pool_purchase",
    "pool_sales",
    "purchase_history",
    "sales_history",
    "manual",
)
# v2 §16.1：置信度按来源定档——direct/window 校准中位偏差 0%、month_avg 6.9%、追溯/销售参考 25%+
_CONFIDENCE = {"direct": "high", "window": "high", "month_avg": "medium",
               "trace_avg": "low", "sales_ref": "low",
               "pool_purchase": "low", "pool_sales": "low",
               "purchase_history": "low", "sales_history": "low",
               "manual": "high"}

# recompute 写回的派生成本字段全集（与下方 updates 字典键一致；anomaly_flags 单列）。
# 「实际成本变化」判定 = 写前读库快照 vs 内存计算结果逐字段比对；只有语义变化
# 才 bump 归属项目 workbook revision——import_batch_id/时间戳类不在比对集。
_TRACKED_COST_FIELDS = (
    "unit_cost", "cost_amount",
    "unit_cost_inc_tax", "unit_cost_ex_tax",
    "cost_amount_inc_tax", "cost_amount_ex_tax",
    "cost_source", "cost_tax_basis",
    "price_month", "trace_months", "linked_purchase_order_no",
    "price_distance_days", "confidence",
    "reference_side", "reference_pool_group_id", "reference_pool_version",
    "reference_sample_count", "reference_from_date", "reference_to_date",
    "reference_latest_date",
)


def _import_owned_flags(flags) -> list[str]:
    """flag 中的导入自有子集（变化判定用；派生 flag 是取价结果的纯函数）。"""

    return sorted(f for f in (flags or []) if f in _IMPORT_FLAGS)


def _probe_assigned_project_ids(db: Session, source_order_ids) -> set[str]:
    """当前生效挂靠 probe（只读不加锁）：source_order_id → 稳定项目 ID 集合。

    与 etl.loader 的导入侧 probe 同一口径；锁/ bump 一律走
    maintenance_project_operations 的公共 helper（lock_workbook_states /
    bump_locked_workbook_revision），本模块不另造锁序逻辑。
    """
    ids = sorted({str(s) for s in source_order_ids if s})
    if not ids:
        return set()
    return set(db.scalars(
        select(MaintenanceSourceOrderAssignment.project_id).where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    ))


def _ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _ym_shift(ym: str, k: int) -> str:
    """ym 往前 k 个月（k≥0）。"""
    y, m = int(ym[:4]), int(ym[5:7])
    m -= k
    while m <= 0:
        y -= 1
        m += 12
    return f"{y:04d}-{m:02d}"


def _basis_order() -> tuple[str, str]:
    return ("inc", "ex") if config.MAINT_TAX_PREFERENCE == "inc_first" else ("ex", "inc")


def _purchase_pools(db: Session):
    """构建 直配池 + ±窗口日度池 + 采购月度池。

    direct:  (WBDD_NO_upper, part_id) -> {basis: [Σ金额, Σ数量, 采购单号(最小,可解释)]}
    daily:   part_id -> {采购日: {basis: [Σ金额, Σ数量]}}（window 层：同日加权）
    monthly: (part_id, 'YYYY-MM', basis) -> [Σ金额, Σ数量]
    金额一律 qty×unit_price（采购「其他款项」运费等不摊入单价，§0 客户口径）。
    直配池不过滤采购类型（显式关联到该维保单的采购就是它的成本）；
    日度/月度池按 COST_PURCHASE_TYPES 过滤，各池都排除打包占位 PN。
    """
    q = (
        select(FPurchaseLine.part_id, FPurchaseLine.pn_std, FPurchaseLine.qty,
               FPurchaseLine.unit_price,
               FPurchaseOrder.order_date, FPurchaseOrder.order_no,
               FPurchaseOrder.source_type, FPurchaseOrder.is_tax_inclusive,
               FPurchaseOrder.linked_maintenance_order_no,
               FPurchaseOrder.tax_rate)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseLine.unit_price < _MONEY_MAX,
               FPurchaseLine.qty.is_not(None), FPurchaseLine.qty > 0,
               FPurchaseLine.qty < _MONEY_MAX)
    )
    q = active_orders(q, FPurchaseOrder)
    excl = {exact_match_key(p) for p in config.MAINT_POOL_EXCLUDE_PNS}
    direct: dict[tuple, dict] = defaultdict(dict)
    daily: dict[int, dict] = defaultdict(dict)
    monthly: dict[tuple, list] = defaultdict(lambda: [_ZERO, _ZERO])
    direct_samples: dict[tuple, list] = defaultdict(list)
    daily_samples: dict[tuple, list] = defaultdict(list)
    monthly_samples: dict[tuple, list] = defaultdict(list)
    for part, pn, qty, price, odate, ono, stype, inc, wbdd, tax_rate in db.execute(q):
        if exact_match_key(pn) in excl:
            continue
        basis = "inc" if inc else "ex"
        amt = qty * price
        sample = maintenance_cost_reference.CostSample(
            side="purchase",
            part_id=part,
            occurred_on=odate,
            qty=qty,
            unit_price=price,
            tax_rate=tax_rate,
            is_tax_inclusive=inc,
        )
        if wbdd:
            slot = direct[(exact_match_key(wbdd), part)].setdefault(basis, [_ZERO, _ZERO, ono])
            slot[0] += amt
            slot[1] += qty
            if ono and (slot[2] is None or ono < slot[2]):
                slot[2] = ono
            direct_samples[(exact_match_key(wbdd), part)].append(sample)
        if odate is not None and stype in config.COST_PURCHASE_TYPES:
            key = (part, _ym(odate), basis)
            monthly[key][0] += amt
            monthly[key][1] += qty
            monthly_samples[(part, _ym(odate))].append(sample)
            dslot = daily[part].setdefault(odate, {}).setdefault(basis, [_ZERO, _ZERO])
            dslot[0] += amt
            dslot[1] += qty
            daily_samples[(part, odate)].append(sample)
    # window 层查找用：每 part 的采购日期升序表（bisect 定位 ±窗口）
    daily_dates = {part: sorted(days) for part, days in daily.items()}
    return (
        direct,
        daily,
        daily_dates,
        monthly,
        direct_samples,
        daily_samples,
        monthly_samples,
    )


def _pick_window(daily: dict, daily_dates: dict, part: int, odate: date, max_days: int):
    """±max_days 内最近采购日（同距取更早），该日按税口径优先取加权均价。

    返回 (unit_cost, basis, distance_days, 取价日) | None。日期近优先于税口径偏好
    （用户口径"近日价更准"），税口径只在同一取价日内做取舍。
    """
    dates = daily_dates.get(part)
    if not dates:
        return None
    lo = bisect_left(dates, odate - timedelta(days=max_days))
    hi = bisect_right(dates, odate + timedelta(days=max_days))
    best = None
    for d in dates[lo:hi]:
        key = (abs((d - odate).days), d)          # 距离升序；同距日期更早者胜
        if best is None or key < best:
            best = key
    if best is None:
        return None
    slots = daily[part][best[1]]
    for basis in _basis_order():
        s = slots.get(basis)
        if s and s[1] > 0:
            return tax_policy.round_money(s[0] / s[1]), basis, best[0], best[1]
    return None


def _sales_pool(db: Session):
    """销售参考池（Q3）：备件销售的真实成交单价 → (part_id, 'YYYY-MM', basis) -> [Σ金额, Σ数量]。"""
    q = (
        select(FSalesLine.part_id, FSalesLine.pn_std, FSalesLine.qty, FSalesLine.unit_price,
               FSalesOrder.order_date, FSalesOrder.tax_rate)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.unit_price.is_not(None), FSalesLine.unit_price > 0,
               FSalesLine.unit_price < _MONEY_MAX,
               FSalesLine.qty.is_not(None), FSalesLine.qty > 0,
               FSalesLine.qty < _MONEY_MAX,
               FSalesOrder.business_type.in_(config.MAINT_SALES_REF_BUSINESS_TYPES))
    )
    q = active_orders(q, FSalesOrder)
    excl = set(config.MAINT_POOL_EXCLUDE_PNS)
    monthly: dict[tuple, list] = defaultdict(lambda: [_ZERO, _ZERO])
    monthly_samples: dict[tuple, list] = defaultdict(list)
    for part, pn, qty, price, odate, trate in db.execute(q):
        if pn in excl or odate is None:
            continue
        basis = "inc" if (trate and trate > 0) else "ex"
        key = (part, _ym(odate), basis)
        monthly[key][0] += qty * price
        monthly[key][1] += qty
        monthly_samples[(part, _ym(odate))].append(
            maintenance_cost_reference.CostSample(
                side="sales",
                part_id=part,
                occurred_on=odate,
                qty=qty,
                unit_price=price,
                tax_rate=trate,
            )
        )
    return monthly, monthly_samples


def _pick(monthly: dict, part: int, ym: str):
    """按税口径优先级取该月加权均价 → (unit_cost, basis) | None。"""
    for basis in _basis_order():
        slot = monthly.get((part, ym, basis))
        if slot and slot[1] > 0:
            return tax_policy.round_money(slot[0] / slot[1]), basis
    return None


@dataclass(frozen=True, slots=True)
class _LegacyCostSelection:
    """旧五层的只读解析结果；历史参考只能消费 source=None 的行。"""

    unit_cost: Decimal | None = None
    basis: str | None = None
    source: str | None = None
    price_month: str | None = None
    trace_months: int | None = None
    linked_purchase_order_no: str | None = None
    selected_samples: (
        list[maintenance_cost_reference.CostSample]
        | tuple[maintenance_cost_reference.CostSample, ...]
        | None
    ) = None
    distance_days: int | None = None


def _resolve_legacy_cost(
    *,
    part: int,
    qty: Decimal | None,
    order_no: str | None,
    order_date: date,
    direct: dict,
    daily: dict,
    daily_dates: dict,
    monthly: dict,
    direct_samples: dict,
    daily_samples: dict,
    monthly_samples: dict,
    window_days: int,
) -> _LegacyCostSelection:
    """解析自动瀑布前三层；历史参考必须在通用池优先的新解析器中完成。"""
    if qty is None:
        return _LegacyCostSelection(source="none")

    month = _ym(order_date)
    direct_key = (exact_match_key(order_no), part)
    slots = direct.get(direct_key)
    if slots:
        samples = direct_samples.get(direct_key)
        reference_date_missing = bool(samples) and any(
            sample.occurred_on is None
            for sample in samples
        )
        for basis in _basis_order():
            slot = slots.get(basis)
            if slot and slot[1] > 0:
                return _LegacyCostSelection(
                    unit_cost=tax_policy.round_money(slot[0] / slot[1]),
                    basis=basis,
                    source="direct",
                    price_month=None if reference_date_missing else month,
                    trace_months=None if reference_date_missing else 0,
                    linked_purchase_order_no=slot[2],
                    selected_samples=samples,
                    distance_days=None if reference_date_missing else 0,
                )

    window = _pick_window(
        daily,
        daily_dates,
        part,
        order_date,
        window_days,
    )
    if window:
        unit_cost, basis, distance, price_date = window
        return _LegacyCostSelection(
            unit_cost=unit_cost,
            basis=basis,
            source="window",
            price_month=_ym(price_date),
            trace_months=0,
            selected_samples=daily_samples.get((part, price_date)),
            distance_days=distance,
        )

    picked = _pick(monthly, part, month)
    if picked:
        unit_cost, basis = picked
        return _LegacyCostSelection(
            unit_cost=unit_cost,
            basis=basis,
            source="month_avg",
            price_month=month,
            trace_months=0,
            selected_samples=monthly_samples.get((part, month)),
        )

    return _LegacyCostSelection()


class MaintenanceCostRecomputeBusy(RuntimeError):
    """导入或另一轮重算正在持有数据变更锁。"""


class WorkbookInvalidationConflictError(RuntimeError):
    """写后复核发现归属项目超出预锁集合（并发挂靠）——fail closed，本轮回滚。"""


def recompute(
    db: Session,
    *,
    commit: bool = True,
    line_ids: set[int] | None = None,
) -> dict:
    """重算作用域内维保出库行的成本，批量回填。返回各来源计数。

    作用域 = 已生效 且 order_date ≥ MAINT_COST_START_DATE；起算日前/无日期 → 不计价（cost_source=NULL）；
    在期但 qty 缺失 → none + missing_qty 标记（可见可查，不静默丢）。
    先尝试取得与导入共用的 advisory lock；忙时立即拒绝，取得后再整体清零，
    防止并发重算/导入交错且避免管理员请求无限排队。

    写序两阶段（K3 writer-side workbook revision invalidation）：阶段一纯读
    （取价在内存完成）并做「实际成本变化」判定；只有确有变化的行的归属项目
    才进入阶段二——先排序预锁其 workbook state，再写行（失效清零 + 批量
    回填），最后同事务各 bump 一次 revision。禁止 line→state 反序。
    """
    acquired = db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:k)"),
        {"k": config.DATA_CHANGE_ADVISORY_LOCK_KEY},
    )
    if acquired is not True:
        raise MaintenanceCostRecomputeBusy(
            "维保数据导入或另一轮成本重算正在进行，请稍后重试",
        )
    (
        direct,
        daily,
        daily_dates,
        monthly,
        direct_samples,
        daily_samples,
        monthly_samples,
    ) = _purchase_pools(db)

    # 阶段一（纯读）：连当前派生成本字段一并取出作为 before 快照；失效清零
    # （UPDATE 行）推迟到 state 预锁之后，保证全局锁序 state → 事实行。
    q = (
        select(FMaintenanceLine.id, FMaintenanceLine.part_id,
               FMaintenanceLine.qty, FMaintenanceLine.return_qty,
               FMaintenanceLine.anomaly_flags,
               FMaintenanceOrder.order_no, FMaintenanceOrder.order_date,
               FMaintenanceOrder.raw_order_id,
               *[getattr(FMaintenanceLine, c) for c in _TRACKED_COST_FIELDS])
        .join(FMaintenanceOrder, FMaintenanceLine.order_id == FMaintenanceOrder.id)
        # 2026-08-19：作废行不参与取价重算（#55）
        .where(FMaintenanceLine.is_active.is_(True))
    )
    q = active_orders(q, FMaintenanceOrder)
    if line_ids is not None:
        q = q.where(FMaintenanceLine.id.in_(sorted(line_ids)))
    rows = db.execute(q).all()
    before_costs = {
        row.id: {c: row._mapping[c] for c in _TRACKED_COST_FIELDS}
        for row in rows
    }
    before_flags = {row.id: list(row.anomaly_flags or []) for row in rows}
    line_order_raw = {row.id: row.raw_order_id for row in rows}

    start = config.MAINT_COST_START_DATE
    window_days = config.MAINT_PRICE_WINDOW_DAYS
    legacy_selections = {
        row.id: _resolve_legacy_cost(
            part=row.part_id,
            qty=row.qty,
            order_no=row.order_no,
            order_date=row.order_date,
            direct=direct,
            daily=daily,
            daily_dates=daily_dates,
            monthly=monthly,
            direct_samples=direct_samples,
            daily_samples=daily_samples,
            monthly_samples=monthly_samples,
            window_days=window_days,
        )
        for row in rows
        if row.order_date is not None and row.order_date >= start
    }
    reference_scope = [
        row for row in rows
        if (
            row.order_date is not None
            and row.order_date >= start
            and legacy_selections[row.id].source is None
        )
    ]
    reference_index = maintenance_cost_reference.build_reference_index(
        db,
        target_part_ids=(row.part_id for row in reference_scope),
        max_as_of=max(
            (row.order_date for row in reference_scope),
            default=start,
        ),
    )
    manual_overrides = {
        override.line_id: override
        for override in db.scalars(
            select(MaintenanceManualCostOverride).where(
                MaintenanceManualCostOverride.active.is_(True),
                MaintenanceManualCostOverride.line_id.in_(
                    [row.id for row in reference_scope],
                ),
            )
        )
    } if reference_scope else {}
    stats = {"lines_in_scope": 0, "out_of_scope": 0, "missing_qty": 0,
             "direct": 0, "window": 0, "month_avg": 0, "trace_avg": 0, "sales_ref": 0,
             "pool_purchase": 0, "pool_sales": 0,
             "purchase_history": 0, "sales_history": 0,
             "manual": 0, "none": 0, "cost_overflow": 0,
             "projects_workbook_bumped": 0}
    updates = []
    for row in rows:
        lid, part, qty, rqty = row.id, row.part_id, row.qty, row.return_qty
        flags, order_no, odate = row.anomaly_flags, row.order_no, row.order_date
        if odate is None or odate < start:      # 起算日外：不计价，flags 由下方失效清零收敛
            stats["out_of_scope"] += 1
            continue
        stats["lines_in_scope"] += 1
        base_flags = [f for f in (flags or []) if f in _IMPORT_FLAGS]
        legacy = legacy_selections[lid]
        unit_cost = legacy.unit_cost
        basis = legacy.basis
        source = legacy.source
        price_month = legacy.price_month
        trace = legacy.trace_months
        linked_po = legacy.linked_purchase_order_no
        unit_cost_inc = unit_cost_ex = None
        selected_samples = legacy.selected_samples
        reference_side = reference_pool_group_id = reference_pool_version = None
        reference_sample_count = None
        reference_from_date = reference_to_date = reference_latest_date = None
        distance = legacy.distance_days

        if qty is None:                          # 在期但数量缺失：可见的 none，非静默丢弃
            base_flags.append("missing_qty")
            stats["missing_qty"] += 1

        if source in {"direct", "window", "month_avg"}:
            if selected_samples:
                (
                    unit_cost_inc,
                    unit_cost_ex,
                    _legacy,
                    _legacy_basis,
                    reference_sample_count,
                    reference_from_date,
                    reference_latest_date,
                    tax_rate_estimated,
                    inc_tax_estimated,
                    ex_tax_estimated,
                    reference_date_missing,
                ) = maintenance_cost_reference.summarize_samples(selected_samples)
                reference_to_date = reference_latest_date
                reference_side = "purchase"
                if tax_rate_estimated:
                    base_flags.append("tax_rate_estimated")
                if inc_tax_estimated:
                    base_flags.append("inc_tax_estimated")
                if ex_tax_estimated:
                    base_flags.append("ex_tax_estimated")
                if reference_date_missing:
                    reference_from_date = None
                    reference_to_date = None
                    reference_latest_date = None
                    base_flags.append("reference_date_missing")
        # 前三层全部失配后，严格按池采购→池销售→本 PN 采购→本 PN 销售。
        if source is None:
            reference = reference_index.resolve(part, odate)
            if reference is not None:
                source = reference.source
                unit_cost = reference.legacy_unit_cost
                basis = reference.legacy_tax_basis
                unit_cost_inc = reference.unit_cost_inc_tax
                unit_cost_ex = reference.unit_cost_ex_tax
                price_month = reference.price_month
                trace = reference.trace_months
                reference_side = reference.reference_side
                reference_pool_group_id = reference.pool_group_id
                reference_pool_version = reference.pool_version
                reference_sample_count = reference.sample_count
                reference_from_date = reference.reference_from_date
                reference_to_date = reference.reference_to_date
                reference_latest_date = reference.reference_latest_date
                base_flags.extend(reference.anomaly_flags)
        # 人工成本只能接管自动瀑布仍未命中的行；不允许覆盖任何自动证据。
        if source is None and (override := manual_overrides.get(lid)) is not None:
            try:
                manual_ex = Decimal(override.unit_cost_ex_tax)
                manual_inc = Decimal(override.unit_cost_inc_tax)
            except (ArithmeticError, TypeError, ValueError):
                manual_ex = manual_inc = Decimal("NaN")
            if (
                manual_ex.is_finite()
                and manual_inc.is_finite()
                and _ZERO <= manual_ex < _MONEY_MAX
                and _ZERO <= manual_inc < _MONEY_MAX
            ):
                source = "manual"
                basis = "ex"
                unit_cost = unit_cost_ex = tax_policy.round_money(manual_ex)
                unit_cost_inc = tax_policy.round_money(manual_inc)
        if source is None:
            source = "none"
            base_flags.append("no_cost")

        if rqty and rqty > 0:
            base_flags.append("has_return")
        cost_amount = None
        cost_amount_inc = cost_amount_ex = None
        if unit_cost is not None:
            eff_qty = max((qty or _ZERO) - (rqty or _ZERO), _ZERO)
            cost_amount = tax_policy.round_money(eff_qty * unit_cost)
            if unit_cost_inc is not None and unit_cost_ex is not None:
                cost_amount_inc = tax_policy.round_money(
                    eff_qty * unit_cost_inc,
                )
                cost_amount_ex = tax_policy.round_money(
                    eff_qty * unit_cost_ex,
                )
            # 溢出守卫：单价/数量异常导致金额超 Numeric(14,2) 容量 → 行级隔离（可见可修，不拖垮全批）
            amounts = (
                unit_cost,
                cost_amount,
                unit_cost_inc,
                unit_cost_ex,
                cost_amount_inc,
                cost_amount_ex,
            )
            if any(value is not None and value >= _MONEY_MAX for value in amounts):
                unit_cost = cost_amount = basis = price_month = trace = linked_po = None
                unit_cost_inc = unit_cost_ex = cost_amount_inc = cost_amount_ex = None
                reference_side = reference_pool_group_id = reference_pool_version = None
                reference_sample_count = None
                reference_from_date = reference_to_date = reference_latest_date = None
                distance = None
                source = "none"
                base_flags.append("cost_overflow")
                stats["cost_overflow"] += 1

        stats[source] += 1
        updates.append({
            "id": lid, "unit_cost": unit_cost, "cost_amount": cost_amount,
            "unit_cost_inc_tax": unit_cost_inc,
            "unit_cost_ex_tax": unit_cost_ex,
            "cost_amount_inc_tax": cost_amount_inc,
            "cost_amount_ex_tax": cost_amount_ex,
            "cost_source": source, "cost_tax_basis": basis,
            "price_month": price_month, "trace_months": trace,
            "linked_purchase_order_no": linked_po,
            "price_distance_days": distance,
            "confidence": _CONFIDENCE.get(source),
            "reference_side": reference_side,
            "reference_pool_group_id": reference_pool_group_id,
            "reference_pool_version": reference_pool_version,
            "reference_sample_count": reference_sample_count,
            "reference_from_date": reference_from_date,
            "reference_to_date": reference_to_date,
            "reference_latest_date": reference_latest_date,
            "anomaly_flags": base_flags,
        })

    # —— 阶段一收尾：实际成本变化判定（before 读库快照 vs 内存计算结果）——
    # 起算日外行的 after = 失效清零态（派生字段全 NULL + 仅导入期 flag）。
    updates_by_id = {u["id"]: u for u in updates}
    changed_line_ids: set[int] = set()
    physical_write_line_ids: set[int] = set()
    for row in rows:
        upd = updates_by_id.get(row.id)
        if upd is None:
            after = {c: None for c in _TRACKED_COST_FIELDS}
            after_flags = [
                fl for fl in (row.anomaly_flags or []) if fl in _IMPORT_FLAGS
            ]
        else:
            after = {c: upd[c] for c in _TRACKED_COST_FIELDS}
            after_flags = list(upd["anomaly_flags"])
        before = before_costs[row.id]
        # flag 只比较导入自有子集：派生 flag 是取价结果的纯函数（成本字段变化
        # 必然同步体现），且导入 upsert 会在同事务内先把派生 flag 剥掉再由
        # 本函数重挂——全量比较会让 no-op 重导产生假变化。
        tracked_changed = any(
            before[c] != after[c] for c in _TRACKED_COST_FIELDS
        )
        if tracked_changed or before_flags[row.id] != after_flags:
            # derived flag 被 loader 暂时剥离时，最终业务语义可能是 no-op，
            # 但仍需把它重挂；只物理写这些真正不同的行，避免每轮全表 WAL/行锁。
            physical_write_line_ids.add(row.id)
        if (
            tracked_changed
            or _import_owned_flags(before_flags[row.id])
            != _import_owned_flags(after_flags)
        ):
            changed_line_ids.add(row.id)

    # 阶段二前置：所有即将物理写入行的归属项目都先排序锁 state；revision
    # 只对业务语义变化行对应项目 bump。这样 loader 派生 flag flap 的恢复写入
    # 也不会形成 line→state 反序。
    changed_raw_ids = sorted({line_order_raw[lid] for lid in changed_line_ids})
    physical_write_raw_ids = sorted({
        line_order_raw[lid] for lid in physical_write_line_ids
    })
    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssue,
        MaintenanceSiteIssueLine,
    )
    from app.models.maintenance_project import MaintenanceProject

    # Purchase/sales/WBDD changes can alter the deterministic waterfall of an
    # already-confirmed site issue even when no demand-line amount changes.  Lock
    # every affected project state before the first cost fact write; production
    # currently has only hundreds of such rows, so correctness beats a late-lock
    # optimization that would reopen state→fact deadlocks.
    site_project_ids = set(db.scalars(
        select(MaintenanceSiteIssue.project_id)
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .join(
            MaintenanceProject,
            MaintenanceProject.project_id == MaintenanceSiteIssue.project_id,
        )
        .where(
            MaintenanceProject.is_active.is_(True),
            MaintenanceSiteIssue.status_mapping_state == "mapped",
            MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
        .distinct()
    ))
    locked_states = maintenance_project_operations.lock_workbook_states(
        db,
        project_ids=(
            _probe_assigned_project_ids(db, physical_write_raw_ids)
            | site_project_ids
        ),
    )
    initial_revisions = {
        project_id: state.revision for project_id, state in locked_states.items()
    }

    # 只清本轮阶段一真实读取到的 active 行。按“所有 active order”批量清会把
    # inactive line 的历史成本也抹掉；并且会写到未进入 before/after、未预锁
    # 归属项目的行，破坏 revision 证明。
    if physical_write_line_ids:
        invalidation_condition = FMaintenanceLine.id.in_(sorted(physical_write_line_ids))
        maintenance_cost_invalidation.invalidate_line_costs(
            db,
            condition=invalidation_condition,
            pending_recompute=False,
        )

    write_updates = [u for u in updates if u["id"] in physical_write_line_ids]
    for i in range(0, len(write_updates), 1000):
        db.execute(update(FMaintenanceLine), write_updates[i:i + 1000])
    db.flush()

    site_lines_repriced = 0
    site_projects_repriced = 0
    for project_id in sorted(site_project_ids):
        site_result = maintenance_project_operations.recompute_cost_gaps(
            db,
            project_id=project_id,
            reason="上游采购/销售/维保需求成本变化，自动刷新现场领用取价证据",
            operated_by="system:maintenance-cost-recompute",
            allow_downgrade=True,
        )
        if site_result and site_result["resolved"]:
            site_lines_repriced += int(site_result["resolved"])
            site_projects_repriced += 1
    # 写后复核（只读）：若归属项目超出预锁集合（重算期间被并发挂靠），绝不
    # 新拿 state 锁（行锁已持有，反序即死锁面）——fail closed 整体回滚。
    unexpected = _probe_assigned_project_ids(db, physical_write_raw_ids) - set(locked_states)
    if unexpected:
        raise WorkbookInvalidationConflictError(
            "成本重算期间出现预锁集合外的项目挂靠（并发变更），请重试："
            f"{sorted(unexpected)}"
        )
    # 实际成本变化的归属项目同事务各 bump 一次（helper 保证每事务每项目至多一次）
    revision_project_ids = _probe_assigned_project_ids(db, changed_raw_ids)
    for project_id in sorted(revision_project_ids):
        state = locked_states[project_id]
        maintenance_project_operations.bump_locked_workbook_revision(db, state=state)
    stats["site_lines_repriced"] = site_lines_repriced
    stats["site_projects_repriced"] = site_projects_repriced
    stats["projects_workbook_bumped"] = sum(
        1
        for project_id, state in locked_states.items()
        if state.revision != initial_revisions[project_id]
    )
    if commit:
        db.commit()
    else:
        db.flush()
    _log.info("maintenance_cost.recompute: %s", stats)
    return stats


# ============================================================
# 项目聚合 / 明细（§5）——只统计作用域内（已生效 + 起算日后）的出库行。
# 聚合下推到 SQL（GROUP BY project_std）避免全量 ORM 物化（大数据量级内存/耗时可控）。
# ============================================================

def _f(x):
    return float(x) if x is not None else None


def _scoped_filters(stmt, date_from, date_to):
    stmt = stmt.where(FMaintenanceOrder.order_date >= config.MAINT_COST_START_DATE)
    stmt = active_orders(stmt, FMaintenanceOrder)
    if date_from:
        stmt = stmt.where(FMaintenanceOrder.order_date >= date_from)
    if date_to:
        stmt = stmt.where(FMaintenanceOrder.order_date <= date_to)
    return stmt


def _effective_cost_date_from(date_from: date | None) -> date:
    """项目成本及其费用证据统一不得早于项目成本起算日。"""
    if date_from is None:
        return config.MAINT_COST_START_DATE
    return max(date_from, config.MAINT_COST_START_DATE)


def _date_filtered_budget_decision(decision: dict, *, date_filtered: bool) -> dict:
    """期间支出不得和整合同预算比较，但证据缺口仍须保持最高优先级。"""
    if not date_filtered or decision["decision_status"] in {
        "incomplete_cost",
        "expense_data_unavailable",
    }:
        return decision
    return {
        "decision_status": "filtered_scope",
        "known_spend_total": decision["known_spend_total"],
        "remaining": None,
        "remaining_pct": None,
    }


def _matched_maintenance_contracts(date_from, date_to, q_text: str):
    """找出命中项目搜索的合同号，但不裁掉同合同下的其他项目成本。

    看板是合同粒度。搜索只负责召回合同；后续主查询仍聚合该合同在当前日期
    作用域内的完整项目、备件成本和期限，避免把共享合同重算得虚假健康。
    """
    match_order = aliased(FMaintenanceOrder)
    contract = match_order.linked_sales_order_no
    stmt = (
        select(contract)
        .select_from(match_order)
        .where(
            match_order.order_date >= config.MAINT_COST_START_DATE,
            match_order.linked_sales_order_no.is_not(None),
            func.btrim(match_order.linked_sales_order_no) != "",
        )
    )
    stmt = active_orders(stmt, match_order)
    if date_from:
        stmt = stmt.where(match_order.order_date >= date_from)
    if date_to:
        stmt = stmt.where(match_order.order_date <= date_to)
    for keyword_group in keyword_groups_or_substr(q_text):
        stmt = stmt.where(col_matches_any(match_order.project_std, keyword_group))
    return stmt.distinct()


_LIFECYCLE_FILTERS = frozenset({"ongoing", "ended", "missing", "all"})


def _normalize_lifecycle(lifecycle: str) -> str:
    if lifecycle not in _LIFECYCLE_FILTERS:
        raise ValueError(f"unsupported maintenance lifecycle: {lifecycle}")
    return lifecycle


def _lifecycle_status(missing_count: int, latest_end: date | None, as_of: date) -> str:
    """聚合期限：任一底层单缺日期优先判 missing，否则以最晚终止日判断。"""
    if missing_count or latest_end is None:
        return "missing"
    return "ended" if latest_end < as_of else "ongoing"


def _parts_tax_basis_summary(
    *,
    basis: str,
    lines: int,
    amount,
    actual_lines: int,
    estimated_lines: int,
) -> dict:
    """构造归一双税口径摘要；不复用/改写 legacy 原始税口径分桶。"""
    prefix = f"parts_cost_{basis}_tax"
    if lines <= 0:
        return {
            prefix: None,
            f"{prefix}_complete": False,
            f"{prefix}_quality": "incomplete",
            f"{prefix}_missing_lines": 0,
        }
    missing_lines = lines - actual_lines - estimated_lines
    if missing_lines < 0:
        raise ValueError("normalized maintenance cost line counts do not add up")
    quality = (
        "incomplete"
        if missing_lines
        else "contains_estimate"
        if estimated_lines
        else "actual_only"
    )
    return {
        prefix: tax_policy.round_money(amount or _ZERO),
        f"{prefix}_complete": missing_lines == 0,
        f"{prefix}_quality": quality,
        f"{prefix}_missing_lines": missing_lines,
    }


def _normalized_cost_expressions(ml, *, basis: str):
    """Return strict normalized cost evidence, including active manual fallback."""
    normalized_amount = (
        ml.cost_amount_inc_tax if basis == "inc" else ml.cost_amount_ex_tax
    )
    manual_unit = (
        MaintenanceManualCostOverride.unit_cost_inc_tax
        if basis == "inc"
        else MaintenanceManualCostOverride.unit_cost_ex_tax
    )
    return maintenance_cost_quality.sql_normalized_line_cost(
        source_column=ml.cost_source,
        tax_basis_column=ml.cost_tax_basis,
        legacy_amount_column=ml.cost_amount,
        normalized_amount_column=normalized_amount,
        normalized_basis=basis,
        anomaly_flags_column=ml.anomaly_flags,
        qty_column=ml.qty,
        return_qty_column=ml.return_qty,
        manual_unit_cost_column=manual_unit,
        manual_active_column=MaintenanceManualCostOverride.active,
    )


def _dual_cost_aggregate_columns(ml, _actual_bucket=None, _estimated_bucket=None):
    """返回项目/合同查询共用的 normalized 双税聚合列。"""
    columns = []
    for basis in ("inc", "ex"):
        amount, normalized_actual, normalized_estimated, _missing = (
            _normalized_cost_expressions(ml, basis=basis)
        )
        known = or_(normalized_actual, normalized_estimated)
        columns.extend([
            func.coalesce(func.sum(amount).filter(known), 0).label(
                f"parts_cost_{basis}_tax"
            ),
            func.count().filter(normalized_actual).label(
                f"parts_cost_{basis}_tax_actual_lines"
            ),
            func.count().filter(normalized_estimated).label(
                f"parts_cost_{basis}_tax_estimated_lines"
            ),
        ])
    return columns


def _normalized_cost_summary(
    *,
    lines: int,
    actual_cost_inc,
    actual_cost_ex,
    estimated_cost_inc,
    estimated_cost_ex,
    actual_lines: int,
    estimated_lines: int,
) -> dict:
    """Compatibility summary whose decision total is canonical inc-tax cost."""
    actual_lines = int(actual_lines)
    estimated_lines = int(estimated_lines)
    missing_lines = max(0, int(lines) - actual_lines - estimated_lines)
    quality = (
        "incomplete"
        if lines <= 0 or missing_lines
        else "contains_estimate"
        if estimated_lines
        else "actual_only"
    )
    actual_inc = Decimal(actual_cost_inc or 0).quantize(_CENT)
    estimated_inc = Decimal(estimated_cost_inc or 0).quantize(_CENT)
    return {
        "actual_cost_inc": actual_inc,
        "actual_cost_ex": Decimal(actual_cost_ex or 0).quantize(_CENT),
        "estimated_cost_inc": estimated_inc,
        "estimated_cost_ex": Decimal(estimated_cost_ex or 0).quantize(_CENT),
        "actual_lines": actual_lines,
        "estimated_lines": estimated_lines,
        "missing_cost_lines": missing_lines,
        "known_cost_total": (actual_inc + estimated_inc).quantize(_CENT),
        "cost_quality": quality,
    }


def _normalized_cost_summary_from_row(row, *, lines: int) -> dict:
    return _normalized_cost_summary(
        lines=lines,
        actual_cost_inc=row.actual_cost_inc,
        actual_cost_ex=row.actual_cost_ex,
        estimated_cost_inc=row.estimated_cost_inc,
        estimated_cost_ex=row.estimated_cost_ex,
        actual_lines=row.parts_cost_inc_tax_actual_lines,
        estimated_lines=row.parts_cost_inc_tax_estimated_lines,
    )


def _dual_cost_summary_from_row(row, *, lines: int) -> dict:
    result = {}
    for basis in ("inc", "ex"):
        prefix = f"parts_cost_{basis}_tax"
        result.update(_parts_tax_basis_summary(
            basis=basis,
            lines=lines,
            amount=getattr(row, prefix),
            actual_lines=getattr(row, f"{prefix}_actual_lines"),
            estimated_lines=getattr(row, f"{prefix}_estimated_lines"),
        ))
    return result


def projects_aggregate(db: Session, date_from: date | None = None,
                       date_to: date | None = None, q_text: str | None = None,
                       user_ctx: security.UserContext | None = None,
                       lifecycle: str = "all",
                       as_of: date | None = None) -> dict:
    """项目维度聚合：成本按税口径分列小计（Q4：不混加），来源分布、覆盖率、合同额参考。

    合同额只认当前稳定项目合同台账的 ``amount_inc_tax``；未映射、缺金额、重复或
    跨项目共享均 fail-closed，不再从销售未税金额猜含税参考。
    """
    lifecycle = _normalize_lifecycle(lifecycle)
    as_of = as_of or business_today()
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    proj = func.coalesce(mo.project_std, "(未填项目)")
    inc_amount, inc_actual, inc_estimated, inc_missing = (
        _normalized_cost_expressions(ml, basis="inc")
    )
    ex_amount, ex_actual, ex_estimated, _ex_missing = (
        _normalized_cost_expressions(ml, basis="ex")
    )
    resolved_source = case(
        (and_(
            inc_actual,
            or_(ml.cost_source.is_(None), ml.cost_source == "none"),
        ), "manual"),
        else_=ml.cost_source,
    )
    src_cols = [
        func.count().filter(
            ~inc_missing,
            resolved_source == source,
        ).label(f"src_{source}")
        for source in COSTED_SOURCES
    ]
    stmt = (
        select(
            proj.label("project"),
            func.count(func.distinct(mo.id)).label("order_count"),
            func.count(func.distinct(mo.id)).filter(
                ml.id.is_(None),
            ).label("missing_detail_orders"),
            func.count(ml.id).label("lines"),
            func.coalesce(func.sum(ml.qty), 0).label("qty"),
            func.coalesce(func.sum(inc_amount).filter(inc_actual), 0).label(
                "actual_cost_inc"),
            func.coalesce(func.sum(ex_amount).filter(ex_actual), 0).label(
                "actual_cost_ex"),
            func.coalesce(func.sum(inc_amount).filter(inc_estimated), 0).label(
                "estimated_cost_inc"),
            func.coalesce(func.sum(ex_amount).filter(ex_estimated), 0).label(
                "estimated_cost_ex"),
            func.count(func.distinct(func.date_trunc("month", mo.order_date))).label("months"),
            func.count(func.distinct(mo.id)).filter(
                mo.maint_end.is_(None),
            ).label("maint_end_missing"),
            func.max(mo.maint_end).label("latest_maint_end"),
            func.array_agg(func.distinct(mo.linked_sales_order_no))
                .filter(
                    mo.linked_sales_order_no.is_not(None),
                    func.btrim(mo.linked_sales_order_no) != "",
                ).label("sales_orders"),
            *_dual_cost_aggregate_columns(ml),
            *src_cols,
        )
        .select_from(mo)
        .outerjoin(ml, ml.order_id == mo.id)
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == ml.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
        .group_by(proj)
    )
    stmt = _scoped_filters(stmt, date_from, date_to)
    scoped_sales = security.is_scoped_sales(user_ctx)
    if scoped_sales:
        if user_ctx and user_ctx.salesperson_name:
            stmt = stmt.where(mo.salesperson == user_ctx.salesperson_name)
        else:
            stmt = stmt.where(text("false"))
    if q_text and q_text.strip():
        # 分词模糊（大小写不敏感 + 变体，与全站搜索同源）：'联通 备件' 词序无关即可命中项目名
        for g in keyword_groups_or_substr(q_text):
            stmt = stmt.where(col_matches_any(mo.project_std, g))

    raw_all = db.execute(stmt).all()
    lifecycle_counts = {"ongoing": 0, "ended": 0, "missing": 0}
    classified = []
    for row in raw_all:
        lifecycle_status = _lifecycle_status(
            row.maint_end_missing, row.latest_maint_end, as_of,
        )
        lifecycle_counts[lifecycle_status] += 1
        classified.append((row, lifecycle_status))
    raw = [
        (row, lifecycle_status)
        for row, lifecycle_status in classified
        if lifecycle == "all" or lifecycle_status == lifecycle
    ]

    # order_no → 引用它的项目集合（判共用）；合同额一次性查回
    order_no_projects: dict[str, set] = defaultdict(set)
    # 共享合同判定仍看 lifecycle 过滤前的同一业务作用域，避免切换期限后
    # 把本来由多个项目共用的合同误标成独占。
    for r, _lifecycle in classified:
        for ono in (r.sales_orders or []):
            order_no_projects[ono].add(r.project)
    project_stable_ids: dict[str, set[str]] = defaultdict(set)
    project_contract_facts: dict[str, dict] = {}
    canonical_contract_project_ids: set[str] = set()
    # 受限销售账号只能用本人可见的 WBDD 行做项目召回，不能据此穿透到
    # 项目级 canonical 合同台账（同项目还可能包含其他销售的合同事实）。
    if classified and not scoped_sales:
        active_assignment = and_(
            MaintenanceSourceOrderAssignment.source_order_id == mo.raw_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        stable_stmt = (
            select(
                proj.label("project"),
                MaintenanceSourceOrderAssignment.project_id,
            )
            .select_from(mo)
            .join(MaintenanceSourceOrderAssignment, active_assignment)
            .group_by(proj, MaintenanceSourceOrderAssignment.project_id)
        )
        stable_stmt = _scoped_filters(stable_stmt, date_from, date_to)
        if q_text and q_text.strip():
            for group in keyword_groups_or_substr(q_text):
                stable_stmt = stable_stmt.where(col_matches_any(mo.project_std, group))
        for project_name, stable_id in db.execute(stable_stmt):
            project_stable_ids[project_name].add(stable_id)
        stable_ids = sorted({
            stable_id
            for ids in project_stable_ids.values()
            for stable_id in ids
        })
        if stable_ids:
            from app.models.maintenance_project import MaintenanceProjectContract
            from app.services.maintenance_boss_board import _card_contracts

            project_contract_facts = _card_contracts(db, stable_ids)
            canonical_contract_project_ids = set(db.scalars(
                select(MaintenanceProjectContract.project_id).where(
                    MaintenanceProjectContract.project_id.in_(stable_ids),
                    MaintenanceProjectContract.effective_from <= as_of,
                    or_(
                        MaintenanceProjectContract.effective_to.is_(None),
                        MaintenanceProjectContract.effective_to > as_of,
                    ),
                ).distinct()
            ))

    rows = []
    for r, lifecycle_status in raw:
        sales_orders = sorted(r.sales_orders or [])
        by_source = {s: getattr(r, f"src_{s}") for s in COSTED_SOURCES}
        cost_summary = _normalized_cost_summary_from_row(r, lines=r.lines)
        actual_lines = cost_summary["actual_lines"]
        estimated_lines = cost_summary["estimated_lines"]
        missing_cost_lines = r.lines - actual_lines - estimated_lines
        by_source["none"] = missing_cost_lines
        stable_ids = project_stable_ids.get(r.project, set())
        contract_fact = (
            project_contract_facts.get(next(iter(stable_ids)))
            if len(stable_ids) == 1
            and next(iter(stable_ids)) in canonical_contract_project_ids
            else None
        )
        if (
            not scoped_sales
            and contract_fact
            and contract_fact.get("contract_nos")
        ):
            # 金额来自稳定合同台账时，随行合同号也必须来自同一事实源；否则
            # 旧 WBDD 的空/陈旧 XSDD 会造成“有合同额却显示未关联合同”。
            sales_orders = sorted(set(contract_fact["contract_nos"]))
        contract_amt = (
            None
            if scoped_sales
            else contract_fact.get("amount_inc_tax") if contract_fact else None
        )
        # ``None`` is an explicit unknown canonical amount, including projects
        # which have no XSDD/current contract relationship at all.
        contract_incomplete = (
            None
            if scoped_sales
            else (
                contract_fact is None
                or bool(contract_fact.get("contract_incomplete"))
                or bool(contract_fact.get("contract_shared"))
            )
        )
        # Public contract amounts use an explicit tax basis.  The historical
        # ``contract_amount`` key remains an exact compatibility alias, but a
        # partial/ambiguous contract envelope must not surface as a precise
        # amount under either name.
        explicit_contract_amt = (
            contract_amt if contract_incomplete is False else None
        )
        if r.missing_detail_orders:
            cost_summary["cost_quality"] = "incomplete"
        cost_inc = tax_policy.round_money(
            cost_summary["actual_cost_inc"] + cost_summary["estimated_cost_inc"]
        )
        cost_ex = tax_policy.round_money(
            cost_summary["actual_cost_ex"] + cost_summary["estimated_cost_ex"]
        )
        dual_summary = _dual_cost_summary_from_row(r, lines=r.lines)
        if r.missing_detail_orders:
            for basis in ("inc", "ex"):
                prefix = f"parts_cost_{basis}_tax"
                dual_summary[f"{prefix}_complete"] = False
                dual_summary[f"{prefix}_quality"] = "incomplete"
        public_cost_summary = dict(cost_summary)
        if r.lines == 0:
            cost_inc = cost_ex = None
            for field in (
                "actual_cost_inc",
                "actual_cost_ex",
                "estimated_cost_inc",
                "estimated_cost_ex",
                "known_cost_total",
            ):
                public_cost_summary[field] = None
        rows.append({
            "project": r.project,
            "order_count": r.order_count,
            "missing_detail_orders": r.missing_detail_orders,
            "structure_complete": r.missing_detail_orders == 0,
            "lines": r.lines, "qty": _f(r.qty),
            "cost_inc": _f(cost_inc), "cost_ex": _f(cost_ex),
            "cost_total": _f(public_cost_summary["known_cost_total"]),
            **{key: _f(value) if isinstance(value, Decimal) else value
               for key, value in public_cost_summary.items()},
            **{key: _f(value) if isinstance(value, Decimal) else value
               for key, value in dual_summary.items()},
            "coverage_pct": round(
                (actual_lines + estimated_lines) / r.lines * 100, 1,
            ) if r.lines else None,
            "by_source": by_source,
            "months": r.months,
            "sales_orders": sales_orders,
            "contract_amount_inc_tax": (
                _f(explicit_contract_amt)
                if explicit_contract_amt is not None else None
            ),
            "contract_amount_basis": "inc_tax",
            "contract_amount": (
                _f(explicit_contract_amt)
                if explicit_contract_amt is not None else None
            ),
            "contract_shared": (
                any(len(order_no_projects[o]) > 1 for o in sales_orders)
                if scoped_sales
                else (
                    bool(contract_fact and contract_fact.get("contract_shared"))
                    or len(stable_ids) > 1
                    or any(len(order_no_projects[o]) > 1 for o in sales_orders)
                )
            ),
            # 只读当前台账含税事实；缺主档、未映射、重复或跨项目共享均不回退猜税。
            "contract_incomplete": (
                contract_incomplete
            ),
            "maint_end": (
                r.latest_maint_end.isoformat()
                if lifecycle_status != "missing" and r.latest_maint_end else None
            ),
            "lifecycle_status": lifecycle_status,
        })
    cost_restricted = security.is_field_hidden(user_ctx, "cost_total")
    budget_restricted = (
        scoped_sales
        or security.is_field_hidden(user_ctx, "contract_amount")
    )
    if budget_restricted:
        for row in rows:
            row["contract_amount_inc_tax"] = None
            row["contract_amount"] = None
    if cost_restricted:
        # 叶子金额置 null 还不够：按隐藏 cost_total 排序本身会泄漏相对成本，Agent
        # 再截前 N 条时泄漏更严重。无成本权限统一退回项目名稳定序。
        rows.sort(key=lambda r: (r["project"] or "").casefold())
    else:
        rows.sort(key=lambda r: r["cost_total"] or 0, reverse=True)
    return {
        "rows": rows, "start_date": config.MAINT_COST_START_DATE.isoformat(),
        "effective_sort": "project" if cost_restricted else "cost_total",
        "ranking_restricted": cost_restricted,
        "as_of": as_of.isoformat(),
        "lifecycle_filter": lifecycle,
        "lifecycle_counts": lifecycle_counts,
    }


def _project_lines_query(
    project: str,
    month: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    user_ctx: security.UserContext | None = None,
):
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    base = (
        select(ml, mo, MaintenanceManualCostOverride)
        .join(mo, ml.order_id == mo.id)
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == ml.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
    )
    # E2E #3：行级作废默认不进全局行列表（与总表/看板同口径）。
    base = base.where(ml.is_active.is_(True))
    base = base.where(mo.project_std == project if project != "(未填项目)"
                      else mo.project_std.is_(None))
    if security.is_scoped_sales(user_ctx):
        if user_ctx and user_ctx.salesperson_name:
            base = base.where(mo.salesperson == user_ctx.salesperson_name)
        else:
            base = base.where(text("false"))
    base = _scoped_filters(base, date_from, date_to)
    if month:
        base = base.where(func.to_char(mo.order_date, "YYYY-MM") == month)
    return base


def project_line_count(
    db: Session,
    project: str,
    month: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    user_ctx: security.UserContext | None = None,
) -> int:
    """返回与项目明细/CSV 完全同作用域的行数，供资源预检。"""
    base = _project_lines_query(
        project,
        month,
        date_from,
        date_to,
        user_ctx=user_ctx,
    )
    return db.scalar(select(func.count()).select_from(base.subquery())) or 0


def project_exists(
    db: Session,
    project: str,
    *,
    user_ctx: security.UserContext | None = None,
) -> bool:
    """判断调用者是否可见项目对象，不把成本范围或明细存在性混入对象存在。"""
    order = FMaintenanceOrder
    visible = select(order.id).where(
        order.project_std == project
        if project != "(未填项目)"
        else order.project_std.is_(None),
        maintenance_demands.active_demand_condition(order),
    )
    if security.is_scoped_sales(user_ctx):
        if user_ctx and user_ctx.salesperson_name:
            visible = visible.where(order.salesperson == user_ctx.salesperson_name)
        else:
            visible = visible.where(text("false"))
    return bool(db.scalar(select(visible.exists())))


def _serialize_project_line(
    ln: FMaintenanceLine,
    order: FMaintenanceOrder,
    override: MaintenanceManualCostOverride | None = None,
    *,
    hide_cost_signals: bool,
) -> dict:
    cost_fact = maintenance_cost_quality.resolved_line_cost_fields(
        source=ln.cost_source,
        tax_basis=ln.cost_tax_basis,
        legacy_unit_cost=ln.unit_cost,
        legacy_amount=ln.cost_amount,
        unit_cost_inc_tax=ln.unit_cost_inc_tax,
        unit_cost_ex_tax=ln.unit_cost_ex_tax,
        cost_amount_inc_tax=ln.cost_amount_inc_tax,
        cost_amount_ex_tax=ln.cost_amount_ex_tax,
        anomaly_flags=ln.anomaly_flags,
        confidence=ln.confidence,
        qty=ln.qty,
        return_qty=ln.return_qty,
        manual_unit_cost_inc_tax=(
            override.unit_cost_inc_tax if override is not None else None
        ),
        manual_unit_cost_ex_tax=(
            override.unit_cost_ex_tax if override is not None else None
        ),
        manual_active=override is not None and override.active is True,
    )
    cost_tier = cost_fact["tier"]
    has_known_cost = cost_tier != "missing"
    manual_fallback = cost_fact["manual_fallback"]
    flags = cost_fact["anomaly_flags"]
    if hide_cost_signals:
        flags = [flag for flag in flags if flag not in _COST_DERIVED_FLAGS]
    public_id = hmac.new(
        get_settings().secret_key.encode("utf-8"),
        f"maintenance-project-line:{ln.raw_line_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]
    return {
        "id": f"ML-{public_id}",
        "order_no": order.order_no,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "demand_type": order.demand_type,
        "business_type": order.business_type,
        "warehouse": order.warehouse,
        "pn_std": ln.pn_std,
        "description": ln.description,
        "qty": _f(ln.qty),
        "return_qty": _f(ln.return_qty),
        "unit_cost": _f(cost_fact["unit_cost"]) if has_known_cost else None,
        "cost_amount": _f(cost_fact["cost_amount"]) if has_known_cost else None,
        "unit_cost_inc_tax": (
            _f(cost_fact["unit_cost_inc_tax"]) if has_known_cost else None
        ),
        "unit_cost_ex_tax": (
            _f(cost_fact["unit_cost_ex_tax"]) if has_known_cost else None
        ),
        "cost_amount_inc_tax": (
            _f(cost_fact["cost_amount_inc_tax"]) if has_known_cost else None
        ),
        "cost_amount_ex_tax": (
            _f(cost_fact["cost_amount_ex_tax"]) if has_known_cost else None
        ),
        "cost_tier": cost_tier,
        "cost_source": cost_fact["source"],
        "cost_tax_basis": cost_fact["tax_basis"],
        "price_month": None if manual_fallback else ln.price_month,
        "trace_months": None if manual_fallback else ln.trace_months,
        "linked_purchase_order_no": (
            None if manual_fallback else ln.linked_purchase_order_no
        ),
        "price_distance_days": None if manual_fallback else ln.price_distance_days,
        "confidence": cost_fact["confidence"],
        "reference_side": None if manual_fallback else ln.reference_side,
        "reference_pool_group_id": (
            None if manual_fallback else ln.reference_pool_group_id
        ),
        "reference_pool_version": (
            None if manual_fallback else ln.reference_pool_version
        ),
        "reference_sample_count": (
            None if manual_fallback else ln.reference_sample_count
        ),
        "reference_from_date": (
            ln.reference_from_date.isoformat()
            if not manual_fallback and ln.reference_from_date else None
        ),
        "reference_to_date": (
            ln.reference_to_date.isoformat()
            if not manual_fallback and ln.reference_to_date else None
        ),
        "reference_latest_date": (
            ln.reference_latest_date.isoformat()
            if not manual_fallback and ln.reference_latest_date else None
        ),
        "anomaly_flags": flags,
    }


def iter_project_lines(
    db: Session,
    project: str,
    month: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    user_ctx: security.UserContext | None = None,
    yield_per: int = 1000,
):
    """流式遍历项目明细；与分页 API 共用查询和序列化真相源。"""
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    statement = _project_lines_query(
        project,
        month,
        date_from,
        date_to,
        user_ctx=user_ctx,
    ).order_by(
        mo.order_date.desc().nullslast(), ml.id.desc(),
    ).execution_options(stream_results=True, yield_per=yield_per)
    hide_cost_signals = security.is_field_hidden(user_ctx, "cost_total")
    result = db.execute(statement)
    try:
        for row in result:
            ln, order = row[0], row[1]
            override = row[2] if len(row) > 2 else None
            yield _serialize_project_line(
                ln,
                order,
                override,
                hide_cost_signals=hide_cost_signals,
            )
    finally:
        result.close()


def project_lines(db: Session, project: str, month: str | None = None,
                  date_from: date | None = None, date_to: date | None = None,
                  page: int = 1, page_size: int = 50,
                  user_ctx: security.UserContext | None = None) -> dict:
    """单项目 SKU 级明细（分页）：含成本来源/税口径/追溯月/关联采购单，逐行可解释。"""
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    base = _project_lines_query(
        project,
        month,
        date_from,
        date_to,
        user_ctx=user_ctx,
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = max(page, 1)
    paged = base.order_by(
        mo.order_date.desc().nullslast(), ml.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size)
    hide_cost_signals = security.is_field_hidden(user_ctx, "cost_total")
    rows = [
        _serialize_project_line(
            row[0],
            row[1],
            row[2] if len(row) > 2 else None,
            hide_cost_signals=hide_cost_signals,
        )
        for row in db.execute(paged)
    ]
    return {"total": total, "page": page, "page_size": page_size, "rows": rows}


# ============================================================
# v2 §16.2 盈亏看板（合同 XSDD 级）+ §16.4 工作簿导出数据
# ============================================================

def _contract_amounts(db: Session, order_nos: list[str]) -> dict[str, Decimal]:
    """XSDD → current mapped canonical ``amount_inc_tax`` (fail closed)."""
    from app.models.maintenance_project import MaintenanceProjectContract

    if not order_nos:
        return {}
    today = business_today()
    rows = db.execute(
        select(MaintenanceProjectContract)
        .where(
            MaintenanceProjectContract.contract_no.in_(order_nos),
            MaintenanceProjectContract.effective_from <= today,
            or_(
                MaintenanceProjectContract.effective_to.is_(None),
                MaintenanceProjectContract.effective_to > today,
            ),
        )
    ).scalars().all()
    grouped: dict[str, list] = defaultdict(list)
    for relation in rows:
        grouped[relation.contract_no].append(relation)
    out: dict[str, Decimal] = {}
    for contract_no, relations in grouped.items():
        included = [relation for relation in relations if relation.included_in_total]
        identities = {relation.contract_id for relation in included}
        amounts = {relation.amount_inc_tax for relation in included}
        duplicate_relationship = len({
            (relation.project_id, relation.contract_id)
            for relation in included
        }) != len(included)
        if (
            not included
            or any(relation.status_mapping_state != "mapped" for relation in relations)
            or len(identities) != 1
            or len({relation.project_id for relation in included}) != 1
            or duplicate_relationship
            or None in amounts
            or len(amounts) != 1
        ):
            continue
        out[contract_no] = Decimal(next(iter(amounts)))
    return out


def _expense_by_contract(db: Session, date_from: date | None = None,
                         date_to: date | None = None) -> dict[str, Decimal]:
    """报销费用（仅生效口径 MAINT_EXPENSE_ACTIVE_STATUS）按 XSDD 归集。"""
    pe = FProjectExpense
    stmt = (
        select(pe.linked_sales_order_no, func.coalesce(func.sum(pe.amount), 0))
        .where(pe.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS,
               pe.linked_sales_order_no.is_not(None))
        .group_by(pe.linked_sales_order_no)
    )
    if date_from:
        stmt = stmt.where(pe.expense_date >= date_from)
    if date_to:
        stmt = stmt.where(pe.expense_date <= date_to)
    return {k: Decimal(v) for k, v in db.execute(stmt).all()}


def board(db: Session, date_from: date | None = None, date_to: date | None = None,
          status: str | None = None,
          user_ctx: security.UserContext | None = None,
          lifecycle: str = "all",
          as_of: date | None = None,
          q_text: str | None = None) -> dict:
    """合同级预算消耗参考看板。

    预算 = 当前 mapped 合同台账 ``amount_inc_tax``；已知支出 = 归一含税备件成本
    + 生效报销费用。任一成本行缺失时优先返回 ``incomplete_cost``，不计算余额或
    红黄绿；完整且无正预算才返回 ``no_budget``。
    """
    lifecycle = _normalize_lifecycle(lifecycle)
    date_filtered = date_from is not None or date_to is not None
    as_of = as_of or business_today()
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    contract_col = mo.linked_sales_order_no
    proj = func.coalesce(mo.project_std, "(未填项目)")
    inc_amount, inc_actual, inc_estimated, _inc_missing = (
        _normalized_cost_expressions(ml, basis="inc")
    )
    ex_amount, ex_actual, ex_estimated, _ex_missing = (
        _normalized_cost_expressions(ml, basis="ex")
    )
    stmt = (
        select(
            contract_col.label("contract"), proj.label("project"),
            func.count(func.distinct(mo.id)).label("order_count"),
            func.count(func.distinct(mo.id)).filter(
                ml.id.is_(None),
            ).label("missing_detail_orders"),
            func.count(ml.id).label("lines"),
            func.coalesce(func.sum(inc_amount).filter(inc_actual), 0).label(
                "actual_cost_inc"),
            func.coalesce(func.sum(ex_amount).filter(ex_actual), 0).label(
                "actual_cost_ex"),
            func.coalesce(func.sum(inc_amount).filter(inc_estimated), 0).label(
                "estimated_cost_inc"),
            func.coalesce(func.sum(ex_amount).filter(ex_estimated), 0).label(
                "estimated_cost_ex"),
            func.count().filter(inc_actual).label("actual_lines"),
            func.count().filter(inc_estimated).label("estimated_lines"),
            func.coalesce(func.sum(inc_amount).filter(
                inc_estimated,
                ml.confidence == "low",
            ), 0).label("low_conf"),
            *_dual_cost_aggregate_columns(ml),
            func.min(mo.maint_start).label("mstart"), func.max(mo.maint_end).label("mend"),
            func.count(func.distinct(mo.id)).filter(
                mo.maint_end.is_(None),
            ).label("mend_missing"),
            func.min(mo.order_date).label("first_out"), func.max(mo.order_date).label("last_out"),
        )
        .select_from(mo)
        .outerjoin(ml, ml.order_id == mo.id)
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == ml.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
        .where(
            mo.linked_sales_order_no.is_not(None),
            func.btrim(mo.linked_sales_order_no) != "",
        )
        .group_by(contract_col, proj)
    )
    stmt = _scoped_filters(stmt, date_from, date_to)
    if q_text and q_text.strip():
        stmt = stmt.where(
            contract_col.in_(
                _matched_maintenance_contracts(date_from, date_to, q_text),
            ),
        )
    raw = db.execute(stmt).all()

    groups: dict[str, dict] = {}
    cost_restricted = security.is_field_hidden(user_ctx, "cost_total")
    profit_restricted = security.is_field_hidden(user_ctx, "gross_profit")
    for r in raw:
        project_summary = _normalized_cost_summary_from_row(r, lines=r.lines)
        missing_cost_lines = project_summary["missing_cost_lines"]
        if r.missing_detail_orders:
            project_summary["cost_quality"] = "incomplete"
        project_dual_summary = _dual_cost_summary_from_row(r, lines=r.lines)
        if r.missing_detail_orders:
            for basis in ("inc", "ex"):
                prefix = f"parts_cost_{basis}_tax"
                project_dual_summary[f"{prefix}_complete"] = False
                project_dual_summary[f"{prefix}_quality"] = "incomplete"
        project_public_summary = dict(project_summary)
        if r.lines == 0:
            for field in (
                "actual_cost_inc",
                "actual_cost_ex",
                "estimated_cost_inc",
                "estimated_cost_ex",
                "known_cost_total",
            ):
                project_public_summary[field] = None
        g = groups.get(r.contract)
        if g is None:
            g = {
                "projects": [], "order_count": 0,
                "missing_detail_orders": 0, "lines": 0,
                "actual_cost_inc": _ZERO, "actual_cost_ex": _ZERO,
                "estimated_cost_inc": _ZERO, "estimated_cost_ex": _ZERO,
                "actual_lines": 0, "estimated_lines": 0, "missing_cost_lines": 0,
                "parts_cost_inc_tax": _ZERO,
                "parts_cost_ex_tax": _ZERO,
                "parts_cost_inc_tax_actual_lines": 0,
                "parts_cost_inc_tax_estimated_lines": 0,
                "parts_cost_ex_tax_actual_lines": 0,
                "parts_cost_ex_tax_estimated_lines": 0,
                "low_conf": _ZERO,
                "mstart": None, "mend": None, "mend_missing": 0,
                "first_out": None, "last_out": None,
                # 单项目合同直接复用项目摘要；出现第二个项目时再走合同级合并。
                "single_project_summary": project_summary,
            }
            groups[r.contract] = g
        else:
            g["single_project_summary"] = None
        g["projects"].append({
            "project": r.project,
            "order_count": r.order_count,
            "missing_detail_orders": r.missing_detail_orders,
            "lines": r.lines,
            "spent_parts": (
                _f(project_summary["known_cost_total"])
                if r.lines else None
            ),
            **{
                key: _f(value) if isinstance(value, Decimal) else value
                for key, value in project_public_summary.items()
            },
            **{
                key: _f(value) if isinstance(value, Decimal) else value
                for key, value in project_dual_summary.items()
            },
        })
        g["order_count"] += r.order_count
        g["missing_detail_orders"] += r.missing_detail_orders
        g["lines"] += r.lines
        for key in (
            "actual_cost_inc", "actual_cost_ex",
            "estimated_cost_inc", "estimated_cost_ex",
        ):
            g[key] += project_summary[key]
        for key in ("actual_lines", "estimated_lines", "missing_cost_lines"):
            g[key] += project_summary[key]
        for basis in ("inc", "ex"):
            prefix = f"parts_cost_{basis}_tax"
            g[prefix] += Decimal(getattr(r, prefix))
            g[f"{prefix}_actual_lines"] += getattr(
                r, f"{prefix}_actual_lines"
            )
            g[f"{prefix}_estimated_lines"] += getattr(
                r, f"{prefix}_estimated_lines"
            )
        g["low_conf"] += Decimal(r.low_conf)
        g["mend_missing"] += r.mend_missing
        for k, v in (("mstart", r.mstart), ("mend", r.mend)):
            if v is not None and (g[k] is None or (v < g[k] if k == "mstart" else v > g[k])):
                g[k] = v
        for k, v, fn in (("first_out", r.first_out, min), ("last_out", r.last_out, max)):
            if v is not None:
                g[k] = v if g[k] is None else fn(g[k], v)

    contract_nos = [contract_no for contract_no in groups if contract_no]
    revenue_evidence = maintenance_margin_evidence.load_contract_revenue_evidence(
        db,
        contract_nos,
    )
    expense_evidence = maintenance_margin_evidence.load_untyped_expense_evidence(
        db,
        contract_nos,
        date_from=_effective_cost_date_from(date_from),
        date_to=date_to,
    )
    expense_snapshot_complete = (
        maintenance_margin_evidence.load_expense_snapshot_completeness(
            db,
            contract_nos,
            required_through=date_to or as_of,
        )
    )
    # 预算/含税收入只认当前合同台账 amount_inc_tax；销售表仍只为未税毛利证据，
    # 绝不能再用固定税率猜出含税合同额覆盖工作簿修改。
    contracts = _contract_amounts(db, contract_nos)
    expenses = {
        contract_no: evidence.legacy_raw_total
        for contract_no, evidence in expense_evidence.items()
    }
    warn = Decimal(str(config.MAINT_BUDGET_WARN_PCT))
    rows = []
    for cno, g in groups.items():
        cost_summary = g["single_project_summary"]
        if cost_summary is None:
            cost_summary = _normalized_cost_summary(
                lines=g["lines"],
                actual_cost_inc=g["actual_cost_inc"],
                actual_cost_ex=g["actual_cost_ex"],
                estimated_cost_inc=g["estimated_cost_inc"],
                estimated_cost_ex=g["estimated_cost_ex"],
                actual_lines=g["actual_lines"],
                estimated_lines=g["estimated_lines"],
            )
        if g["missing_detail_orders"]:
            cost_summary["cost_quality"] = "incomplete"
        has_parts_lines = g["lines"] > 0
        spent_parts = (
            cost_summary["known_cost_total"]
            if has_parts_lines else None
        )
        public_cost_summary = dict(cost_summary)
        if not has_parts_lines:
            for field in (
                "actual_cost_inc",
                "actual_cost_ex",
                "estimated_cost_inc",
                "estimated_cost_ex",
                "known_cost_total",
            ):
                public_cost_summary[field] = None
        dual_summary = {}
        for basis in ("inc", "ex"):
            prefix = f"parts_cost_{basis}_tax"
            dual_summary.update(_parts_tax_basis_summary(
                basis=basis,
                lines=g["lines"],
                amount=g[prefix],
                actual_lines=g[f"{prefix}_actual_lines"],
                estimated_lines=g[f"{prefix}_estimated_lines"],
            ))
            if g["missing_detail_orders"]:
                dual_summary[f"{prefix}_complete"] = False
                dual_summary[f"{prefix}_quality"] = "incomplete"
        expense = (expenses.get(cno) or _ZERO).quantize(_CENT)
        budget = contracts.get(cno) if cno else None
        contract_revenue = revenue_evidence.get(cno)
        contract_expense = expense_evidence.get(cno)
        expense_data_available = expense_snapshot_complete.get(cno, False)
        expense_evidence_status = (
            maintenance_margin_evidence.expense_evidence_status(
                contract_expense,
                data_available=expense_data_available,
            )
        )
        margin_result = maintenance_margin.calculate_contract_margin(
            revenue_inc=budget,
            revenue_ex=(
                contract_revenue.revenue_ex
                if contract_revenue is not None else None
            ),
            tax_rate=(
                contract_revenue.tax_rate
                if contract_revenue is not None else None
            ),
            parts_cost_inc_tax=dual_summary["parts_cost_inc_tax"],
            parts_cost_ex_tax=dual_summary["parts_cost_ex_tax"],
            cost_quality_inc=dual_summary["parts_cost_inc_tax_quality"],
            cost_quality_ex=dual_summary["parts_cost_ex_tax_quality"],
            expense_inc=(
                contract_expense.expense_inc
                if contract_expense is not None else _ZERO
            ),
            expense_ex=(
                contract_expense.expense_ex
                if contract_expense is not None else _ZERO
            ),
            expense_data_available=expense_data_available,
            date_filtered=date_filtered,
            # 含税收入只认 canonical current amount_inc_tax；销售表的重复/税率
            # 证据只影响未税侧，不能覆盖或污染含税侧。
            revenue_ambiguous_inc=False,
            revenue_ambiguous_ex=(
                contract_revenue.ambiguous_ex
                if contract_revenue is not None else False
            ),
        )
        decision = maintenance_cost_quality.budget_decision(
            cost_summary,
            budget=budget,
            expense_total=expense,
            expense_data_available=expense_data_available,
            warn_pct=warn,
        )
        decision = _date_filtered_budget_decision(
            decision,
            date_filtered=date_filtered,
        )
        st = decision["decision_status"]
        remaining = decision["remaining"]
        remaining_pct = decision["remaining_pct"]
        spent = decision["known_spend_total"]
        if len(g["projects"]) > 1:
            if cost_restricted:
                g["projects"].sort(key=lambda p: (p["project"] or "").casefold())
            else:
                g["projects"].sort(key=lambda p: -(p["spent_parts"] or 0))
        lifecycle_status = _lifecycle_status(g["mend_missing"], g["mend"], as_of)
        rows.append({
            "contract": cno or None,
            "decision_status": st,
            "status": st,  # 兼容一版：与 decision_status 同一计算结果
            "projects": g["projects"],
            "order_count": g["order_count"],
            "missing_detail_orders": g["missing_detail_orders"],
            "lines": g["lines"],
            **{
                key: _f(value) if isinstance(value, Decimal) else value
                for key, value in public_cost_summary.items()
            },
            **{
                key: _f(value) if isinstance(value, Decimal) else value
                for key, value in dual_summary.items()
            },
            **{
                key: _f(value) if isinstance(value, Decimal) else value
                for key, value in margin_result.items()
            },
            "coverage_pct": round(
                (g["actual_lines"] + g["estimated_lines"]) / g["lines"] * 100, 1,
            ) if g["lines"] else None,
            "spent_parts": _f(spent_parts),
            "spent_expense": _f(expense) if expense_data_available else None,
            "spent": (
                _f(spent)
                if has_parts_lines and expense_data_available
                else None
            ),
            "expense_data_available": expense_data_available,
            "expense_evidence_status": expense_evidence_status,
            "budget": _f(budget), "remaining": _f(remaining),
            "remaining_pct": _f(remaining_pct),
            # 低置信成本占比高 → 卡片提示"估算成分高"
            "low_conf_pct": (
                round(float(g["low_conf"] / spent_parts * 100), 1)
                if spent_parts else None
            ),
            "maint_start": g["mstart"].isoformat() if g["mstart"] else None,
            "maint_end": (
                g["mend"].isoformat()
                if lifecycle_status != "missing" and g["mend"] else None
            ),
            "lifecycle_status": lifecycle_status,
            "first_out": g["first_out"].isoformat() if g["first_out"] else None,
            "last_out": g["last_out"].isoformat() if g["last_out"] else None,
        })
    order = {
        "incomplete_cost": 0,
        "expense_data_unavailable": 1,
        "filtered_scope": 2,
        "red": 3,
        "yellow": 4,
        "green": 5,
        "no_budget": 6,
    }
    decision_restricted = profit_restricted or cost_restricted
    if decision_restricted:
        # status 与 status_counts 本身就是盈亏结论；即使金额随后被 mask，红黄绿归属、
        # 状态筛选及排序仍会泄漏。受限用户忽略 status 请求，移除分类结构并按最近出库
        # 日期稳定排序。Agent 与 API 共用本函数，不得各自遗漏。
        lifecycle_counts = {
            lifecycle_status: sum(
                1 for row in rows if row["lifecycle_status"] == lifecycle_status
            )
            for lifecycle_status in ("ongoing", "ended", "missing")
        }
        if lifecycle != "all":
            rows = [row for row in rows if row["lifecycle_status"] == lifecycle]
        rows.sort(
            key=lambda r: (
                r["last_out"] is not None,
                r["last_out"] or "",
                r["contract"] or "",
            ),
            reverse=True,
        )
        effective_sort = "last_out"
        for row in rows:
            row.pop("status", None)
            row.pop("decision_status", None)
            if profit_restricted:
                for field in (
                    "budget", "remaining", "remaining_pct",
                    "revenue_inc", "revenue_ex",
                    "parts_gross_profit_inc", "parts_gross_profit_ex",
                    "parts_gross_margin_inc", "parts_gross_margin_ex",
                    "parts_profit_status_inc", "parts_profit_status_ex",
                    "contribution_profit_inc", "contribution_profit_ex",
                    "contribution_margin_inc", "contribution_margin_ex",
                    "contribution_status_inc", "contribution_status_ex",
                    "expense_inc", "expense_ex",
                ):
                    row[field] = None
        return {
            "rows": rows,
            "profit_restricted": profit_restricted,
            "decision_restricted": True,
            "ranking_restricted": True,
            "effective_sort": effective_sort,
            "status_filter_applied": False,
            "warn_pct": float(warn),
            "start_date": config.MAINT_COST_START_DATE.isoformat(),
            "as_of": as_of.isoformat(),
            "lifecycle_filter": lifecycle,
            "lifecycle_counts": lifecycle_counts,
        }
    rows.sort(key=lambda r: (
        order[r["status"]],
        -(r["spent"] if r["spent"] is not None else r["spent_parts"] or 0),
        r["contract"] or "",
    ))
    if status:
        rows = [r for r in rows if r["decision_status"] == status]
    lifecycle_counts = {
        lifecycle_status: sum(
            1 for row in rows if row["lifecycle_status"] == lifecycle_status
        )
        for lifecycle_status in ("ongoing", "ended", "missing")
    }
    if lifecycle != "all":
        rows = [row for row in rows if row["lifecycle_status"] == lifecycle]
    counts = {s: sum(1 for r in rows if r["decision_status"] == s) for s in order}
    legacy_counts = {
        status_name: counts[status_name]
        for status_name in ("red", "yellow", "green", "no_budget")
    }
    return {"rows": rows, "status_counts": legacy_counts,
            "decision_status_counts": counts,
            "profit_restricted": False, "decision_restricted": False,
            "ranking_restricted": False,
            "effective_sort": "status_then_spent", "status_filter_applied": bool(status),
            "warn_pct": float(warn), "start_date": config.MAINT_COST_START_DATE.isoformat(),
            "as_of": as_of.isoformat(), "lifecycle_filter": lifecycle,
            "lifecycle_counts": lifecycle_counts}


def contract_workbook_data(
    db: Session,
    contract: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """§16.4 工作簿导出数据：合同抬头 + 月度×分类汇总 + 出库明细(单据级回填) + 报销明细。"""
    date_filtered = date_from is not None or date_to is not None
    ml, mo = FMaintenanceLine, FMaintenanceOrder
    stmt = (
        select(mo, ml)
        .select_from(mo)
        .outerjoin(ml, ml.order_id == mo.id)
        .where(
            mo.linked_sales_order_no == contract,
            # 2026-08-19：作废明细行不进合同工作簿；无明细单的占位行保留（#55）
            or_(ml.id.is_(None), ml.is_active.is_(True)),
        )
    )
    stmt = _scoped_filters(stmt, date_from, date_to)
    stmt = stmt.order_by(
        mo.order_date.asc().nullslast(),
        mo.order_no,
        ml.line_no.asc().nullslast(),
        ml.id,
    )
    selected = db.execute(stmt).all()
    orders = []
    seen_order_ids: set[int] = set()
    lines = []
    for order, line in selected:
        if order.id not in seen_order_ids:
            seen_order_ids.add(order.id)
            orders.append(order)
        if line is not None:
            lines.append((line, order))
    order_count = len(orders)
    orders_with_details = len({order.id for _line, order in lines})
    missing_detail_orders = order_count - orders_with_details

    line_ids = [line.id for line, _order in lines]
    active_overrides = {
        override.line_id: override
        for override in db.scalars(
            select(MaintenanceManualCostOverride).where(
                MaintenanceManualCostOverride.line_id.in_(line_ids),
                MaintenanceManualCostOverride.active.is_(True),
            )
        )
    } if line_ids else {}
    line_cost_display = {}
    for line, _order in lines:
        override = active_overrides.get(line.id)
        line_cost_display[line.id] = maintenance_cost_quality.resolved_line_cost_fields(
            source=line.cost_source,
            tax_basis=line.cost_tax_basis,
            legacy_unit_cost=line.unit_cost,
            legacy_amount=line.cost_amount,
            unit_cost_inc_tax=line.unit_cost_inc_tax,
            unit_cost_ex_tax=line.unit_cost_ex_tax,
            cost_amount_inc_tax=line.cost_amount_inc_tax,
            cost_amount_ex_tax=line.cost_amount_ex_tax,
            anomaly_flags=line.anomaly_flags,
            confidence=line.confidence,
            qty=line.qty,
            return_qty=line.return_qty,
            manual_unit_cost_inc_tax=(
                override.unit_cost_inc_tax if override is not None else None
            ),
            manual_unit_cost_ex_tax=(
                override.unit_cost_ex_tax if override is not None else None
            ),
            manual_active=override is not None and override.active is True,
        )

    # 单据级已知成本参考；脏历史行同样走统一 fail-closed 分类，未知来源金额不纳入。
    doc_total: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    doc_records: dict[str, list] = defaultdict(list)
    for ln, o in lines:
        fact = line_cost_display[ln.id]
        record = (fact["source"], fact["tax_basis"], fact["cost_amount"])
        doc_records[o.order_no].append(record)
        if fact["tier"] != "missing":
            doc_total[o.order_no] += fact["cost_amount"]
    doc_cost_summary = {
        order_no: maintenance_cost_quality.summarize_records(records)
        for order_no, records in doc_records.items()
    }
    line_cost_tiers = {
        ln.id: line_cost_display[ln.id]["tier"]
        for ln, _order in lines
    }
    cost_summary = maintenance_cost_quality.summarize_records(
        (
            line_cost_display[ln.id]["source"],
            line_cost_display[ln.id]["tax_basis"],
            line_cost_display[ln.id]["cost_amount"],
        )
        for ln, _order in lines
    )
    if missing_detail_orders:
        cost_summary["cost_quality"] = "incomplete"
    dual_cost_summary = {}
    for basis, field in (
        ("inc", "cost_amount_inc_tax"),
        ("ex", "cost_amount_ex_tax"),
    ):
        actual_lines = estimated_lines = 0
        amount = _ZERO
        for ln, _order in lines:
            fact = line_cost_display[ln.id]
            value = fact[field]
            tier = fact[f"{basis}_tier"]
            if tier == "missing":
                continue
            amount += Decimal(value)
            if tier == "actual":
                actual_lines += 1
            else:
                estimated_lines += 1
        dual_cost_summary.update(_parts_tax_basis_summary(
            basis=basis,
            lines=len(lines),
            amount=amount,
            actual_lines=actual_lines,
            estimated_lines=estimated_lines,
        ))
        if missing_detail_orders:
            prefix = f"parts_cost_{basis}_tax"
            dual_cost_summary[f"{prefix}_complete"] = False
            dual_cost_summary[f"{prefix}_quality"] = "incomplete"

    pe = FProjectExpense
    expense_stmt = select(pe).where(pe.linked_sales_order_no == contract)
    expense_stmt = expense_stmt.where(
        pe.expense_date >= _effective_cost_date_from(date_from),
    )
    if date_to is not None:
        expense_stmt = expense_stmt.where(pe.expense_date <= date_to)
    exp_rows = db.execute(
        expense_stmt.order_by(
            pe.expense_date.asc().nullslast(),
            pe.bxd_no,
            pe.line_no,
            pe.id,
        )
    ).scalars().all()

    # 月度汇总分命名空间保存，避免费用分类与内置的备件成本列同名时互相覆盖。
    monthly_parts: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    monthly_expenses: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: _ZERO),
    )
    monthly_missing: dict[str, int] = defaultdict(int)
    for ln, o in lines:
        if not o.order_date:
            continue
        fact = line_cost_display[ln.id]
        tier = fact["tier"]
        if tier == "missing":
            monthly_missing[_ym(o.order_date)] += 1
        else:
            monthly_parts[_ym(o.order_date)] += fact["cost_amount"]
    for e in exp_rows:
        if (e.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS
                and e.amount is not None and e.expense_date):
            monthly_expenses[_ym(e.expense_date)][
                e.fee_category or "(未分类费用)"
            ] += e.amount

    revenue_evidence = maintenance_margin_evidence.load_contract_revenue_evidence(
        db,
        [contract],
    ).get(contract)
    budget = _contract_amounts(db, [contract]).get(contract)
    # canonical amount_inc_tax does not prove a tax rate; do not synthesize 13%.
    contract_tax_rate = None
    contract_tax_status = "available" if budget is not None else "missing"
    expense_total = sum(
        (
            expense.amount
            for expense in exp_rows
            if expense.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS
            and expense.amount is not None
        ),
        _ZERO,
    )
    expense_data_available = (
        maintenance_margin_evidence.load_expense_snapshot_completeness(
            db,
            [contract],
            required_through=date_to or business_today(),
        ).get(contract, False)
    )
    decision = maintenance_cost_quality.budget_decision(
        cost_summary,
        budget=budget,
        expense_total=expense_total,
        expense_data_available=expense_data_available,
        warn_pct=Decimal(str(config.MAINT_BUDGET_WARN_PCT)),
    )
    decision = _date_filtered_budget_decision(
        decision,
        date_filtered=date_filtered,
    )
    active_expense_evidence = maintenance_margin_evidence.summarize_expense_records(
        (
            expense.amount,
            expense.amount_ex_tax,
            expense.amount_inc_tax,
        )
        for expense in exp_rows
        if expense.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS
    )
    expense_evidence_status = maintenance_margin_evidence.expense_evidence_status(
        active_expense_evidence,
        data_available=expense_data_available,
    )
    if not expense_data_available:
        expense_inc = None
        expense_ex = None
    elif active_expense_evidence is None:
        expense_inc = _ZERO
        expense_ex = _ZERO
    else:
        expense_inc = active_expense_evidence.expense_inc
        expense_ex = active_expense_evidence.expense_ex
    margin_result = maintenance_margin.calculate_contract_margin(
        revenue_inc=budget,
        revenue_ex=(
            revenue_evidence.revenue_ex
            if revenue_evidence is not None else None
        ),
        tax_rate=(
            revenue_evidence.tax_rate
            if revenue_evidence is not None else None
        ),
        parts_cost_inc_tax=dual_cost_summary["parts_cost_inc_tax"],
        parts_cost_ex_tax=dual_cost_summary["parts_cost_ex_tax"],
        cost_quality_inc=dual_cost_summary["parts_cost_inc_tax_quality"],
        cost_quality_ex=dual_cost_summary["parts_cost_ex_tax_quality"],
        expense_inc=(
            active_expense_evidence.expense_inc
            if active_expense_evidence is not None else _ZERO
        ),
        expense_ex=(
            active_expense_evidence.expense_ex
            if active_expense_evidence is not None else _ZERO
        ),
        expense_data_available=expense_data_available,
        date_filtered=date_filtered,
        revenue_ambiguous_inc=False,
        revenue_ambiguous_ex=(
            revenue_evidence.ambiguous_ex
            if revenue_evidence is not None else False
        ),
    )
    return {"contract": contract, "budget": budget,
            "date_filtered": date_filtered,
            "contract_tax_rate": contract_tax_rate,
            "contract_tax_status": contract_tax_status,
            "orders": orders,
            "order_count": order_count,
            "orders_with_details": orders_with_details,
            "missing_detail_orders": missing_detail_orders,
            "structure_complete": missing_detail_orders == 0,
            "lines": lines, "doc_total": doc_total,
            "doc_cost_summary": doc_cost_summary,
            "line_cost_tiers": line_cost_tiers,
            "line_cost_display": line_cost_display,
            "cost_summary": cost_summary,
            "dual_cost_summary": dual_cost_summary,
            "margin": margin_result,
            "decision": decision,
            "expense_data_available": expense_data_available,
            "expense_inc": expense_inc,
            "expense_ex": expense_ex,
            "expense_evidence_status": expense_evidence_status,
            "expenses": exp_rows, "expense_total": expense_total,
            "monthly_parts": dict(monthly_parts),
            "monthly_expenses": {
                year_month: dict(categories)
                for year_month, categories in monthly_expenses.items()
            },
            "monthly_missing": dict(monthly_missing)}
