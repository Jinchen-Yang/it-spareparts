"""坏件循环可售意图出站 worker（D-0058 / v2）。

默认关闭：``SHOP_CYCLE_SELL_ENABLED=false`` 或 URL 为空时不启动网络循环。
载荷仍是六键：``schemaVersion`` 为 2，``unitId`` 为结果的 ``wire_unit_id``，
``qty`` 为修好数。不发送 ``recordId``。
422 进入 failed 且不重试；409 与 5xx 一样回到 pending 并退避。
身份一致的 ``unchanged`` 直接记 delivered，不要求先前见过 ``applied``。
网络失败不得清掉上一份未加库存回执。
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
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.models.maintenance_cycle import BLOCK_CODES as MODEL_BLOCK_CODES
from app.models.maintenance_cycle import MaintenanceCycleSellIntent

log = logging.getLogger(__name__)

LEASE_SECONDS = 60
HTTP_DEADLINE_SECONDS = 15
POLL_SECONDS = 5
MAX_RECEIPT_BYTES = 4096
BLOCK_BACKOFF_SECONDS = (300, 900, 3600, 21600)
NETWORK_BACKOFF_BASE_SECONDS = 5
NETWORK_BACKOFF_CAP_SECONDS = 3600
BLOCK_CODES = frozenset(MODEL_BLOCK_CODES)
NETWORK_RESULTS = frozenset({
    "transport_error",
    "deadline_exceeded",
    "remote_http_error",
    "invalid_receipt",
    "remote_busy",
    "credential_rejected",
})
TERMINAL_OK = frozenset({"applied", "unchanged"})
WIRE_KEYS = (
    "schemaVersion",
    "deliveryId",
    "unitId",
    "partId",
    "conditionBucket",
    "qty",
)


@dataclass(frozen=True)
class Claim:
    delivery_id: UUID
    wire_unit_id: int
    part_id: int
    qty: int
    condition_bucket: str
    lease_token: UUID
    attempts: int
    previous_receipt_status: str | None
    previous_block_code: str | None


def _bound_transaction(db: Session) -> None:
    db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    db.execute(text("SET LOCAL lock_timeout = '1000ms'"))


def _network_backoff(attempts: int) -> int:
    exponent = min(max(attempts - 1, 0), 10)
    return min(
        NETWORK_BACKOFF_CAP_SECONDS, NETWORK_BACKOFF_BASE_SECONDS * 2 ** exponent
    )


def _block_backoff(attempts: int) -> int:
    return BLOCK_BACKOFF_SECONDS[
        min(max(attempts - 1, 0), len(BLOCK_BACKOFF_SECONDS) - 1)
    ]


def build_payload(claim: Claim) -> dict:
    """itsp 只发 v2 六键。坏件桶不发出；调用方插入的意图本期都是 good。"""
    payload = {
        "schemaVersion": 2,
        "deliveryId": str(claim.delivery_id),
        "unitId": int(claim.wire_unit_id),
        "partId": int(claim.part_id),
        "conditionBucket": claim.condition_bucket,
        "qty": int(claim.qty),
    }
    if set(payload) != set(WIRE_KEYS) or "recordId" in payload:
        raise RuntimeError("cycle sell payload drifted from the six wire keys")
    return payload


def claim_one(session_factory=SessionLocal) -> Claim | None:
    """领取一条可投递意图；事务在返回前结束（HTTP 之前不持锁）。"""
    with session_factory() as db, db.begin():
        _bound_transaction(db)
        now = db.scalar(select(func.clock_timestamp()))
        row = db.scalar(
            select(MaintenanceCycleSellIntent)
            .where(or_(
                and_(
                    MaintenanceCycleSellIntent.state == "pending",
                    MaintenanceCycleSellIntent.available_at <= now,
                ),
                and_(
                    MaintenanceCycleSellIntent.state == "delivering",
                    MaintenanceCycleSellIntent.lease_until <= now,
                ),
                and_(
                    MaintenanceCycleSellIntent.state == "blocked",
                    MaintenanceCycleSellIntent.available_at <= now,
                ),
            ))
            .order_by(
                MaintenanceCycleSellIntent.available_at,
                MaintenanceCycleSellIntent.created_at,
                MaintenanceCycleSellIntent.delivery_id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        previous_receipt_status = row.receipt_status
        if row.state == "blocked" and previous_receipt_status is None:
            previous_receipt_status = "blocked"
        previous_block_code = row.block_code
        row.state = "delivering"
        row.lease_token = uuid4()
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        row.attempts += 1
        row.updated_at = now
        return Claim(
            delivery_id=row.delivery_id,
            wire_unit_id=int(row.wire_unit_id),
            part_id=int(row.part_id),
            qty=int(row.qty),
            condition_bucket=row.condition_bucket,
            lease_token=row.lease_token,
            attempts=int(row.attempts),
            previous_receipt_status=previous_receipt_status,
            previous_block_code=previous_block_code,
        )


def _preserved(claim: Claim) -> tuple[str | None, str | None]:
    """网络失败或 409 不得擦掉已证明的入账/未入账证据。"""
    status = claim.previous_receipt_status
    block = claim.previous_block_code if claim.previous_block_code in BLOCK_CODES else None
    if status in {"applied", "unchanged"}:
        return "applied", None
    if status == "blocked" and block is not None:
        return "blocked", block
    if block == "publish_disabled":
        return "blocked", block
    return None, None


def acknowledge(
    claim: Claim,
    result: str,
    *,
    block_code: str | None = None,
    session_factory=SessionLocal,
) -> bool:
    """租约 CAS 落回执；过期或已被作废改成 failed 的回执影响 0 行。"""
    if result not in TERMINAL_OK | {"blocked", "conflict", "invalid_request"} | NETWORK_RESULTS:
        result = "transport_error"
    with session_factory() as db, db.begin():
        _bound_transaction(db)
        now = db.scalar(select(func.clock_timestamp()))
        row = db.scalar(
            select(MaintenanceCycleSellIntent)
            .where(MaintenanceCycleSellIntent.delivery_id == claim.delivery_id)
            .with_for_update()
        )
        if row is None:
            return False
        effective = result
        values: dict = {
            "lease_token": None,
            "lease_until": None,
            "updated_at": now,
        }
        if effective in TERMINAL_OK:
            # unchanged 与 applied 都表示库存已经加上。持久状态写成 applied。
            values.update(
                state="delivered",
                receipt_status="applied",
                completed_at=now,
                block_code=None,
                last_error_code=None,
            )
        elif effective == "blocked":
            code = block_code if block_code in BLOCK_CODES else None
            if code is None:
                receipt_status, preserved_block = _preserved(claim)
                values.update(
                    state="pending",
                    receipt_status=receipt_status,
                    block_code=preserved_block,
                    last_error_code="invalid_receipt",
                    available_at=now + timedelta(seconds=_network_backoff(claim.attempts)),
                )
            else:
                values.update(
                    state="blocked",
                    receipt_status="blocked",
                    block_code=code,
                    last_error_code=None,
                    available_at=now + timedelta(seconds=_block_backoff(claim.attempts)),
                )
        elif effective in {"conflict", "invalid_request"}:
            receipt_status, preserved_block = _preserved(claim)
            stocked = receipt_status == "applied" or preserved_block == "publish_disabled"
            values.update(
                state="failed",
                receipt_status="applied" if stocked and receipt_status == "applied" else (
                    "blocked" if stocked else ("conflict" if effective == "conflict" else None)
                ),
                block_code=preserved_block if stocked else None,
                completed_at=now,
                last_error_code=(
                    "manual_conflict" if effective == "conflict" else "invalid_request"
                ),
            )
        else:
            code = effective if effective in NETWORK_RESULTS else "transport_error"
            receipt_status, preserved_block = _preserved(claim)
            values.update(
                state="pending",
                receipt_status=receipt_status,
                block_code=preserved_block,
                last_error_code=code,
                available_at=now + timedelta(seconds=_network_backoff(claim.attempts)),
            )
        changed = db.execute(
            update(MaintenanceCycleSellIntent)
            .where(
                MaintenanceCycleSellIntent.delivery_id == claim.delivery_id,
                MaintenanceCycleSellIntent.state == "delivering",
                MaintenanceCycleSellIntent.lease_token == claim.lease_token,
                MaintenanceCycleSellIntent.lease_until > func.clock_timestamp(),
            )
            .values(**values)
        )
        return changed.rowcount == 1


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1, 2]
    deliveryId: str
    unitId: int = Field(gt=0, le=2**63 - 1)
    partId: int = Field(gt=0, le=2**31 - 1)
    status: Literal["applied", "unchanged", "blocked", "conflict"]
    # 六键都必须出现。blockCode 允许 JSON null，缺键则是无效回执。
    blockCode: str | None


def interpret_shop_response(
    sent: dict, status_code: int, body: bytes
) -> tuple[str, str | None]:
    """按金样例把一次商城响应收成回执码。``sent`` 是这次实际发出的 JSON。"""
    if status_code == 422:
        return "invalid_request", None
    if status_code == 409:
        return "remote_busy", None
    if status_code in {401, 403}:
        return "credential_rejected", None
    if status_code != 200:
        return "remote_http_error", None
    try:
        receipt = Receipt.model_validate_json(body)
    except (ValidationError, ValueError):
        return "invalid_receipt", None
    if (
        receipt.schemaVersion != sent.get("schemaVersion")
        or receipt.deliveryId != str(sent.get("deliveryId"))
        or receipt.unitId != sent.get("unitId")
        or receipt.partId != sent.get("partId")
    ):
        return "invalid_receipt", None
    if receipt.status == "blocked":
        if receipt.blockCode not in BLOCK_CODES:
            return "invalid_receipt", None
        return "blocked", receipt.blockCode
    if receipt.blockCode is not None:
        return "invalid_receipt", None
    return receipt.status, None


async def deliver(
    claim: Claim, settings: Settings, *, transport=None
) -> tuple[str, str | None]:
    """发送一条 sell-intent；返回 (结果码, blockCode)。不做任何 DB 写入。"""
    token = settings.shop_cycle_sell_token.get_secret_value()
    payload = build_payload(claim)

    async def request() -> tuple[str, str | None]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=3),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            async with client.stream(
                "POST",
                settings.shop_cycle_sell_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                if response.status_code != 200:
                    return interpret_shop_response(payload, response.status_code, b"")
                if (
                    response.headers.get("content-encoding", "identity").lower()
                    != "identity"
                ):
                    return "invalid_receipt", None
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=MAX_RECEIPT_BYTES + 1):
                    body.extend(chunk)
                    if len(body) > MAX_RECEIPT_BYTES:
                        return "invalid_receipt", None
                return interpret_shop_response(payload, response.status_code, bytes(body))

    try:
        return await asyncio.wait_for(request(), timeout=HTTP_DEADLINE_SECONDS)
    except TimeoutError:
        return "deadline_exceeded", None
    except (ValidationError, ValueError):
        return "invalid_receipt", None
    except httpx.HTTPError:
        return "transport_error", None


async def run_worker(settings: Settings, stop: asyncio.Event, wake: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            claim = await asyncio.to_thread(claim_one)
            if claim is not None:
                result, block_code = await deliver(claim, settings)
                accepted = await asyncio.to_thread(
                    acknowledge, claim, result, block_code=block_code
                )
                log.info(
                    "cycle_sell_intent delivery_id=%s part_id=%s result=%s accepted=%s",
                    claim.delivery_id, claim.part_id, result, accepted,
                )
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("cycle_sell_intent worker_error")
        try:
            await asyncio.wait_for(wake.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass


class SellIntentWorker:
    """默认关闭的可售意图出站 worker；开关或 URL 不满足时完全不起网络循环。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.settings.shop_cycle_sell_enabled:
            return
        if not self.settings.shop_cycle_sell_url:
            return
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.task = asyncio.create_task(run_worker(self.settings, self.stop, self.wake))

    async def close(self) -> None:
        self.stop.set()
        self.wake.set()
        if self.task is not None:
            await self.task
            self.task = None


def read_status(db: Session, record_id: UUID) -> dict:
    from app.services.maintenance_repair import intent_status

    return intent_status(db, record_id)
