"""维保成本事实分层与预算决策门禁的单一真值。

本模块只解释已经由 ``maintenance_cost.recompute`` 产出的成本结果，不改变取价
瀑布。所有项目、合同、CSV、工作簿与 Agent 都应复用这里的分类和决策，避免各自把
估算价或缺失价重新解释成经营结论。
"""
from decimal import Decimal
from typing import Iterable

from sqlalchemy import and_, case, false, func, or_, true


ACTUAL_SOURCES = frozenset({"direct", "window", "month_avg", "manual"})
ESTIMATED_SOURCES = frozenset({
    "trace_avg",
    "sales_ref",
    "pool_purchase",
    "pool_sales",
    "purchase_history",
    "sales_history",
})
KNOWN_SOURCES = ACTUAL_SOURCES | ESTIMATED_SOURCES
TAX_BASES = frozenset({"inc", "ex"})

COST_BUCKET_MISSING = 0
COST_BUCKET_ACTUAL_INC = 1
COST_BUCKET_ACTUAL_EX = 2
COST_BUCKET_ESTIMATED_INC_LOW = 3
COST_BUCKET_ESTIMATED_INC_OTHER = 4
COST_BUCKET_ESTIMATED_EX_LOW = 5
COST_BUCKET_ESTIMATED_EX_OTHER = 6

ACTUAL_BUCKETS = frozenset({
    COST_BUCKET_ACTUAL_INC,
    COST_BUCKET_ACTUAL_EX,
})
ESTIMATED_BUCKETS = frozenset({
    COST_BUCKET_ESTIMATED_INC_LOW,
    COST_BUCKET_ESTIMATED_INC_OTHER,
    COST_BUCKET_ESTIMATED_EX_LOW,
    COST_BUCKET_ESTIMATED_EX_OTHER,
})
KNOWN_BUCKETS = ACTUAL_BUCKETS | ESTIMATED_BUCKETS
INC_BUCKETS = frozenset({
    COST_BUCKET_ACTUAL_INC,
    COST_BUCKET_ESTIMATED_INC_LOW,
    COST_BUCKET_ESTIMATED_INC_OTHER,
})
EX_BUCKETS = frozenset({
    COST_BUCKET_ACTUAL_EX,
    COST_BUCKET_ESTIMATED_EX_LOW,
    COST_BUCKET_ESTIMATED_EX_OTHER,
})
ESTIMATED_LOW_BUCKETS = frozenset({
    COST_BUCKET_ESTIMATED_INC_LOW,
    COST_BUCKET_ESTIMATED_EX_LOW,
})
COST_DERIVED_ANOMALY_FLAGS = frozenset({
    "no_cost",
    "cost_overflow",
    "tax_rate_estimated",
    "inc_tax_estimated",
    "ex_tax_estimated",
    "stale_cost_reference",
    "reference_date_missing",
})

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_MAX_AMOUNT_EXCLUSIVE = Decimal("1000000000000")


def source_tier(
    source: str | None,
    tax_basis: str | None,
    amount: Decimal | None,
) -> str:
    """把单行归为 actual / estimated / missing。

    历史脏数据必须 fail-closed：未知/空来源、空金额、未知税口径都算 missing；即便未知
    来源携带金额，也绝不把它纳入已知成本参考。
    """
    if amount is None or tax_basis not in TAX_BASES:
        return "missing"
    try:
        amount_value = Decimal(amount)
    except (ArithmeticError, TypeError, ValueError):
        return "missing"
    if (
        not amount_value.is_finite()
        or amount_value < _ZERO
        or amount_value >= _MAX_AMOUNT_EXCLUSIVE
    ):
        return "missing"
    if source in ACTUAL_SOURCES:
        return "actual"
    if source in ESTIMATED_SOURCES:
        return "estimated"
    return "missing"


def normalized_tax_tier(
    *,
    source: str | None,
    tax_basis: str | None,
    legacy_amount: Decimal | None,
    normalized_amount: Decimal | None,
    normalized_basis: str,
    anomaly_flags: Iterable[str] | None,
) -> str:
    """解释一行归一双税成本的 actual / estimated / missing。

    legacy 来源本身必须先是已知成本；未知来源即使夹带双税金额或估算 flag 也不能
    被提升。实际来源只在该目标税口径经过缺税率换算时降为 estimated。
    """
    legacy_tier = source_tier(source, tax_basis, legacy_amount)
    if legacy_tier == "missing" or normalized_basis not in TAX_BASES:
        return "missing"
    try:
        value = Decimal(normalized_amount) if normalized_amount is not None else None
    except (ArithmeticError, TypeError, ValueError):
        return "missing"
    if (
        value is None
        or not value.is_finite()
        or value < _ZERO
        or value >= _MAX_AMOUNT_EXCLUSIVE
    ):
        return "missing"
    if legacy_tier == "estimated":
        return "estimated"
    flags = frozenset(anomaly_flags or ())
    return (
        "estimated"
        if f"{normalized_basis}_tax_estimated" in flags
        else "actual"
    )


