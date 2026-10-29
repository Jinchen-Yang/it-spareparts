"""Transactional outbox, short leases and CAS acknowledgements for new PN identity.

The notification is only a hint: shop fetches the current source row by stable ID.
The DB transaction is always closed before any network I/O.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, event, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.models.catalog_delivery import CatalogDelivery

log = logging.getLogger(__name__)
LEASE_SECONDS = 60
HTTP_DEADLINE_SECONDS = 15
POLL_SECONDS = 5
MAX_RECEIPT_BYTES = 4096
_wake_loop: asyncio.AbstractEventLoop | None = None
_wake_event: asyncio.Event | None = None


def _wake_after_commit(db: Session) -> None:
    # SQLAlchemy also emits after_commit for a savepoint; only outer commit wakes.
    if not db.in_nested_transaction() and _wake_loop is not None and _wake_event is not None:
        try:
            _wake_loop.call_soon_threadsafe(_wake_event.set)
        except RuntimeError:
            pass  # Shutdown raced commit; durable polling recovers on next start.


def enqueue(db: Session, part_id: int) -> CatalogDelivery:
    """Caller owns commit/rollback; every shared create path gets the same hook."""
    row = CatalogDelivery(part_id=part_id)
    db.add(row)
    db.flush()
    if not event.contains(db, "after_commit", _wake_after_commit):
        event.listen(db, "after_commit", _wake_after_commit)
    return row


@dataclass(frozen=True)
class Claim:
    delivery_id: UUID
    part_id: int
    lease_token: UUID
    attempts: int


def _bound_transaction(db: Session) -> None:
    db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    db.execute(text("SET LOCAL lock_timeout = '1000ms'"))


def claim_one(session_factory=SessionLocal) -> Claim | None:
    with session_factory() as db, db.begin():
        _bound_transaction(db)
        now = db.scalar(select(func.clock_timestamp()))
        row = db.scalar(
            select(CatalogDelivery).where(or_(
                and_(CatalogDelivery.state == "pending", CatalogDelivery.available_at <= now),
                and_(CatalogDelivery.state == "delivering", CatalogDelivery.lease_until <= now),
            )).order_by(CatalogDelivery.created_at, CatalogDelivery.delivery_id)
            .with_for_update(skip_locked=True).limit(1)
        )
        if row is None:
            return None
        row.state = "delivering"
        row.lease_token = uuid4()
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        row.attempts += 1
        row.updated_at = now
        return Claim(row.delivery_id, row.part_id, row.lease_token, row.attempts)


def acknowledge(claim: Claim, result: str, session_factory=SessionLocal) -> bool:
    """A late worker cannot acknowledge a reclaimed or expired lease."""
    success = result in {"applied", "unchanged", "conflict"}
    # Only these fixed codes are stored. Exception text, SQL, response and tokens never are.
    if not success and result not in {
        "transport_error", "deadline_exceeded", "remote_http_error", "invalid_receipt",
    }:
        result = "transport_error"
    with session_factory() as db, db.begin():
        _bound_transaction(db)
        now = db.scalar(select(func.clock_timestamp()))
        values = {
            "state": ("conflict" if result == "conflict" else "succeeded") if success else "pending",
            "receipt_status": result if success else None,
            "last_error_code": ("manual_conflict" if result == "conflict" else None) if success else result,
            "completed_at": now if success else None,
            "lease_token": None,
            "lease_until": None,
            "updated_at": now,
        }
        if not success:
            values["available_at"] = now + timedelta(seconds=min(3600, 5 * 2 ** min(claim.attempts - 1, 10)))
        changed = db.execute(update(CatalogDelivery).where(
            CatalogDelivery.delivery_id == claim.delivery_id,
            CatalogDelivery.state == "delivering",
            CatalogDelivery.lease_token == claim.lease_token,
            CatalogDelivery.lease_until > now,
        ).values(**values))
        return changed.rowcount == 1


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: int = Field(ge=1, le=1)
    deliveryId: str
    partId: int = Field(gt=0, le=2**31 - 1)
    status: Literal["applied", "unchanged", "conflict"]


async def deliver(claim: Claim, settings: Settings, *, transport=None) -> str:
    """Bound both response size and wall time, including a slow streaming peer."""
    async def request() -> str:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=3), follow_redirects=False,
            trust_env=False, transport=transport,
        ) as client:
            async with client.stream(
                "POST", settings.shop_catalog_sync_url,
                headers={"Authorization": f"Bearer {settings.shop_catalog_sync_token.get_secret_value()}",
                         "Accept-Encoding": "identity"},
                json={"schemaVersion": 1, "deliveryId": str(claim.delivery_id), "partId": claim.part_id},
            ) as response:
                if response.status_code != 200:
                    return "remote_http_error"
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return "invalid_receipt"
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=MAX_RECEIPT_BYTES + 1):
                    body.extend(chunk)
                    if len(body) > MAX_RECEIPT_BYTES:
                        return "invalid_receipt"
                receipt = Receipt.model_validate_json(bytes(body))
                if receipt.deliveryId != str(claim.delivery_id) or receipt.partId != claim.part_id:
                    return "invalid_receipt"
                return receipt.status

    try:
        return await asyncio.wait_for(request(), timeout=HTTP_DEADLINE_SECONDS)
    except TimeoutError:
        return "deadline_exceeded"
    except (ValidationError, ValueError):
        return "invalid_receipt"
    except httpx.HTTPError:
        return "transport_error"


async def run_worker(settings: Settings, stop: asyncio.Event, wake: asyncio.Event) -> None:
    while not stop.is_set():
        wake.clear()
        try:
            claim = await asyncio.to_thread(claim_one)
            if claim is not None:
                result = await deliver(claim, settings)
                accepted = await asyncio.to_thread(acknowledge, claim, result)
                log.info("catalog_delivery result=%s accepted=%s", result, accepted)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            # No traceback: SQL errors may contain statement parameters or business data.
            log.warning("catalog_delivery worker_error")
        try:
            await asyncio.wait_for(wake.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass


class DeliveryWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        global _wake_loop, _wake_event
        if not self.settings.shop_catalog_sync_enabled:
            return
        _wake_loop, _wake_event = asyncio.get_running_loop(), self.wake
        self.task = asyncio.create_task(run_worker(self.settings, self.stop, self.wake))

    async def close(self) -> None:
        global _wake_loop, _wake_event
        _wake_loop, _wake_event = None, None
        self.stop.set()
        self.wake.set()
        if self.task is not None:
            # Let the bounded current request finish and acknowledge before shutdown.
            await self.task


def read_status(db: Session, part_id: int) -> dict:
    row = db.scalar(select(CatalogDelivery).where(CatalogDelivery.part_id == part_id))
    if row is None:
        return {"partId": part_id, "status": "not_queued"}
    return {
        "partId": row.part_id, "deliveryId": str(row.delivery_id), "status": row.state,
        "attempts": row.attempts, "lastErrorCode": row.last_error_code,
        "receiptStatus": row.receipt_status, "createdAt": row.created_at,
        "updatedAt": row.updated_at, "completedAt": row.completed_at,
        "nextAttemptAt": row.available_at if row.state == "pending" else None,
    }
