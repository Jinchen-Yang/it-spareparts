"""维保合同双口径毛利的纯计算规则。

本模块不查询数据库，只把已核实的收入、备件成本和费用完整性转换成可展示结果。
任何输入证据不完整时按口径 fail closed，不以 0 或另一税口径代替。
"""

from decimal import Decimal

from app import tax_policy


_RATE = Decimal("0.0001")
_ZERO = Decimal("0")
_COMPLETE_QUALITIES = frozenset({"actual_only", "contains_estimate"})
_COMPLETE_PARTS_STATUSES = frozenset({"complete_actual", "complete_estimated"})


def _money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        normalized = Decimal(value)
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not normalized.is_finite():
        return None
    return tax_policy.round_money(normalized)


def _margin(profit: Decimal | None, revenue: Decimal | None) -> Decimal | None:
    if profit is None or revenue is None or revenue <= 0:
        return None
    return (profit / revenue).quantize(_RATE)


def _complete_status(cost_quality: str) -> str:
    return (
        "complete_estimated"
        if cost_quality == "contains_estimate"
        else "complete_actual"
    )


def calculate_contract_margin(
    *,
    revenue_inc: Decimal | None,
    revenue_ex: Decimal | None,
    tax_rate: Decimal | None,
    parts_cost_inc_tax: Decimal | None,
    parts_cost_ex_tax: Decimal | None,
    cost_quality_inc: str,
    cost_quality_ex: str,
    expense_data_available: bool,
    date_filtered: bool,
    expense_inc: Decimal | None = None,
    expense_ex: Decimal | None = None,
    unknown_expense_total: Decimal | None = None,
    revenue_ambiguous_inc: bool = False,
    revenue_ambiguous_ex: bool = False,
) -> dict[str, Decimal | str | None]:
    """计算合同级含税、未税备件毛利与合同级贡献毛利。

    两套收入必须由各自的事实源显式传入；本函数绝不跨税口径反推金额。
    ``tax_rate`` 只为兼容旧调用，不参与业务计算；``unknown_expense_total``
    同样只兼容迁移前的未拆税费用证据。
    """
    del tax_rate
    revenue_inc = _money(revenue_inc)
    revenue_ex = _money(revenue_ex)
    if revenue_ambiguous_inc:
        revenue_inc = None
    parts_cost_inc_tax = _money(parts_cost_inc_tax)
    parts_cost_ex_tax = _money(parts_cost_ex_tax)
    expense_inc = _money(expense_inc)
    expense_ex = _money(expense_ex)
    unknown_expense_total = _money(unknown_expense_total)
    expense_unavailable = not expense_data_available
    # 兼容旧调用：明确传入 unknown=0 代表已证明无报销；非零或非法则两套都失败关闭。
    legacy_expense_complete = (
        unknown_expense_total is not None and unknown_expense_total == _ZERO
    )
    if expense_data_available and legacy_expense_complete:
        if expense_inc is None:
            expense_inc = _ZERO
        if expense_ex is None:
            expense_ex = _ZERO
    expense_tax_unknown = {
        "inc": expense_data_available and expense_inc is None,
        "ex": expense_data_available and expense_ex is None,
    }

    result: dict[str, Decimal | str | None] = {
        "revenue_inc": None if revenue_ambiguous_inc else revenue_inc,
        "revenue_ex": None if revenue_ambiguous_ex else revenue_ex,
        "parts_cost_inc_tax": parts_cost_inc_tax,
        "parts_cost_ex_tax": parts_cost_ex_tax,
        "parts_gross_profit_inc": None,
        "parts_gross_profit_ex": None,
        "parts_gross_margin_inc": None,
        "parts_gross_margin_ex": None,
        "parts_profit_status_inc": "incomplete_cost",
        "parts_profit_status_ex": "incomplete_cost",
        "expense_inc": None if expense_unavailable else expense_inc,
        "expense_ex": None if expense_unavailable else expense_ex,
        "contribution_profit_inc": None,
        "contribution_profit_ex": None,
        "contribution_margin_inc": None,
        "contribution_margin_ex": None,
        "contribution_status_inc": "incomplete_cost",
        "contribution_status_ex": "incomplete_cost",
    }

    if date_filtered:
        for basis in ("inc", "ex"):
            parts_cost = parts_cost_inc_tax if basis == "inc" else parts_cost_ex_tax
            cost_quality = cost_quality_inc if basis == "inc" else cost_quality_ex
            revenue = revenue_inc if basis == "inc" else revenue_ex
            revenue_ambiguous = (
                revenue_ambiguous_inc if basis == "inc" else revenue_ambiguous_ex
            )
            parts_status = (
                "ambiguous_revenue"
                if revenue_ambiguous
                else "missing_revenue"
                if revenue is None
                else "incomplete_cost"
                if cost_quality not in _COMPLETE_QUALITIES or parts_cost is None
                else "filtered_scope"
            )
            result[f"parts_profit_status_{basis}"] = parts_status
            if parts_status != "filtered_scope":
                result[f"contribution_status_{basis}"] = parts_status
            elif expense_unavailable:
                result[f"contribution_status_{basis}"] = "expense_data_unavailable"
            elif expense_tax_unknown[basis]:
                result[f"contribution_status_{basis}"] = "expense_tax_unknown"
            else:
                result[f"contribution_status_{basis}"] = "filtered_scope"
        return result

    cost_complete_inc = cost_quality_inc in _COMPLETE_QUALITIES
    cost_complete_ex = cost_quality_ex in _COMPLETE_QUALITIES
    if cost_complete_inc and revenue_inc is not None and parts_cost_inc_tax is not None:
        parts_profit_inc = tax_policy.round_money(
            revenue_inc - parts_cost_inc_tax,
        )
        result["parts_gross_profit_inc"] = parts_profit_inc
        result["parts_gross_margin_inc"] = _margin(parts_profit_inc, revenue_inc)
    if (
        not revenue_ambiguous_ex
        and cost_complete_ex
        and revenue_ex is not None
        and parts_cost_ex_tax is not None
    ):
        parts_profit_ex = tax_policy.round_money(
            revenue_ex - parts_cost_ex_tax,
        )
        result["parts_gross_profit_ex"] = parts_profit_ex
        result["parts_gross_margin_ex"] = _margin(parts_profit_ex, revenue_ex)

    if revenue_ambiguous_inc:
        result["parts_profit_status_inc"] = "ambiguous_revenue"
    elif revenue_inc is None:
        result["parts_profit_status_inc"] = "missing_revenue"
    elif not cost_complete_inc or parts_cost_inc_tax is None:
        result["parts_profit_status_inc"] = "incomplete_cost"
    else:
        result["parts_profit_status_inc"] = _complete_status(cost_quality_inc)

    if revenue_ambiguous_ex:
        result["parts_profit_status_ex"] = "ambiguous_revenue"
    elif revenue_ex is None:
        result["parts_profit_status_ex"] = "missing_revenue"
    elif not cost_complete_ex or parts_cost_ex_tax is None:
        result["parts_profit_status_ex"] = "incomplete_cost"
    else:
        result["parts_profit_status_ex"] = _complete_status(cost_quality_ex)

    for basis in ("inc", "ex"):
        parts_status = result[f"parts_profit_status_{basis}"]
        if parts_status not in _COMPLETE_PARTS_STATUSES:
            result[f"contribution_status_{basis}"] = parts_status
            continue
        if expense_unavailable:
            result[f"contribution_status_{basis}"] = "expense_data_unavailable"
            continue
        if expense_tax_unknown[basis]:
            result[f"contribution_status_{basis}"] = "expense_tax_unknown"
            continue
        parts_profit = result[f"parts_gross_profit_{basis}"]
        expense = result[f"expense_{basis}"]
        contribution_profit = (
            tax_policy.round_money(parts_profit - expense)
            if isinstance(parts_profit, Decimal) and isinstance(expense, Decimal)
            else None
        )
        result[f"contribution_profit_{basis}"] = contribution_profit
        result[f"contribution_margin_{basis}"] = _margin(
            contribution_profit,
            result[f"revenue_{basis}"],
        )
        result[f"contribution_status_{basis}"] = "complete"

    return result