def effective_manual_cost(
    *,
    qty: Decimal | None,
    return_qty: Decimal | None,
    unit_cost: Decimal | None,
) -> Decimal | None:
    """Resolve one active manual override with the canonical net-quantity rule.

    A real zero is evidence (``qty == return_qty`` or ``unit_cost == 0``), while
    a missing/invalid quantity or unit price remains missing.  This mirrors the
    SQL helper below so workbook/API renderers cannot drift from aggregations.
    """
    try:
        quantity = Decimal(qty) if qty is not None else None
        returned = Decimal(return_qty or _ZERO)
        unit = Decimal(unit_cost) if unit_cost is not None else None
    except (ArithmeticError, TypeError, ValueError):
        return None
    if quantity is None or unit is None:
        return None
    if (
        not quantity.is_finite()
        or not returned.is_finite()
        or not unit.is_finite()
        or quantity < _ZERO
        or returned < _ZERO
        or unit < _ZERO
        or quantity >= _MAX_AMOUNT_EXCLUSIVE
        or returned >= _MAX_AMOUNT_EXCLUSIVE
        or unit >= _MAX_AMOUNT_EXCLUSIVE
    ):
        return None
    amount = unit * max(quantity - returned, _ZERO)
    if not amount.is_finite() or amount >= _MAX_AMOUNT_EXCLUSIVE:
        return None
    return amount.quantize(_CENT)


def normalized_line_cost(
    *,
    source: str | None,
    tax_basis: str | None,
    legacy_amount: Decimal | None,
    normalized_amount: Decimal | None,
    normalized_basis: str,
    anomaly_flags: Iterable[str] | None,
    qty: Decimal | None,
    return_qty: Decimal | None,
    manual_unit_cost: Decimal | None,
    manual_active: bool,
) -> dict:
    """Return one normalized cost fact, including the active-manual fallback.

    Automatic evidence always wins.  A manual override is eligible only for an
    unresolved ``NULL``/``none`` source; it cannot launder an unknown dirty
    source into a known cost.  The returned shape is intentionally small so it
    can be shared by workbooks, row APIs, and evidence drill-downs.
    """
    tier = normalized_tax_tier(
        source=source,
        tax_basis=tax_basis,
        legacy_amount=legacy_amount,
        normalized_amount=normalized_amount,
        normalized_basis=normalized_basis,
        anomaly_flags=anomaly_flags,
    )
    if tier != "missing":
        return {
            "amount": Decimal(normalized_amount).quantize(_CENT),
            "tier": tier,
            "source": source,
        }
    if manual_active and source in (None, "none"):
        amount = effective_manual_cost(
            qty=qty,
            return_qty=return_qty,
            unit_cost=manual_unit_cost,
        )
        if amount is not None:
            return {"amount": amount, "tier": "actual", "source": "manual"}
    return {"amount": None, "tier": "missing", "source": source}


