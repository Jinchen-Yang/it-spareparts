"""Persist one current source value per sales order (D-0069)."""
from decimal import Decimal

from sqlalchemy import String, column, or_, update, values
from sqlalchemy.orm import Session

from app.models._types import Money
from app.models.sales import FSalesOrder


def write_sales_receivable(
    session: Session,
    orders: dict[str, dict],
    batch_id: int,
) -> int:
    balances: list[tuple[str, Decimal | None]] = sorted(
        (raw_id, order["sales_receivable_amount"])
        for raw_id, order in orders.items()
        if "sales_receivable_amount" in order
    )
    sales_order = FSalesOrder.__table__
    updated = 0
    for start in range(0, len(balances), 1000):
        incoming = values(
            column("raw_order_id", String(64)),
            column("amount", Money),
            name="incoming_sales_receivable",
        ).data(balances[start:start + 1000])
        # Explicit typing covers all-NULL chunks in PostgreSQL VALUES.
        stmt = (
            update(sales_order)
            .where(sales_order.c.raw_order_id == incoming.c.raw_order_id)
            .where(or_(
                sales_order.c.sales_receivable_batch_id.is_(None),
                sales_order.c.sales_receivable_batch_id < batch_id,
            ))
            .values(
                sales_receivable_amount=incoming.c.amount.cast(Money),
                sales_receivable_batch_id=batch_id,
            )
        )
        updated += session.execute(stmt).rowcount
    return updated
