"""商城 → itsp 的循环可售入站（D-0058）。

requeue 仍只把 blocked 意图的 available_at 调到现在。
mark-sold / revert-sold 由 API 直接返回 409 phase_deferred，这里不再改行。
不写库存、氚云或变卖台账。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_cycle import MaintenanceCycleSellIntent

log = logging.getLogger(__name__)


class InboundError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _lock_intent(db: Session, delivery_id: UUID) -> MaintenanceCycleSellIntent:
    row = db.scalar(
        select(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.delivery_id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise InboundError(404, "not_found")
    return row


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def requeue(db: Session, delivery_id: UUID) -> dict:
    row = _lock_intent(db, delivery_id)
    requeued = False
    if row.state == "blocked":
        now = datetime.now(UTC)
        row.available_at = now
        row.updated_at = now
        requeued = True
        db.flush()
    log.info(
        "cycle requeue delivery=%s status=%s requeued=%s",
        row.delivery_id,
        row.state,
        requeued,
    )
    return {
        "schemaVersion": 1,
        "deliveryId": str(row.delivery_id),
        "status": row.state,
        "requeued": requeued,
        "nextAttemptAt": _iso(row.available_at) if row.state in {"pending", "blocked"} else None,
    }
