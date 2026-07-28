from decimal import Decimal

from app.services import maintenance_margin


def _calculate(**overrides):
    values = {
        "revenue_ex": Decimal("1000"),
        "tax_rate": Decimal("0.06"),
        "parts_cost_inc_tax": Decimal("226"),
        "parts_cost_ex_tax": Decimal("200"),
        "cost_quality_inc": "actual_only",
        "cost_quality_ex": "actual_only",
        "unknown_expense_total": Decimal("0"),
        "expense_data_available": True,
        "date_filtered": False,
    }
    values.update(overrides)
    return maintenance_margin.calculate_contract_margin(**values)


def test_calculates_inc_and_ex_contract_margin_independently():
    result = _calculate()

    assert result["revenue_inc"] == Decimal("1060.00")
    assert result["revenue_ex"] == Decimal("1000.00")
    assert result["parts_gross_profit_inc"] == Decimal("834.00")
    assert result["parts_gross_profit_ex"] == Decimal("800.00")
    assert result["parts_profit_status_inc"] == "complete_actual"
    assert result["parts_profit_status_ex"] == "complete_actual"
    assert result["contribution_profit_inc"] == Decimal("834.00")
    assert result["contribution_profit_ex"] == Decimal("800.00")
    assert result["contribution_margin_inc"] == Decimal("0.7868")
    assert result["contribution_margin_ex"] == Decimal("0.8000")
    assert result["contribution_status_inc"] == "complete"
    assert result["contribution_status_ex"] == "complete"
    assert "gross_profit_inc" not in result
    assert "gross_profit_ex" not in result
    assert "profit_status_inc" not in result
    assert "profit_status_ex" not in result


def test_estimated_cost_is_visible_but_never_labeled_actual():
    result = _calculate(
        cost_quality_inc="contains_estimate",
        cost_quality_ex="contains_estimate",
    )

    assert result["parts_profit_status_inc"] == "complete_estimated"
    assert result["parts_profit_status_ex"] == "complete_estimated"
    assert result["contribution_profit_inc"] == Decimal("834.00")
    assert result["contribution_profit_ex"] == Decimal("800.00")
    assert result["contribution_status_inc"] == "complete"
    assert result["contribution_status_ex"] == "complete"


def test_missing_contract_tax_rate_only_blocks_inc_basis():
    result = _calculate(tax_rate=None)

    assert result["revenue_inc"] is None
    assert result["parts_gross_profit_inc"] is None
    assert result["parts_gross_margin_inc"] is None
    assert result["parts_profit_status_inc"] == "missing_tax_rate"
    assert result["contribution_profit_inc"] is None
    assert result["contribution_status_inc"] == "missing_tax_rate"
    assert result["parts_gross_profit_ex"] == Decimal("800.00")
    assert result["parts_profit_status_ex"] == "complete_actual"
    assert result["contribution_profit_ex"] == Decimal("800.00")
    assert result["contribution_status_ex"] == "complete"


def test_conflicting_duplicate_revenue_fails_closed_with_explicit_status():
    result = _calculate(
        revenue_ambiguous_inc=True,
        revenue_ambiguous_ex=True,
    )

    assert result["revenue_inc"] is None
    assert result["revenue_ex"] is None
    assert result["parts_gross_profit_inc"] is None
    assert result["parts_gross_profit_ex"] is None
    assert result["parts_profit_status_inc"] == "ambiguous_revenue"
    assert result["parts_profit_status_ex"] == "ambiguous_revenue"
    assert result["contribution_profit_inc"] is None
    assert result["contribution_profit_ex"] is None
    assert result["contribution_status_inc"] == "ambiguous_revenue"
    assert result["contribution_status_ex"] == "ambiguous_revenue"


def test_tax_only_duplicate_conflict_blocks_inc_but_not_ex_basis():
    result = _calculate(
        tax_rate=None,
        revenue_ambiguous_inc=True,
        revenue_ambiguous_ex=False,
    )

    assert result["parts_gross_profit_inc"] is None
    assert result["parts_profit_status_inc"] == "ambiguous_revenue"
    assert result["contribution_profit_inc"] is None
    assert result["contribution_status_inc"] == "ambiguous_revenue"
    assert result["parts_gross_profit_ex"] == Decimal("800.00")
    assert result["parts_profit_status_ex"] == "complete_actual"
    assert result["contribution_profit_ex"] == Decimal("800.00")
    assert result["contribution_status_ex"] == "complete"


def test_out_of_range_contract_tax_rate_only_blocks_inc_basis():
    for invalid_rate in (Decimal("1"), Decimal("1.3"), Decimal("NaN")):
        result = _calculate(tax_rate=invalid_rate)

        assert result["revenue_inc"] is None
        assert result["parts_gross_profit_inc"] is None
        assert result["parts_profit_status_inc"] == "invalid_tax_rate"
        assert result["contribution_profit_inc"] is None
        assert result["contribution_status_inc"] == "invalid_tax_rate"
        assert result["parts_gross_profit_ex"] == Decimal("800.00")
        assert result["parts_profit_status_ex"] == "complete_actual"
        assert result["contribution_profit_ex"] == Decimal("800.00")
        assert result["contribution_status_ex"] == "complete"