def resolved_line_cost_fields(
    *,
    source: str | None,
    tax_basis: str | None,
    legacy_unit_cost: Decimal | None,
    legacy_amount: Decimal | None,
    unit_cost_inc_tax: Decimal | None,
    unit_cost_ex_tax: Decimal | None,
    cost_amount_inc_tax: Decimal | None,
    cost_amount_ex_tax: Decimal | None,
    anomaly_flags: Iterable[str] | None,
    confidence: str | None,
    qty: Decimal | None,
    return_qty: Decimal | None,
    manual_unit_cost_inc_tax: Decimal | None,
    manual_unit_cost_ex_tax: Decimal | None,
    manual_active: bool,
) -> dict:
    """Resolve the row-shaped cost view shared by exports and workbooks.

    Some historical active manual overrides predate the recompute that mirrors
    them into ``f_maintenance_line``.  Read consumers must therefore merge the
    override at query time, using exactly the same net-quantity and automatic-
    evidence precedence as the aggregate helpers.  This function intentionally
    returns a display projection and never mutates the persisted line.
    """
    inc = normalized_line_cost(
        source=source,
        tax_basis=tax_basis,
        legacy_amount=legacy_amount,
        normalized_amount=cost_amount_inc_tax,
        normalized_basis="inc",
        anomaly_flags=anomaly_flags,
        qty=qty,
        return_qty=return_qty,
        manual_unit_cost=manual_unit_cost_inc_tax,
        manual_active=manual_active,
    )
    ex = normalized_line_cost(
        source=source,
        tax_basis=tax_basis,
        legacy_amount=legacy_amount,
        normalized_amount=cost_amount_ex_tax,
        normalized_basis="ex",
        anomaly_flags=anomaly_flags,
        qty=qty,
        return_qty=return_qty,
        manual_unit_cost=manual_unit_cost_ex_tax,
        manual_active=manual_active,
    )
    manual_fallback = (
        source in (None, "none")
        and inc["source"] == "manual"
        and inc["tier"] == "actual"
        and ex["source"] == "manual"
        and ex["tier"] == "actual"
    )
    if manual_fallback:
        return {
            "tier": "actual",
            "inc_tier": "actual",
            "ex_tier": "actual",
            "source": "manual",
            "tax_basis": "ex",
            "confidence": "high",
            "unit_cost": Decimal(manual_unit_cost_ex_tax).quantize(_CENT),
            "cost_amount": ex["amount"],
            "unit_cost_inc_tax": Decimal(manual_unit_cost_inc_tax).quantize(_CENT),
            "unit_cost_ex_tax": Decimal(manual_unit_cost_ex_tax).quantize(_CENT),
            "cost_amount_inc_tax": inc["amount"],
            "cost_amount_ex_tax": ex["amount"],
            "anomaly_flags": [
                flag for flag in (anomaly_flags or ())
                if flag not in COST_DERIVED_ANOMALY_FLAGS
            ],
            "manual_fallback": True,
        }

    legacy_tier = source_tier(source, tax_basis, legacy_amount)
    return {
        "tier": legacy_tier,
        "inc_tier": inc["tier"],
        "ex_tier": ex["tier"],
        "source": source,
        "tax_basis": tax_basis,
        "confidence": confidence,
        "unit_cost": legacy_unit_cost if legacy_tier != "missing" else None,
        "cost_amount": legacy_amount if legacy_tier != "missing" else None,
        "unit_cost_inc_tax": (
            unit_cost_inc_tax if inc["tier"] != "missing" else None
        ),
        "unit_cost_ex_tax": (
            unit_cost_ex_tax if ex["tier"] != "missing" else None
        ),
        "cost_amount_inc_tax": inc["amount"],
        "cost_amount_ex_tax": ex["amount"],
        "anomaly_flags": list(anomaly_flags or ()),
        "manual_fallback": False,
    }


def summarize_records(
    records: Iterable[tuple[str | None, str | None, Decimal | None]],
) -> dict:
    """从 ``(cost_source, cost_tax_basis, cost_amount)`` 行构造统一成本摘要。"""
    amounts = {
        "actual_cost_inc": _ZERO,
        "actual_cost_ex": _ZERO,
        "estimated_cost_inc": _ZERO,
        "estimated_cost_ex": _ZERO,
    }
    actual_lines = estimated_lines = missing_cost_lines = 0
    for source, tax_basis, amount in records:
        tier = source_tier(source, tax_basis, amount)
        if tier == "missing":
            missing_cost_lines += 1
            continue
        value = Decimal(amount).quantize(_CENT)
        amounts[f"{tier}_cost_{tax_basis}"] += value
        if tier == "actual":
            actual_lines += 1
        else:
            estimated_lines += 1

    return summarize_aggregate(
        lines=actual_lines + estimated_lines + missing_cost_lines,
        actual_lines=actual_lines,
        estimated_lines=estimated_lines,
        missing_cost_lines=missing_cost_lines,
        **amounts,
    )


def summarize_aggregate(
    *,
    lines: int,
    actual_cost_inc,
    actual_cost_ex,
    estimated_cost_inc,
    estimated_cost_ex,
    actual_lines: int,
    estimated_lines: int,
    missing_cost_lines: int,
) -> dict:
    """把 SQL 聚合值收敛成与内存逐行汇总完全相同的摘要。"""
    if actual_lines + estimated_lines + missing_cost_lines != lines:
        raise ValueError("maintenance cost tier line counts do not add up")
    amounts = {
        "actual_cost_inc": Decimal(actual_cost_inc or _ZERO).quantize(_CENT),
        "actual_cost_ex": Decimal(actual_cost_ex or _ZERO).quantize(_CENT),
        "estimated_cost_inc": Decimal(estimated_cost_inc or _ZERO).quantize(_CENT),
        "estimated_cost_ex": Decimal(estimated_cost_ex or _ZERO).quantize(_CENT),
    }
    quality = (
        "incomplete"
        if lines <= 0 or missing_cost_lines
        else "contains_estimate"
        if estimated_lines
        else "actual_only"
    )
    known_cost_total = sum(amounts.values(), _ZERO).quantize(_CENT)
    return {
        **amounts,
        "actual_lines": actual_lines,
        "estimated_lines": estimated_lines,
        "missing_cost_lines": missing_cost_lines,
        "known_cost_total": known_cost_total,
        "cost_quality": quality,
    }


