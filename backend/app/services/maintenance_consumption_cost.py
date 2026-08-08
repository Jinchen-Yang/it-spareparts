"""Reproducible unit-cost resolution for confirmed site consumption.

The waterfall is intentionally strict and deterministic:
direct purchase line -> all valid purchase samples in ±7 days -> all valid
sales samples in ±7 days -> manual fill -> unresolved.  Returns are outside the
scope of this fact chain and therefore never offset consumption.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app import tax_policy
from app.models.maintenance_project_operations import MaintenanceSiteIssueLine
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder


ALGORITHM_VERSION = "site-issue-cost-v1"
_CENT = Decimal("0.01")
_MONEY_MAX_EXCLUSIVE = Decimal("1000000000000")
_QUANTITY_MAX_EXCLUSIVE = Decimal("100000000000")


class CostResolutionError(ValueError):
    """A resolved monetary value cannot be represented by Numeric(14,2)."""


def _amount(value: Decimal, *, label: str = "成本单价") -> Decimal:
    try:
        normalized = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise CostResolutionError(f"现场领用{label}超出允许范围") from exc
    if normalized < 0 or normalized >= _MONEY_MAX_EXCLUSIVE:
        raise CostResolutionError(f"现场领用{label}超出允许范围")
    return normalized


def _valid(qty: Decimal | None, unit_price: Decimal | None) -> bool:
    return (
        qty is not None
        and unit_price is not None
        and Decimal(qty) > 0
        and Decimal(unit_price) > 0
        and Decimal(qty) < _QUANTITY_MAX_EXCLUSIVE
        and Decimal(unit_price) < _MONEY_MAX_EXCLUSIVE
    )


def _weighted(samples: list[dict]) -> Decimal | None:
    valid = [sample for sample in samples if _valid(sample["quantity"], sample["unit_price_ex_tax"])]
    total_qty = sum((Decimal(sample["quantity"]) for sample in valid), start=Decimal("0"))
    if total_qty <= 0:
        return None
    return _amount(
        sum(
            (
                Decimal(sample["quantity"]) * Decimal(sample["unit_price_ex_tax"])
                for sample in valid
            ),
            start=Decimal("0"),
        )
        / total_qty
    )


def _direct_purchase(
    db: Session, line: MaintenanceSiteIssueLine, *, issue_date: date
) -> tuple[Decimal, list[dict]] | None:
    if line.linked_purchase_line_id is None:
        return None
    sample = db.execute(
        select(
            FPurchaseLine.id,
            FPurchaseLine.part_id,
            FPurchaseLine.qty,
            FPurchaseLine.unit_price,
            FPurchaseOrder.is_tax_inclusive,
            FPurchaseOrder.order_no,
            FPurchaseOrder.order_date,
        )
        .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
        .where(
            FPurchaseLine.id == line.linked_purchase_line_id,
            FPurchaseOrder.data_status == config.ACTIVE_STATUS,
        )
    ).one_or_none()
    if sample is None or sample.part_id != line.part_id or not _valid(sample.qty, sample.unit_price):
        return None
    unit_ex = (
        tax_policy.ex_from_inc(sample.unit_price)
        if sample.is_tax_inclusive is True
        else _amount(Decimal(sample.unit_price))
    )
    return unit_ex, [
        {
            "sample_id": f"purchase:{sample.id}",
            "document_no": sample.order_no,
            "document_date": sample.order_date.isoformat() if sample.order_date else None,
            "distance_days": (
                abs((sample.order_date - issue_date).days) if sample.order_date else None
            ),
            "quantity": format(sample.qty, "f"),
            "unit_price_raw": format(sample.unit_price, "f"),
            "unit_price_ex_tax": format(unit_ex, "f"),
            "tax_conversion": "divide_1.13" if sample.is_tax_inclusive is True else "none",
        }
    ]


def _purchase_window(
    db: Session, *, part_id: int, issue_date: date, from_date: date, to_date: date
) -> tuple[Decimal, list[dict]] | None:
    raw_samples = list(
        db.execute(
            select(
                FPurchaseLine.id,
                FPurchaseLine.qty,
                FPurchaseLine.unit_price,
                FPurchaseOrder.is_tax_inclusive,
                FPurchaseOrder.order_no,
                FPurchaseOrder.order_date,
            )
            .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
            .where(
                FPurchaseLine.part_id == part_id,
                FPurchaseOrder.data_status == config.ACTIVE_STATUS,
                FPurchaseOrder.order_date >= from_date,
                FPurchaseOrder.order_date <= to_date,
                FPurchaseLine.qty > 0,
                FPurchaseLine.unit_price > 0,
            )
            .order_by(FPurchaseOrder.order_date, FPurchaseLine.id)
        )
    )
    samples = [
        {
            "sample_id": f"purchase:{row.id}",
            "document_no": row.order_no,
            "document_date": row.order_date.isoformat(),
            "distance_days": abs((row.order_date - issue_date).days),
            "quantity": format(row.qty, "f"),
            "unit_price_raw": format(row.unit_price, "f"),
            "unit_price_ex_tax": format(
                Decimal(row.unit_price) / tax_policy.TAX_FACTOR
                if row.is_tax_inclusive is True
                else _amount(Decimal(row.unit_price)),
                "f",
            ),
            "tax_conversion": "divide_1.13" if row.is_tax_inclusive is True else "none",
        }
        for row in raw_samples
        if _valid(row.qty, row.unit_price)
    ]
    value = _weighted(samples)
    if value is None:
        return None
    return value, samples


def _sales_window(
    db: Session, *, part_id: int, issue_date: date, from_date: date, to_date: date
) -> tuple[Decimal, list[dict]] | None:
    raw_samples = list(
        db.execute(
            select(
                FSalesLine.id,
                FSalesLine.qty,
                FSalesLine.unit_price,
                FSalesOrder.order_no,
                FSalesOrder.order_date,
            )
            .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
            .where(
                FSalesLine.part_id == part_id,
                FSalesOrder.data_status == config.ACTIVE_STATUS,
                FSalesOrder.order_date >= from_date,
                FSalesOrder.order_date <= to_date,
                FSalesLine.qty > 0,
                FSalesLine.unit_price > 0,
            )
            .order_by(FSalesOrder.order_date, FSalesLine.id)
        )
    )
    samples = [
        {
            "sample_id": f"sales:{row.id}",
            "document_no": row.order_no,
            "document_date": row.order_date.isoformat(),
            "distance_days": abs((row.order_date - issue_date).days),
            "quantity": format(row.qty, "f"),
            "unit_price_raw": format(row.unit_price, "f"),
            "unit_price_ex_tax": format(
                Decimal(row.unit_price) / tax_policy.TAX_FACTOR,
                "f",
            ),
            "tax_conversion": "divide_1.13",
        }
        for row in raw_samples
        if _valid(row.qty, row.unit_price)
    ]
    value = _weighted(samples)
    if value is None:
        return None
    return value, samples


def resolve_line(
    db: Session,
    *,
    issue_date: date,
    line: MaintenanceSiteIssueLine,
) -> MaintenanceSiteIssueLine:
    """Recompute and persist one line's evidence; strong evidence beats manual."""

    from_date = issue_date - timedelta(days=7)
    to_date = issue_date + timedelta(days=7)
    source: str | None = None
    side: str | None = None
    unit_cost: Decimal | None = None
    samples: list[dict] = []

    direct = _direct_purchase(db, line, issue_date=issue_date)
    if direct is not None:
        source, side = "direct_purchase", "purchase"
        unit_cost, samples = direct
    else:
        purchase = _purchase_window(
            db,
            part_id=line.part_id,
            issue_date=issue_date,
            from_date=from_date,
            to_date=to_date,
        )
        if purchase is not None:
            source, side = "purchase_window", "purchase"
            unit_cost, samples = purchase
        else:
            sales = _sales_window(
                db,
                part_id=line.part_id,
                issue_date=issue_date,
                from_date=from_date,
                to_date=to_date,
            )
            if sales is not None:
                source, side = "sales_window", "sales"
                unit_cost, samples = sales
            elif line.manual_unit_cost is not None:
                source, side = "manual", "manual"
                unit_cost = _amount(Decimal(line.manual_unit_cost))

    line.cost_source = source
    line.price_basis = "ex_tax"
    line.reference_side = side
    line.reference_samples = samples
    line.reference_sample_ids = [sample["sample_id"] for sample in samples]
    line.reference_sample_count = len(samples)
    line.reference_window_from = from_date if source in {"purchase_window", "sales_window"} else None
    line.reference_window_to = to_date if source in {"purchase_window", "sales_window"} else None
    line.algorithm_version = ALGORITHM_VERSION
    line.unit_cost = unit_cost
    line.cost_amount = (
        _amount(Decimal(line.quantity) * unit_cost, label="成本金额")
        if unit_cost is not None
        else None
    )
    return line
