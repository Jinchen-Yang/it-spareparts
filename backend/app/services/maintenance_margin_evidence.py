"""合同毛利所需的数据库证据批量装载。

本模块只装载收入与当前无法拆税的报销证据，不计算毛利。查询结果对冲突的重复
XSDD fail closed，避免沿用“取最大值”后把未确认版本误标成正式收入。
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models.maintenance import FProjectExpense
from app.models.sales import FSalesOrder
from app.services.query_filters import active_orders


_ZERO = Decimal("0")


@dataclass(frozen=True)
class RevenueEvidence:
    revenue_ex: Decimal | None
    tax_rate: Decimal | None
    tax_rate_ambiguous: bool
    ambiguous_inc: bool
    ambiguous_ex: bool
    record_count: int
    legacy_contract_amount_inc: Decimal | None


@dataclass(frozen=True)
class ExpenseEvidence:
    legacy_raw_total: Decimal
    unknown_tax_total: Decimal | None
    record_count: int


def _decimal_key(value: Decimal | None):
    if value is None:
        return None
    normalized = Decimal(value)
    if not normalized.is_finite():
        return ("invalid", str(normalized))
    return normalized.normalize()


def _legacy_contract_amount_inc(
    amount: Decimal | None,
    tax_rate: Decimal | None,
) -> Decimal | None:
    if amount is None:
        return None
    amount_value = Decimal(amount)
    rate_value = Decimal(tax_rate) if tax_rate else _ZERO
    if not amount_value.is_finite() or not rate_value.is_finite():
        return None
    return (
        amount_value * (Decimal("1") + rate_value)
    ).quantize(Decimal("0.01"))


def summarize_revenue_candidates(
    candidates: Iterable[tuple[Decimal | None, Decimal | None]],
) -> RevenueEvidence | None:
    """把同一 XSDD 的全部有效记录归并为按税口径独立的证据。"""
    candidates = list(candidates)
    if not candidates:
        return None
    legacy_values = [
        legacy
        for amount, tax_rate in candidates
        if (legacy := _legacy_contract_amount_inc(amount, tax_rate)) is not None
    ]
    # 兼容旧预算看板：历史实现以 0 为 max 初值，负向销售/冲销不会生成负预算。
    legacy_contract_amount_inc = (
        max([_ZERO, *legacy_values])
        if legacy_values else None
    )
    distinct_ex = {
        _decimal_key(amount)
        for amount, _tax_rate in candidates
    }
    distinct_inc = {
        (_decimal_key(amount), _decimal_key(tax_rate))
        for amount, tax_rate in candidates
    }
    distinct_tax_rates = {
        _decimal_key(tax_rate)
        for _amount, tax_rate in candidates
    }
    ambiguous_ex = len(distinct_ex) != 1
    ambiguous_inc = len(distinct_inc) != 1
    tax_rate_ambiguous = len(distinct_tax_rates) != 1
    amount_ex_tax = None if ambiguous_ex else candidates[0][0]
    tax_rate = None if tax_rate_ambiguous else candidates[0][1]
    return RevenueEvidence(
        revenue_ex=amount_ex_tax,
        tax_rate=tax_rate,
        tax_rate_ambiguous=tax_rate_ambiguous,
        ambiguous_inc=ambiguous_inc,
        ambiguous_ex=ambiguous_ex,
        record_count=len(candidates),
        legacy_contract_amount_inc=legacy_contract_amount_inc,
    )


def summarize_expense_amounts(
    amounts: Iterable[Decimal | None],
) -> ExpenseEvidence | None:
    """把生效报销金额归并为兼容净额与税务未知门禁。"""
    amounts = list(amounts)
    if not amounts:
        return None
    invalid = any(
        amount is None or not Decimal(amount).is_finite()
        for amount in amounts
    )
    normalized_nonnull = [
        Decimal(amount)
        for amount in amounts
        if amount is not None
    ]
    legacy_raw_total = sum(normalized_nonnull, _ZERO)
    unknown_tax_total = (
        None
        if invalid
        else sum((abs(amount) for amount in normalized_nonnull), _ZERO)
    )
    return ExpenseEvidence(
        legacy_raw_total=legacy_raw_total,
        unknown_tax_total=unknown_tax_total,
        record_count=len(amounts),
    )


def load_contract_revenue_evidence(
    db: Session,
    contract_nos: list[str],
) -> dict[str, RevenueEvidence]:
    """一次查询装载 XSDD 收入；相互冲突的重复记录不选择任一版本。"""
    contract_nos = sorted({value for value in contract_nos if value})
    if not contract_nos:
        return {}
    stmt = active_orders(
        select(
            FSalesOrder.order_no,
            FSalesOrder.amount_ex_tax,
            FSalesOrder.tax_rate,
        ).where(FSalesOrder.order_no.in_(contract_nos)),
        FSalesOrder,
    )
    records: dict[str, list[tuple[Decimal | None, Decimal | None]]] = defaultdict(list)
    for order_no, amount_ex_tax, tax_rate in db.execute(stmt):
        records[order_no].append((amount_ex_tax, tax_rate))

    result: dict[str, RevenueEvidence] = {}
    for contract_no, candidates in records.items():
        evidence = summarize_revenue_candidates(candidates)
        if evidence is not None:
            result[contract_no] = evidence
    return result


def load_untyped_expense_evidence(
    db: Session,
    contract_nos: list[str],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, ExpenseEvidence]:
    """一次查询装载生效报销。

    ``unknown_tax_total`` 使用绝对值累计，仅作为“存在未拆税费用”的门禁；正负报销
    即使净额抵消也不能绕过门禁。任一金额为空或非有限数时返回 ``None``。
    """
    contract_nos = sorted({value for value in contract_nos if value})
    if not contract_nos:
        return {}
    stmt = (
        select(FProjectExpense.linked_sales_order_no, FProjectExpense.amount)
        .where(
            FProjectExpense.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS,
            FProjectExpense.linked_sales_order_no.in_(contract_nos),
        )
    )
    if date_from is not None:
        stmt = stmt.where(FProjectExpense.expense_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(FProjectExpense.expense_date <= date_to)
    records: dict[str, list[Decimal | None]] = defaultdict(list)
    for contract_no, amount in db.execute(stmt):
        records[contract_no].append(amount)

    result: dict[str, ExpenseEvidence] = {}
    for contract_no, amounts in records.items():
        evidence = summarize_expense_amounts(amounts)
        if evidence is not None:
            result[contract_no] = evidence
    return result