def sql_amount_is_valid(amount_column):
    """返回与 :func:`source_tier` 一致的金额合法性 SQL 条件。

    聚合查询可先按这个低基数布尔值分组，再在 Python 端复用 ``source_tier`` 完成
    来源/税口径分类，避免 PostgreSQL 为每个 ``FILTER`` 重复求值完整分级表达式。
    """
    return and_(
        amount_column.is_not(None),
        amount_column >= _ZERO,
        amount_column < _MAX_AMOUNT_EXCLUSIVE,
    )


def cost_bucket(
    source: str | None,
    tax_basis: str | None,
    amount: Decimal | None,
    confidence: str | None,
) -> int:
    """把严格成本分类压成聚合列的 smallint；0 永远 fail-closed 为 missing。"""
    tier = source_tier(source, tax_basis, amount)
    if tier == "missing":
        return COST_BUCKET_MISSING
    if tier == "actual":
        return (
            COST_BUCKET_ACTUAL_INC
            if tax_basis == "inc"
            else COST_BUCKET_ACTUAL_EX
        )
    if tax_basis == "inc":
        return (
            COST_BUCKET_ESTIMATED_INC_LOW
            if confidence == "low"
            else COST_BUCKET_ESTIMATED_INC_OTHER
        )
    return (
        COST_BUCKET_ESTIMATED_EX_LOW
        if confidence == "low"
        else COST_BUCKET_ESTIMATED_EX_OTHER
    )


def bucket_tier(bucket: int | None) -> str:
    """反解持久化桶；未知桶同样 fail-closed，避免未来 schema 漂移变成已知成本。"""
    if bucket in ACTUAL_BUCKETS:
        return "actual"
    if bucket in ESTIMATED_BUCKETS:
        return "estimated"
    return "missing"


def sql_tier_predicates(source_column, tax_basis_column, amount_column):
    """返回与 :func:`source_tier` 一致的 actual/estimated/missing SQL 条件。

    ``NOT IN`` 对 NULL 返回 UNKNOWN，因此 missing 条件必须显式包含每个 ``IS NULL``；
    这条约束防止未重算/脏历史行从 ``count(...).filter`` 中静默消失。
    """
    valid_amount_and_basis = and_(
        sql_amount_is_valid(amount_column),
        tax_basis_column.in_(tuple(sorted(TAX_BASES))),
    )
    actual = and_(
        valid_amount_and_basis,
        source_column.in_(tuple(sorted(ACTUAL_SOURCES))),
    )
    estimated = and_(
        valid_amount_and_basis,
        source_column.in_(tuple(sorted(ESTIMATED_SOURCES))),
    )
    missing = or_(
        amount_column.is_(None),
        amount_column < _ZERO,
        amount_column >= _MAX_AMOUNT_EXCLUSIVE,
        tax_basis_column.is_(None),
        tax_basis_column.notin_(tuple(sorted(TAX_BASES))),
        source_column.is_(None),
        source_column.notin_(tuple(sorted(KNOWN_SOURCES))),
    )
    return actual, estimated, missing


def sql_normalized_tax_tier_predicates(
    source_column,
    tax_basis_column,
    legacy_amount_column,
    normalized_amount_column,
    *,
    normalized_basis: str,
    anomaly_flags_column,
):
    """返回与 :func:`normalized_tax_tier` 一致的 SQL 分级条件。

    归一含/未税金额不能仅凭 ``cost_source`` 和自身非空就成为已知成本：它必须先
    通过 legacy 来源、原始税口径与原始金额的严格校验。实际来源若目标税口径由
    缺失税率换算（``<basis>_tax_estimated``），只降级为 estimated，不能计入
    actual。三个返回条件互斥且穷尽，适合直接用于聚合 ``FILTER``/``CASE``。
    """
    if normalized_basis not in TAX_BASES:
        return false(), false(), true()

    legacy_actual, legacy_estimated, legacy_missing = sql_tier_predicates(
        source_column,
        tax_basis_column,
        legacy_amount_column,
    )
    normalized_valid = sql_amount_is_valid(normalized_amount_column)
    normalized_missing = or_(
        normalized_amount_column.is_(None),
        normalized_amount_column < _ZERO,
        normalized_amount_column >= _MAX_AMOUNT_EXCLUSIVE,
    )
    # anomaly_flags 的模型约束为 NOT NULL；COALESCE 仍兼容迁移前历史行，并与
    # normalized_tax_tier(anomaly_flags=None) 将其解释为空集合的行为一致。
    tax_estimated = func.coalesce(
        anomaly_flags_column.any(f"{normalized_basis}_tax_estimated"),
        false(),
    )
    actual = and_(legacy_actual, normalized_valid, ~tax_estimated)
    estimated = and_(
        normalized_valid,
        or_(legacy_estimated, and_(legacy_actual, tax_estimated)),
    )
    missing = or_(legacy_missing, normalized_missing)
    return actual, estimated, missing


