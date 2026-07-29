from decimal import Decimal

import pytest

from app import tax_policy


def test_fixed_tax_rate_and_factor_are_explicit():
    assert tax_policy.TAX_RATE == Decimal("0.13")
    assert tax_policy.TAX_FACTOR == Decimal("1.13")


@pytest.mark.parametrize(
    ("amount_ex", "amount_inc"),
    [
        (Decimal("0.50"), Decimal("0.57")),
        (Decimal("2.50"), Decimal("2.83")),
        (Decimal("-0.50"), Decimal("-0.57")),
    ],
)
def test_inc_from_ex_matches_postgresql_numeric_rounding(
    amount_ex: Decimal,
    amount_inc: Decimal,
):
    assert tax_policy.inc_from_ex(amount_ex) == amount_inc


def test_ex_from_inc_matches_postgresql_numeric_rounding():
    assert tax_policy.ex_from_inc(Decimal("1.00")) == Decimal("0.88")


@pytest.mark.parametrize(
    ("raw", "rounded"),
    [
        (Decimal("0.005"), Decimal("0.01")),
        (Decimal("-0.005"), Decimal("-0.01")),
    ],
)
def test_round_money_uses_half_away_from_zero(raw: Decimal, rounded: Decimal):
    assert tax_policy.round_money(raw) == rounded
