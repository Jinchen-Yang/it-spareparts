"""商城 → itsp 的循环可售入站（D-0057 §10）。

不新建表。幂等看事件 payload 里的 shopOrderLineId。
不写库存、氚云或变卖台账。日志只有 deliveryId 与结果码。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_cycle import (
    MaintenanceCycleEvent,
    MaintenanceCycleSellIntent,
    MaintenanceCycleUnit,
)
from app.services.maintenance_cycle import _bump, _event, _locked_unit

log = logging.getLogger(__name__)

CALLBACK_ACTOR = "integration:cycle-sell"


class InboundError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _intent(db: Session, delivery_id: UUID) -> MaintenanceCycleSellIntent | None:
    return db.scalar(
        select(MaintenanceCycleSellIntent).where(
            MaintenanceCycleSellIntent.delivery_id == delivery_id
        )
    )


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


def _events(db: Session, unit_id: int, event_type: str) -> list[MaintenanceCycleEvent]:
    return list(
        db.scalars(
            select(MaintenanceCycleEvent)
            .where(
                MaintenanceCycleEvent.unit_id == unit_id,
                MaintenanceCycleEvent.event_type == event_type,
            )
            .order_by(MaintenanceCycleEvent.id)
        ).all()
    )


def _line(event: MaintenanceCycleEvent | None) -> str | None:
    if event is None:
        return None
    value = (event.payload or {}).get("shopOrderLineId")
    return value if isinstance(value, str) else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _receipt(
    intent: MaintenanceCycleSellIntent,
    unit: MaintenanceCycleUnit,
    shop_order_line_id: str,
    *,
    idempotent: bool,
) -> dict:
    return {
        "schemaVersion": 1,
        "deliveryId": str(intent.delivery_id),
        "unitId": unit.id,
        "outcome": unit.outcome,
        "shopOrderLineId": shop_order_line_id,
        "idempotent": idempotent,
    }


def requeue(db: Session, delivery_id: UUID) -> dict:
    row = _lock_intent(db, delivery_id)
    requeued = False
    if row.state == "blocked":
        now = datetime.now(UTC)
        row.available_at = now
        row.updated_at = now
        requeued = True
        db.flush()
    log.info("cycle requeue delivery=%s status=%s requeued=%s", row.delivery_id, row.state, requeued)
    return {
        "schemaVersion": 1,
        "deliveryId": str(row.delivery_id),
        "status": row.state,
        "requeued": requeued,
        "nextAttemptAt": _iso(row.available_at) if row.state in {"pending", "blocked"} else None,
    }


def _fail_open_intent(intent: MaintenanceCycleSellIntent) -> None:
    if intent.state not in {"pending", "blocked"}:
        return
    now = datetime.now(UTC)
    intent.state = "failed"
    intent.receipt_status = None
    intent.block_code = None
    intent.last_error_code = "unit_sold_by_callback"
    intent.lease_token = None
    intent.lease_until = None
    intent.completed_at = now
    intent.updated_at = now


def mark_sold(db: Session, *, delivery_id: UUID, shop_order_line_id: str) -> dict:
    preview = _intent(db, delivery_id)
    if preview is None:
        raise InboundError(404, "not_found")
    unit = _locked_unit(db, preview.unit_id)
    intent = _lock_intent(db, delivery_id)
    latest = _events(db, unit.id, "mark_sold")
    latest_mark = latest[-1] if latest else None
    if unit.outcome == "sold" and _line(latest_mark) == shop_order_line_id:
        log.info("cycle mark-sold delivery=%s status=idempotent", delivery_id)
        return _receipt(intent, unit, shop_order_line_id, idempotent=True)
    if unit.station != "closed" or unit.outcome != "to_shop" or intent.state in {"delivering", "failed"}:
        log.info("cycle mark-sold delivery=%s status=remote_conflict", delivery_id)
        raise InboundError(409, "remote_conflict")
    _fail_open_intent(intent)
    unit.outcome = "sold"
    _bump(unit, CALLBACK_ACTOR)
    _event(
        db,
        unit,
        "mark_sold",
        CALLBACK_ACTOR,
        {"outcome": "sold", "shopOrderLineId": shop_order_line_id, "qty": 1},
    )
    db.flush()
    log.info("cycle mark-sold delivery=%s status=sold", delivery_id)
    return _receipt(intent, unit, shop_order_line_id, idempotent=False)


def revert_sold(db: Session, *, delivery_id: UUID, shop_order_line_id: str) -> dict:
    preview = _intent(db, delivery_id)
    if preview is None:
        raise InboundError(404, "not_found")
    unit = _locked_unit(db, preview.unit_id)
    intent = _lock_intent(db, delivery_id)
    marks = _events(db, unit.id, "mark_sold")
    reverts = _events(db, unit.id, "revert_sold")
    latest_mark = marks[-1] if marks else None
    latest_revert = reverts[-1] if reverts else None
    reverted = (
        latest_revert is not None
        and _line(latest_revert) == shop_order_line_id
        and (latest_mark is None or latest_revert.id > latest_mark.id)
    )
    if unit.outcome == "to_shop" and reverted:
        log.info("cycle revert-sold delivery=%s status=idempotent", delivery_id)
        return _receipt(intent, unit, shop_order_line_id, idempotent=True)
    if unit.outcome == "sold" and _line(latest_mark) == shop_order_line_id:
        unit.outcome = "to_shop"
        _bump(unit, CALLBACK_ACTOR)
        _event(
            db,
            unit,
            "revert_sold",
            CALLBACK_ACTOR,
            {"outcome": "to_shop", "shopOrderLineId": shop_order_line_id, "qty": 1},
        )
        db.flush()
        log.info("cycle revert-sold delivery=%s status=to_shop", delivery_id)
        return _receipt(intent, unit, shop_order_line_id, idempotent=False)
    log.info("cycle revert-sold delivery=%s status=remote_conflict", delivery_id)
    raise InboundError(409, "remote_conflict")
