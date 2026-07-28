"""维保合同双口径毛利的纯计算规则。

本模块不查询数据库，只把已核实的收入、备件成本和费用完整性转换成可展示结果。
任何输入证据不完整时按口径 fail closed，不以 0 或另一税口径代替。
"""
from decimal import Decimal


_CENT = Decimal("0.01")
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
    return normalized.quantize(_CENT)


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
    revenue_ex: Decimal | None,
    tax_rate: Decimal | None,
    parts_cost_inc_tax: Decimal | None,
    parts_cost_ex_tax: Decimal | None,
    cost_quality_inc: str,
    cost_quality_ex: str,
    unknown_expense_total: Decimal | None,
    expense_data_available: bool,
    date_filtered: bool,
    revenue_ambiguous_inc: bool = False,
    revenue_ambiguous_ex: bool = False,
) -> dict[str, Decimal | str | None]:
    """计算合同级含税、未税备件毛利与合同级贡献毛利。

    ``unknown_expense_total`` 是当前只有原金额、没有税务字段的生效报销。非零时仍可
    给出不含报销的备件毛利，但合同级贡献毛利两套都保持空值。
    """
    revenue_ex = _money(revenue_ex)
    parts_cost_inc_tax = _money(parts_cost_inc_tax)
    parts_cost_ex_tax = _money(parts_cost_ex_tax)
    unknown_expense_total = _money(unknown_expense_total)
    expense_tax_unknown = (
        expense_data_available
        and (unknown_expense_total is None or unknown_expense_total != _ZERO)
    )
    expense_unavailable = not expense_data_available

    revenue_inc = None
    invalid_tax_rate = tax_rate is not None and (
        not tax_rate.is_finite() or not (_ZERO <= tax_rate < Decimal("1"))
    )
    if (
        not revenue_ambiguous_inc
        and revenue_ex is not None
        and tax_rate is not None
        and not invalid_tax_rate
    ):
        revenue_inc = (revenue_ex * (Decimal(1) + tax_rate)).quantize(_CENT)

    result: dict[str, Decimal | str | None] = {
        "revenue_inc": revenue_inc,
        "revenue_ex": None if revenue_ambiguous_ex else revenue_ex,
        "parts_cost_inc_tax": parts_cost_inc_tax,
        "parts_cost_ex_tax": parts_cost_ex_tax,
        "parts_gross_profit_inc": None,
        "parts_gross_profit_ex": None,
        "parts_gross_margin_inc": None,
        "parts_gross_margin_ex": None,
        "parts_profit_status_inc": "incomplete_cost",
        "parts_profit_status_ex": "incomplete_cost",
        "expense_inc": None if expense_unavailable or expense_tax_unknown else _ZERO,
        "expense_ex": None if expense_unavailable or expense_tax_unknown else _ZERO,
        "contribution_profit_inc": None,
        "contribution_profit_ex": None,
        "contribution_margin_inc": None,
        "contribution_margin_ex": None,
        "contribution_status_inc": "incomplete_cost",
        "contribution_status_ex": "incomplete_cost",
    }

    if date_filtered:
        result["parts_profit_status_inc"] = "filtered_scope"
        result["parts_profit_status_ex"] = "filtered_scope"
        result["contribution_status_inc"] = "filtered_scope"
        result["contribution_status_ex"] = "filtered_scope"
        return result

    cost_complete_inc = cost_quality_inc in _COMPLETE_QUALITIES
    cost_complete_ex = cost_quality_ex in _COMPLETE_QUALITIES
    if cost_complete_inc and revenue_inc is not None and parts_cost_inc_tax is not None:
        parts_profit_inc = (revenue_inc - parts_cost_inc_tax).quantize(_CENT)
        result["parts_gross_profit_inc"] = parts_profit_inc
        result["parts_gross_margin_inc"] = _margin(parts_profit_inc, revenue_inc)
    if (
        not revenue_ambiguous_ex
        and cost_complete_ex
        and revenue_ex is not None
        and parts_cost_ex_tax is not None
    ):
        parts_profit_ex = (revenue_ex - parts_cost_ex_tax).quantize(_CENT)
        result["parts_gross_profit_ex"] = parts_profit_ex
        result["parts_gross_margin_ex"] = _margin(parts_profit_ex, revenue_ex)

    if revenue_ambiguous_inc:
        result["parts_profit_status_inc"] = "ambiguous_revenue"
    elif revenue_ex is None:
        result["parts_profit_status_inc"] = "missing_revenue"
    elif not cost_complete_inc or parts_cost_inc_tax is None:
        result["parts_profit_status_inc"] = "incomplete_cost"
    elif invalid_tax_rate:
        result["parts_profit_status_inc"] = "invalid_tax_rate"
    elif tax_rate is None:
        result["parts_profit_status_inc"] = "missing_tax_rate"
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
        if expense_tax_unknown:
            result[f"contribution_status_{basis}"] = "expense_tax_unknown"
            continue
        parts_profit = result[f"parts_gross_profit_{basis}"]
        expense = result[f"expense_{basis}"]
        contribution_profit = (
            parts_profit - expense
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