def test_incomplete_cost_fails_closed_for_both_bases():
    result = _calculate(
        parts_cost_inc_tax=None,
        parts_cost_ex_tax=None,
        cost_quality_inc="incomplete",
        cost_quality_ex="incomplete",
    )

    assert result["parts_gross_profit_inc"] is None
    assert result["parts_gross_profit_ex"] is None
    assert result["parts_profit_status_inc"] == "incomplete_cost"
    assert result["parts_profit_status_ex"] == "incomplete_cost"
    assert result["contribution_profit_inc"] is None
    assert result["contribution_profit_ex"] is None
    assert result["contribution_status_inc"] == "incomplete_cost"
    assert result["contribution_status_ex"] == "incomplete_cost"


def test_cost_evidence_fails_closed_per_tax_basis_not_globally():
    result = _calculate(
        parts_cost_inc_tax=None,
        cost_quality_inc="incomplete",
        cost_quality_ex="actual_only",
    )

    assert result["parts_gross_profit_inc"] is None
    assert result["parts_profit_status_inc"] == "incomplete_cost"
    assert result["contribution_profit_inc"] is None
    assert result["contribution_status_inc"] == "incomplete_cost"
    assert result["parts_gross_profit_ex"] == Decimal("800.00")
    assert result["parts_profit_status_ex"] == "complete_actual"
    assert result["contribution_profit_ex"] == Decimal("800.00")
    assert result["contribution_status_ex"] == "complete"


def test_nonzero_expense_without_tax_fields_keeps_parts_margin_but_blocks_project_margin():
    result = _calculate(unknown_expense_total=Decimal("50"))

    assert result["parts_gross_profit_inc"] == Decimal("834.00")
    assert result["parts_gross_profit_ex"] == Decimal("800.00")
    assert result["parts_profit_status_inc"] == "complete_actual"
    assert result["parts_profit_status_ex"] == "complete_actual"
    assert result["contribution_profit_inc"] is None
    assert result["contribution_profit_ex"] is None
    assert result["contribution_status_inc"] == "expense_tax_unknown"
    assert result["contribution_status_ex"] == "expense_tax_unknown"


def test_unavailable_expense_dataset_never_treats_absence_as_zero():
    result = _calculate(
        unknown_expense_total=Decimal("0"),
        expense_data_available=False,
    )

    assert result["parts_gross_profit_inc"] == Decimal("834.00")
    assert result["parts_gross_profit_ex"] == Decimal("800.00")
    assert result["parts_profit_status_inc"] == "complete_actual"
    assert result["parts_profit_status_ex"] == "complete_actual"
    assert result["contribution_profit_inc"] is None
    assert result["contribution_profit_ex"] is None
    assert result["contribution_status_inc"] == "expense_data_unavailable"
    assert result["contribution_status_ex"] == "expense_data_unavailable"


def test_invalid_expense_amount_is_never_silently_treated_as_zero():
    result = _calculate(unknown_expense_total=Decimal("NaN"))

    assert result["parts_gross_profit_inc"] == Decimal("834.00")
    assert result["parts_gross_profit_ex"] == Decimal("800.00")
    assert result["parts_profit_status_inc"] == "complete_actual"
    assert result["parts_profit_status_ex"] == "complete_actual"
    assert result["contribution_profit_inc"] is None
    assert result["contribution_profit_ex"] is None
    assert result["contribution_status_inc"] == "expense_tax_unknown"
    assert result["contribution_status_ex"] == "expense_tax_unknown"


def test_nonfinite_revenue_and_cost_fail_closed_instead_of_raising():
    invalid_revenue = _calculate(revenue_ex=Decimal("NaN"))
    assert invalid_revenue["parts_profit_status_inc"] == "missing_revenue"
    assert invalid_revenue["parts_profit_status_ex"] == "missing_revenue"
    assert invalid_revenue["contribution_status_inc"] == "missing_revenue"
    assert invalid_revenue["contribution_status_ex"] == "missing_revenue"

    invalid_inc_cost = _calculate(parts_cost_inc_tax=Decimal("Infinity"))
    assert invalid_inc_cost["parts_profit_status_inc"] == "incomplete_cost"
    assert invalid_inc_cost["parts_profit_status_ex"] == "complete_actual"
    assert invalid_inc_cost["contribution_status_inc"] == "incomplete_cost"
    assert invalid_inc_cost["contribution_status_ex"] == "complete"


def test_date_filtered_costs_are_not_compared_with_full_contract_revenue():
    result = _calculate(date_filtered=True)

    assert result["parts_gross_profit_inc"] is None
    assert result["parts_gross_profit_ex"] is None
    assert result["contribution_profit_inc"] is None
    assert result["contribution_profit_ex"] is None
    assert result["parts_profit_status_inc"] == "filtered_scope"
    assert result["parts_profit_status_ex"] == "filtered_scope"
    assert result["contribution_status_inc"] == "filtered_scope"
    assert result["contribution_status_ex"] == "filtered_scope"


def test_nonpositive_revenue_keeps_amount_but_not_margin_rate():
    result = _calculate(
        revenue_ex=Decimal("0"),
        tax_rate=Decimal("0"),
        parts_cost_inc_tax=Decimal("10"),
        parts_cost_ex_tax=Decimal("10"),
    )

    assert result["parts_gross_profit_inc"] == Decimal("-10.00")
    assert result["parts_gross_profit_ex"] == Decimal("-10.00")
    assert result["parts_gross_margin_inc"] is None
    assert result["parts_gross_margin_ex"] is None
    assert result["contribution_profit_inc"] == Decimal("-10.00")
    assert result["contribution_profit_ex"] == Decimal("-10.00")
    assert result["contribution_margin_inc"] is None
    assert result["contribution_margin_ex"] is None
