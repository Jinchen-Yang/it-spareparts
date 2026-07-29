"""Fixed-rate tax arithmetic shared by Python business write paths.

PostgreSQL ``round(numeric, 2)`` rounds midpoint values away from zero.
``Decimal.quantize`` otherwise inherits the process context (normally
``ROUND_HALF_EVEN``), so DEV-15 fixed-rate tax amounts enter through
:func:`round_money` to keep Python and database facts equal.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import TypeAlias

from app import config


DecimalInput: TypeAlias = Decimal | int | str | float

TAX_RATE = Decimal(config.PROFIT_VAT_RATE)
TAX_FACTOR = Decimal("1") + TAX_RATE
MONEY_QUANTUM = Decimal("0.01")
MONEY_ROUNDING = ROUND_HALF_UP


def _decimal(value: DecimalInput) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_money(value: DecimalInput) -> Decimal:
    """Round money exactly like PostgreSQL ``round(numeric, 2)``."""
    return _decimal(value).quantize(MONEY_QUANTUM, rounding=MONEY_ROUNDING)


def inc_from_ex(amount_ex: DecimalInput) -> Decimal:
    """Convert an ex-tax amount to inc-tax at the fixed 13% rate."""
    return round_money(_decimal(amount_ex) * TAX_FACTOR)


def ex_from_inc(amount_inc: DecimalInput) -> Decimal:
    """Convert an inc-tax amount to ex-tax at the fixed 13% rate."""
    return round_money(_decimal(amount_inc) / TAX_FACTOR)
