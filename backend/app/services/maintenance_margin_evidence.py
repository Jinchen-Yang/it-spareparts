"""合同毛利所需的数据库证据批量装载。

本模块只装载收入、双口径报销和费用完整水位，不计算毛利。同一 XSDD 的多版本
统一选择最新一条有效导入记录，所有调用方共用同一个排序实现。
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config, tax_policy
from app.models.maintenance import (
    FProjectExpense,
    MaintenanceContractWorkbookState,
)
from app.models.sales import FSalesOrder
from app.models.system import SysImportBatch


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
    expense_inc: Decimal | None
    expense_ex: Decimal | None
    record_count: int

    @property
    def unknown_tax_total(self) -> Decimal | None:
        """迁移兼容：双口径齐全等价于不存在未拆税费用。"""
        return (
            _ZERO
            if self.expense_inc is not None and self.expense_ex is not None
            else None
        )


def expense_evidence_status(
    evidence: ExpenseEvidence | None,
    *,
    data_available: bool,
) -> str:
    """独立解释费用证据，不受成本/收入等贡献毛利主阻断状态影响。"""
    if not data_available:
        return "expense_data_unavailable"
    if evidence is None:
        return "complete"
    if evidence.expense_inc is None or evidence.expense_ex is None:
        return "expense_tax_unknown"
    return "complete"


def _decimal_key(value: Decimal | None):
    if value is None:
        return None
    normalized = Decimal(value)
    if not normalized.is_finite():
        return ("invalid", str(normalized))
    return normalized.normalize()


def _reliable_tax_rate(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        normalized = Decimal(value)
    except (ArithmeticError, TypeError, ValueError):
        return None
    if (
        not normalized.is_finite()
        or normalized < _ZERO
        or normalized > Decimal("1")
    ):
        return None
    return normalized


def _legacy_contract_amount_inc(
    amount: Decimal | None,
    tax_rate: Decimal | None,
) -> Decimal | None:
    """Convert legacy ex-tax revenue only when its own rate is trustworthy.

    This field is compatibility evidence only.  It must never revive the old
    global 13% assumption when the source sale has no usable tax rate.
    """
    rate_value = _reliable_tax_rate(tax_rate)
    if amount is None or rate_value is None:
        return None
    try:
        amount_value = Decimal(amount)
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not amount_value.is_finite():
        return None
    return tax_policy.round_money(
        amount_value * (Decimal("1") + rate_value)
    )


def summarize_revenue_candidates(
    candidates: Iterable[tuple[Decimal | None, Decimal | None]],
) -> RevenueEvidence | None:
    """兼容纯函数：金额或税率证据不唯一时均失败关闭。

    数据库生产路径不调用本函数决定版本；统一由
    :func:`load_contract_revenue_evidence` 的 latest-effective 排序选择。
    """
    candidates = list(candidates)
    if not candidates:
        return None
    distinct_ex = {
        _decimal_key(amount)
        for amount, _tax_rate in candidates
    }
    distinct_tax_rates = {
        _decimal_key(tax_rate)
        for _amount, tax_rate in candidates
    }
    ambiguous_ex = len(distinct_ex) != 1
    tax_rate_ambiguous = len(distinct_tax_rates) != 1
    amount_ex_tax = None if ambiguous_ex else candidates[0][0]
    tax_rate = (
        _reliable_tax_rate(candidates[0][1])
        if not tax_rate_ambiguous
        else None
    )
    legacy_value = (
        _legacy_contract_amount_inc(amount_ex_tax, tax_rate)
        if not ambiguous_ex else None
    )
    # 兼容旧预算看板：负向销售/冲销仍以 0 为下限，但只有完整、唯一的
    # amount+tax_rate 证据才允许产生 legacy 含税值。
    legacy_contract_amount_inc = (
        max(_ZERO, legacy_value) if legacy_value is not None else None
    )
    return RevenueEvidence(
        revenue_ex=amount_ex_tax,
        tax_rate=tax_rate,
        tax_rate_ambiguous=tax_rate_ambiguous,
        ambiguous_inc=ambiguous_ex or tax_rate_ambiguous,
        ambiguous_ex=ambiguous_ex,
        record_count=len(candidates),
        legacy_contract_amount_inc=legacy_contract_amount_inc,
    )


def summarize_expense_amounts(
    amounts: Iterable[Decimal | None],
) -> ExpenseEvidence | None:
    """兼容纯函数：单一原金额按默认未税生成双值。"""
    return summarize_expense_records(
        (
            amount,
            amount,
            tax_policy.inc_from_ex(amount)
            if amount is not None and Decimal(amount).is_finite()
            else None,
        )
        for amount in amounts
    )


def summarize_expense_records(
    records: Iterable[
        tuple[Decimal | None, Decimal | None, Decimal | None]
    ],
) -> ExpenseEvidence | None:
    """汇总 ``(原金额, 未税金额, 含税金额)``，任一双税值非法则对应口径失败关闭。"""
    records = list(records)
    if not records:
        return None

    def _values(index: int) -> list[Decimal] | None:
        result: list[Decimal] = []
        for record in records:
            value = record[index]
            if value is None:
                return None
            normalized = Decimal(value)
            if not normalized.is_finite():
                return None
            result.append(normalized)
        return result

    raw_values = _values(0)
    ex_values = _values(1)
    inc_values = _values(2)
    return ExpenseEvidence(
        legacy_raw_total=sum(raw_values or [], _ZERO),
        expense_inc=(
            tax_policy.round_money(sum(inc_values, _ZERO))
            if inc_values is not None else None
        ),
        expense_ex=(
            tax_policy.round_money(sum(ex_values, _ZERO))
            if ex_values is not None else None
        ),
        record_count=len(records),
    )


def _latest_effective_revenue_rows(contract_nos: list[str]):
    """最新有效销售版本的唯一 SQL 定义；并列时依次按批次、事实行 ID 打破。"""
    ranked = (
        select(
            FSalesOrder.order_no.label("order_no"),
            FSalesOrder.amount_ex_tax.label("amount_ex_tax"),
            FSalesOrder.tax_rate.label("tax_rate"),
            func.count().over(
                partition_by=FSalesOrder.order_no,
            ).label("record_count"),
            func.row_number().over(
                partition_by=FSalesOrder.order_no,
                order_by=(
                    SysImportBatch.uploaded_at.desc().nullslast(),
                    FSalesOrder.created_at.desc().nullslast(),
                    FSalesOrder.import_batch_id.desc(),
                    FSalesOrder.id.desc(),
                ),
            ).label("version_rank"),
        )
        .join(
            SysImportBatch,
            SysImportBatch.id == FSalesOrder.import_batch_id,
        )
        .where(
            FSalesOrder.order_no.in_(contract_nos),
            FSalesOrder.data_status == config.ACTIVE_STATUS,
            SysImportBatch.file_type == "sales",
            SysImportBatch.status == "success",
        )
        .subquery()
    )
    return select(
        ranked.c.order_no,
        ranked.c.amount_ex_tax,
        ranked.c.tax_rate,
        ranked.c.record_count,
    ).where(ranked.c.version_rank == 1)


def load_contract_revenue_evidence(
    db: Session,
    contract_nos: list[str],
) -> dict[str, RevenueEvidence]:
    """一次查询装载 XSDD 收入；重复单号选择最新有效导入版本。"""
    contract_nos = sorted({value for value in contract_nos if value})
    if not contract_nos:
        return {}
    result: dict[str, RevenueEvidence] = {}
    for contract_no, amount_ex_tax, tax_rate, record_count in db.execute(
        _latest_effective_revenue_rows(contract_nos),
    ):
        legacy_amount = _legacy_contract_amount_inc(
            amount_ex_tax,
            tax_rate,
        )
        reliable_tax_rate = _reliable_tax_rate(tax_rate)
        result[contract_no] = RevenueEvidence(
            revenue_ex=amount_ex_tax,
            tax_rate=reliable_tax_rate,
            tax_rate_ambiguous=False,
            ambiguous_inc=False,
            ambiguous_ex=False,
            record_count=record_count,
            legacy_contract_amount_inc=(
                max(_ZERO, legacy_amount)
                if legacy_amount is not None else None
            ),
        )
    return result


def load_untyped_expense_evidence(
    db: Session,
    contract_nos: list[str],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, ExpenseEvidence]:
    """一次查询装载生效报销的原金额与双税金额。"""
    contract_nos = sorted({value for value in contract_nos if value})
    if not contract_nos:
        return {}
    stmt = (
        select(
            FProjectExpense.linked_sales_order_no,
            FProjectExpense.amount,
            FProjectExpense.amount_ex_tax,
            FProjectExpense.amount_inc_tax,
        )
        .where(
            FProjectExpense.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS,
            FProjectExpense.linked_sales_order_no.in_(contract_nos),
        )
    )
    if date_from is not None:
        stmt = stmt.where(FProjectExpense.expense_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(FProjectExpense.expense_date <= date_to)
    records: dict[
        str,
        list[tuple[Decimal | None, Decimal | None, Decimal | None]],
    ] = defaultdict(list)
    for contract_no, amount, amount_ex_tax, amount_inc_tax in db.execute(stmt):
        records[contract_no].append(
            (amount, amount_ex_tax, amount_inc_tax),
        )

    result: dict[str, ExpenseEvidence] = {}
    for contract_no, contract_records in records.items():
        evidence = summarize_expense_records(contract_records)
        if evidence is not None:
            result[contract_no] = evidence
    return result


def load_expense_snapshot_completeness(
    db: Session,
    contract_nos: list[str],
    *,
    required_through: date,
) -> dict[str, bool]:
    """读取合同费用完整快照门禁，并校验快照水位覆盖所需截止日。"""
    contract_nos = sorted({value for value in contract_nos if value})
    if not contract_nos:
        return {}
    return {
        contract_no: bool(
            complete
            and complete_through is not None
            and complete_through >= required_through
        )
        for contract_no, complete, complete_through in db.execute(
            select(
                MaintenanceContractWorkbookState.contract_no,
                MaintenanceContractWorkbookState.expense_snapshot_complete,
                MaintenanceContractWorkbookState.expense_complete_through,
            ).where(
                MaintenanceContractWorkbookState.contract_no.in_(contract_nos),
            ),
        )
    }
