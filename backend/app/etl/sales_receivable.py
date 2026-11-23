"""Persist one current source value per sales order (D-0069)."""
from sqlalchemy.orm import Session

from app.etl.order_amount_snapshot import write_order_amount_snapshot
from app.models.sales import FSalesOrder


def write_sales_receivable(
    session: Session,
    orders: dict[str, dict],
    batch_id: int,
) -> int:
    return write_order_amount_snapshot(
        session, FSalesOrder.__table__, orders, batch_id,
        "sales_receivable_amount", "sales_receivable_batch_id",
    )
