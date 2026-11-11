"""坏件循环可售意图出站 worker（D-0057 T4）。

与目录通知（``catalog_delivery``）同款质量，但独立开关/地址/凭据，不塞进目录 worker：
- 默认关闭：``SHOP_CYCLE_SELL_ENABLED=false`` 或 URL 为空时不启动网络循环，意图保持 pending。
- 领取：``FOR UPDATE SKIP LOCKED``（pending 到期 / delivering 租约过期 / blocked 到期），
  租约 60 秒；语句超时 5 秒、锁超时 1 秒；事务在 HTTP 之前结束。
- HTTP：总期限 15 秒、不跟随重定向、``trust_env=false``、响应最大 4 KiB、
  ``content-encoding`` 必须 identity；回执 ``extra=forbid`` 且身份必须与领取行一致。
- 回执落库用租约 CAS；晚到的旧工人回执影响 0 行。
- 日志只含 deliveryId / partId / 结果码 / 是否接受：无 traceback、无 token、无 body。
- 错误与退避：applied/unchanged → delivered；blocked → 5m/15m/1h/6h（封顶 6h）；
  conflict → failed 不自动重试；超时/传输失败/非 200/无效回执 → pending，
  5 秒起指数退避封顶 1 小时；网络失败不写成 blocked。
- R9：先前只是 blocked 的意图若收到 ``unchanged``，视为无效回执（不标 delivered，
  回 pending 重试）；只有「先前已 applied」的 unchanged 才是成功。
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
from app.models.maintenance_cycle import MaintenanceCycleSellIntent

log = logging.getLogger(__name__)

LEASE_SECONDS = 60
HTTP_DEADLINE_SECONDS = 15
POLL_SECONDS = 5
MAX_RECEIPT_BYTES = 4096
BLOCK_BACKOFF_SECONDS = (300, 900, 3600, 21600)
NETWORK_BACKOFF_BASE_SECONDS = 5
NETWORK_BACKOFF_CAP_SECONDS = 3600
# 商城稳定 blockCode 集合（任务书 §8.2）
BLOCK_CODES = frozenset({
    "missing_formal_image",
    "missing_category_mapping",
    "missing_list_price",
    "missing_catalog_identity",
    "publish_api_missing_stock_update",
    "publish_disabled",
})
NETWORK_RESULTS = frozenset({
    "transport_error", "deadline_exceeded", "remote_http_error", "invalid_receipt",
})
TERMINAL_OK = frozenset({"applied", "unchanged"})


@dataclass(frozen=True)
class Claim:
    delivery_id: UUID
    unit_id: int
    part_id: int
    condition_bucket: str
    lease_token: UUID
    attempts: int
    # N1：持久证据——最后一次被商城接受的回执（applied/unchanged/blocked/conflict）。
    # ``unchanged`` 只有在它为 ``applied`` 时才可接受为 delivered。
    previous_receipt_status: str | None


def _bound_transaction(db: Session) -> None:
    db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    db.execute(text("SET LOCAL lock_timeout = '1000ms'"))


def _network_backoff(attempts: int) -> int:
    exponent = min(max(attempts - 1, 0), 10)
    return min(NETWORK_BACKOFF_CAP_SECONDS, NETWORK_BACKOFF_BASE_SECONDS * 2 ** exponent)


def _block_backoff(attempts: int) -> int:
    return BLOCK_BACKOFF_SECONDS[min(max(attempts - 1, 0), len(BLOCK_BACKOFF_SECONDS) - 1)]


def claim_one(session_factory=SessionLocal) -> Claim | None:
    """领取一条可投递意图；事务在返回前结束（HTTP 之前不持锁）。"""
    with session_factory() as db, db.begin():
        _bound_transaction(db)
        now = db.scalar(select(func.clock_timestamp()))
        row = db.scalar(
            select(MaintenanceCycleSellIntent)
            .where(or_(
                and_(MaintenanceCycleSellIntent.state == "pending",
                     MaintenanceCycleSellIntent.available_at <= now),
                and_(MaintenanceCycleSellIntent.state == "delivering",
                     MaintenanceCycleSellIntent.lease_until <= now),
                and_(MaintenanceCycleSellIntent.state == "blocked",
                     MaintenanceCycleSellIntent.available_at <= now),
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
        # N1：把「展示用 block_code」与「尚未得到 applied 的持久证据」分开。
        # 持久证据 = receipt_status（最后一次被商城接受的回执）；block_code 仅供展示，
        # 网络/无效回执失败不得清掉 receipt_status。
        previous_receipt_status = row.receipt_status
        if row.state == "blocked" and previous_receipt_status is None:
            # 兼容历史数据/外部种子：blocked 行即使 receipt_status 空缺也视为未 applied。
            previous_receipt_status = "blocked"
        row.state = "delivering"
        row.lease_token = uuid4()
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        row.attempts += 1
        row.updated_at = now
        return Claim(
            delivery_id=row.delivery_id,
            unit_id=row.unit_id,
            part_id=row.part_id,
            condition_bucket=row.condition_bucket,
            lease_token=row.lease_token,
            attempts=row.attempts,
            previous_receipt_status=previous_receipt_status,
        )


def acknowledge(
    claim: Claim,
    result: str,
    *,
    block_code: str | None = None,
    session_factory=SessionLocal,
) -> bool:
    """租约 CAS 落回执；过期/被回收工人的回执返回 False 且不改任何行。"""
    if result not in TERMINAL_OK | {"blocked", "conflict"} | NETWORK_RESULTS:
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
        # N1/R9：持久记录里「上一次被接受的回执=applied」才允许 unchanged 成功；
        # 否则（None/blocked/conflict/unchanged）一律视为无效回执，保持 pending 退避。
        if effective == "unchanged" and claim.previous_receipt_status != "applied":
            effective = "invalid_receipt"
        # 网络/无效回执失败把行送回 pending 时必须保留持久证据，不能清成 None。
        preserved_status = claim.previous_receipt_status
        values: dict = {
            "lease_token": None,
            "lease_until": None,
            "updated_at": now,
        }
        if effective in TERMINAL_OK:
            values.update(
                state="delivered", receipt_status=effective, completed_at=now,
                block_code=None, last_error_code=None,
            )
        elif effective == "blocked":
            code = block_code if block_code in BLOCK_CODES else None
            if code is None:
                values.update(
                    state="pending", receipt_status=preserved_status, block_code=None,
                    last_error_code="invalid_receipt",
                    available_at=now + timedelta(seconds=_network_backoff(claim.attempts)),
                )
            else:
                values.update(
                    state="blocked", receipt_status="blocked", block_code=code,
                    last_error_code=None,
                    available_at=now + timedelta(seconds=_block_backoff(claim.attempts)),
                )
        elif effective == "conflict":
            values.update(
                state="failed", receipt_status="conflict", completed_at=now,
                block_code=None, last_error_code="manual_conflict",
            )
        else:
            code = effective if effective in NETWORK_RESULTS else "transport_error"
            values.update(
                state="pending", receipt_status=preserved_status, last_error_code=code,
                # block_code 只是展示；网络失败清掉展示码，但保留 receipt_status 证据。
                block_code=None,
                available_at=now + timedelta(seconds=_network_backoff(claim.attempts)),
            )
        changed = db.execute(
            update(MaintenanceCycleSellIntent)
            .where(
                MaintenanceCycleSellIntent.delivery_id == claim.delivery_id,
                MaintenanceCycleSellIntent.state == "delivering",
                MaintenanceCycleSellIntent.lease_token == claim.lease_token,
                # F5：用数据库实时时钟做截止比较；取锁等待跨过到期点后 CAS 必须失败，
                # 不能用进入事务前/取锁前取到的 Python now。
                MaintenanceCycleSellIntent.lease_until > func.clock_timestamp(),
            )
            .values(**values)
        )
        return changed.rowcount == 1


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: int = Field(ge=1, le=1)
    deliveryId: str
    unitId: int = Field(gt=0, le=2**63 - 1)
    partId: int = Field(gt=0, le=2**31 - 1)
    status: Literal["applied", "unchanged", "blocked", "conflict"]
    blockCode: str | None = None


async def deliver(
    claim: Claim, settings: Settings, *, transport=None
) -> tuple[str, str | None]:
    """发送一条 sell-intent；返回 (结果码, blockCode)。不做任何 DB 写入。"""
    token = settings.shop_cycle_sell_token.get_secret_value()

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
                # 跨仓 JSON 用 camelCase；载荷只有 part/桶/qty/投递 id。
                json={
                    "schemaVersion": 1,
                    "deliveryId": str(claim.delivery_id),
                    "unitId": claim.unit_id,
                    "partId": claim.part_id,
                    "conditionBucket": claim.condition_bucket,
                    "qty": 1,
                },
            ) as response:
                if response.status_code != 200:
                    return "remote_http_error", None
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
                try:
                    receipt = Receipt.model_validate_json(bytes(body))
                except (ValidationError, ValueError):
                    return "invalid_receipt", None
                if (
                    receipt.deliveryId != str(claim.delivery_id)
                    or receipt.partId != claim.part_id
                    or receipt.unitId != claim.unit_id
                ):
                    return "invalid_receipt", None
                if receipt.status == "blocked":
                    if receipt.blockCode not in BLOCK_CODES:
                        return "invalid_receipt", None
                    return "blocked", receipt.blockCode
                if receipt.blockCode is not None:
                    return "invalid_receipt", None
                return receipt.status, None

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
            # 不打 traceback：SQL 参数/响应体可能带业务数据或凭据。
            log.warning("cycle_sell_intent worker_error")
        try:
            await asyncio.wait_for(wake.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass


class SellIntentWorker:
    """默认关闭的可售意图出站 worker；开关/URL 不满足时完全不起网络循环。"""

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
            # 让有界当前请求完成并落回执后再退出。
            await self.task
            self.task = None


def read_status(db: Session, unit_id: int) -> dict:
    row = db.scalar(
        select(MaintenanceCycleSellIntent).where(
            MaintenanceCycleSellIntent.unit_id == unit_id
        )
    )
    if row is None:
        return {"unit_id": unit_id, "status": "not_queued"}
    return {
        "unit_id": row.unit_id,
        "deliveryId": str(row.delivery_id),
        "status": row.state,
        "attempts": row.attempts,
        "blockCode": row.block_code,
        "lastErrorCode": row.last_error_code,
        "receiptStatus": row.receipt_status,
        "nextAttemptAt": row.available_at if row.state in {"pending", "blocked"} else None,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
        "completedAt": row.completed_at,
    }