def sql_effective_manual_cost(
    *,
    source_column,
    qty_column,
    return_qty_column,
    unit_cost_column,
    active_column,
):
    """SQL twin of :func:`effective_manual_cost` plus source/active gating."""
    effective_qty = func.greatest(
        func.coalesce(qty_column, _ZERO)
        - func.coalesce(return_qty_column, _ZERO),
        _ZERO,
    )
    amount = unit_cost_column * effective_qty
    known = and_(
        or_(source_column.is_(None), source_column == "none"),
        active_column.is_(True),
        sql_amount_is_valid(qty_column),
        sql_amount_is_valid(func.coalesce(return_qty_column, _ZERO)),
        sql_amount_is_valid(unit_cost_column),
        sql_amount_is_valid(amount),
    )
    return amount, known


def sql_normalized_line_cost(
    *,
    source_column,
    tax_basis_column,
    legacy_amount_column,
    normalized_amount_column,
    normalized_basis: str,
    anomaly_flags_column,
    qty_column,
    return_qty_column,
    manual_unit_cost_column,
    manual_active_column,
):
    """Build one normalized amount expression and its strict tier predicates.

    The tuple is ``(amount, actual, estimated, missing)``.  ``amount`` is NULL
    when evidence is missing, but remains numeric zero for a valid zero-cost
    line.  Consumers should aggregate this expression rather than raw
    ``cost_amount_*`` columns.
    """
    automatic_actual, automatic_estimated, _automatic_missing = (
        sql_normalized_tax_tier_predicates(
            source_column,
            tax_basis_column,
            legacy_amount_column,
            normalized_amount_column,
            normalized_basis=normalized_basis,
            anomaly_flags_column=anomaly_flags_column,
        )
    )
    manual_amount, manual_known = sql_effective_manual_cost(
        source_column=source_column,
        qty_column=qty_column,
        return_qty_column=return_qty_column,
        unit_cost_column=manual_unit_cost_column,
        active_column=manual_active_column,
    )
    automatic_known = or_(automatic_actual, automatic_estimated)
    actual = or_(automatic_actual, manual_known)
    estimated = automatic_estimated
    missing = ~or_(actual, estimated)
    amount = case(
        (automatic_known, normalized_amount_column),
        (manual_known, manual_amount),
        else_=None,
    )
    return amount, actual, estimated, missing


def budget_decision(
    summary: dict,
    *,
    budget: Decimal | None,
    expense_total: Decimal = _ZERO,
    expense_data_available: bool = True,
    warn_pct: Decimal,
) -> dict:
    """按成本与费用完整性决定是否允许计算预算余量。"""
    known_spend_total = (
        Decimal(summary["known_cost_total"]) + Decimal(expense_total or _ZERO)
    ).quantize(_CENT)
    if summary["cost_quality"] == "incomplete":
        return {
            "decision_status": "incomplete_cost",
            "known_spend_total": known_spend_total,
            "remaining": None,
            "remaining_pct": None,
        }
    if not expense_data_available:
        return {
            "decision_status": "expense_data_unavailable",
            "known_spend_total": known_spend_total,
            "remaining": None,
            "remaining_pct": None,
        }
    if budget is None or Decimal(budget) <= _ZERO:
        return {
            "decision_status": "no_budget",
            "known_spend_total": known_spend_total,
            "remaining": None,
            "remaining_pct": None,
        }

    budget_value = Decimal(budget).quantize(_CENT)
    remaining = (budget_value - known_spend_total).quantize(_CENT)
    if known_spend_total >= budget_value:
        status = "red"
    elif remaining <= budget_value * Decimal(warn_pct):
        status = "yellow"
    else:
        status = "green"
    return {
        "decision_status": status,
        "known_spend_total": known_spend_total,
        "remaining": remaining,
        "remaining_pct": (remaining / budget_value * Decimal("100")).quantize(_CENT),
    }
