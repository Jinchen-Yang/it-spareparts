"""维保成本事实分层与预算决策门禁的单一真值。

本模块只解释已经由 ``maintenance_cost.recompute`` 产出的成本结果，不改变取价
瀑布。所有项目、合同、CSV、工作簿与 Agent 都应复用这里的分类和决策，避免各自把
估算价或缺失价重新解释成经营结论。
"""
from decimal import Decimal
from typing import Iterable

from sqlalchemy import and_, or_


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
