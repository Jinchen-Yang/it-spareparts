"""Persist a nullable source amount using its own import batch precedence."""
from decimal import Decimal

from sqlalchemy import String, Table, column, or_, update, values
from sqlalchemy.orm import Session

from app.models._types import Money


def write_order_amount_snapshot(
    session: Session,
    table: Table,
    orders: dict[str, dict],
    batch_id: int,
    amount_field: str,
    batch_field: str,
) -> int:
    balances: list[tuple[str, Decimal | None]] = sorted(
        (raw_id, order[amount_field])
        for raw_id, order in orders.items()
        if amount_field in order
    )
    current_batch = table.c[batch_field]
    updated = 0
    for start in range(0, len(balances), 1000):
        incoming = values(
            column("raw_order_id", String(64)),
            column("amount", Money),
            name="incoming_amount_snapshot",
        ).data(balances[start:start + 1000])
        stmt = (
            update(table)
            .where(table.c.raw_order_id == incoming.c.raw_order_id)
            .where(or_(current_batch.is_(None), current_batch < batch_id))
            .values({
                amount_field: incoming.c.amount.cast(Money),
                batch_field: batch_id,
            })
        )
        updated += session.execute(stmt).rowcount
    return updated
