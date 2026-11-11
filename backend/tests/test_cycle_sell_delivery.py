"""坏件循环 T4：SellIntentWorker 契约（任务书 §6.6 / §8.2 / §11 T4 / §12 R1、R9）。

不连真实商城：全部 HTTP 用 httpx.MockTransport 假回执；默认开关关闭。
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, update

from app.config import Settings
from app.models.dimensions import DimPart
from app.models.maintenance_cycle import MaintenanceCycleSellIntent, MaintenanceCycleUnit
from app.services import cycle_sell_delivery as svc

TOKEN = "synthetic-cycle-sell-token-for-tests"
URL = "https://shop.example/api/integrations/cycle/sell-intents"


def _settings(**kw) -> Settings:
    return Settings(
        _env_file=None,
        shop_cycle_sell_url=URL,
        shop_cycle_sell_token=SecretStr(TOKEN),
        shop_cycle_sell_enabled=True,
        **kw,
    )


def _unit(db, pn: str = "CYCLE-SELL-PN") -> MaintenanceCycleUnit:
    part = DimPart(pn_std=pn, description="sell intent test")
    db.add(part)
    db.flush()
    unit = MaintenanceCycleUnit(
        part_id=part.id,
        pn=pn,
        station="closed",
        outcome="to_shop",
        condition_bucket="good",
        source_kind="external",
        closed_at=datetime.now(timezone.utc),
        created_by="t4-test",
        updated_by="t4-test",
    )
    db.add(unit)
    db.flush()
    return unit


def _intent(db, unit: MaintenanceCycleUnit, **overrides) -> MaintenanceCycleSellIntent:
    values = dict(
        unit_id=unit.id,
        part_id=unit.part_id,
        qty=Decimal(1),
        condition_bucket="good",
        state="pending",
        attempts=0,
    )
    values.update(overrides)
    row = MaintenanceCycleSellIntent(**values)
    db.add(row)
    db.commit()
    return row


def _row(db, delivery_id) -> MaintenanceCycleSellIntent:
    db.expire_all()
    row = db.get(MaintenanceCycleSellIntent, delivery_id)
    assert row is not None
    return row


def _force_due(db, delivery_id, *, seconds_ago: int = 60) -> None:
    db.execute(
        update(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.delivery_id == delivery_id)
        .values(available_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago))
    )
    db.commit()


# ---------------------------------------------------------------------------
# R1/R10：默认关闭
# ---------------------------------------------------------------------------


def test_default_settings_disable_cycle_sell_worker_and_keep_pending(db):
    assert Settings(_env_file=None).shop_cycle_sell_enabled is False
    unit = _unit(db)
    intent = _intent(db, unit)

    async def scenario() -> None:
        worker = svc.SellIntentWorker(Settings(_env_file=None))
        await worker.start()
        assert worker.task is None
        await worker.close()

    asyncio.run(scenario())
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"


def test_worker_without_url_does_not_start(db):
    from types import SimpleNamespace

    settings = SimpleNamespace(
        shop_cycle_sell_enabled=True,
        shop_cycle_sell_url="",
        shop_cycle_sell_token=SecretStr(TOKEN),
    )

    async def scenario() -> None:
        worker = svc.SellIntentWorker(settings)  # type: ignore[arg-type]
        await worker.start()
        assert worker.task is None
        await worker.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 领取：pending 到期 / blocked 到期 / delivering 租约过期
# ---------------------------------------------------------------------------


def test_claim_pending_leases_and_increments_attempts(db):
    unit = _unit(db)
    intent = _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    assert claim.delivery_id == intent.delivery_id
    assert claim.unit_id == unit.id
    assert claim.part_id == unit.part_id
    assert claim.condition_bucket == "good"
    assert claim.attempts == 1
    assert claim.previous_receipt_status is None
    row = _row(db, intent.delivery_id)
    assert row.state == "delivering"
    assert row.lease_token == claim.lease_token
    assert row.lease_until is not None
    # 租约未过期：第二个工人领不到
    assert svc.claim_one() is None


def test_claim_expired_delivering_lease_reclaims(db):
    unit = _unit(db)
    intent = _intent(db, unit)
    first = svc.claim_one()
    assert first is not None
    db.execute(
        update(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.delivery_id == intent.delivery_id)
        .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    db.commit()
    second = svc.claim_one()
    assert second is not None
    assert second.delivery_id == intent.delivery_id
    assert second.lease_token != first.lease_token
    assert second.attempts == 2


def test_claim_blocked_due_exposes_previous_receipt_status(db):
    unit = _unit(db)
    intent = _intent(
        db, unit, state="blocked", receipt_status="blocked",
        block_code="missing_list_price",
        available_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    claim = svc.claim_one()
    assert claim is not None
    assert claim.delivery_id == intent.delivery_id
    assert claim.previous_receipt_status == "blocked"


# ---------------------------------------------------------------------------
# 回执：delivered / blocked 退避 / conflict / 网络失败
# ---------------------------------------------------------------------------


def test_applied_receipt_marks_delivered_and_stops(db):
    unit = _unit(db)
    intent = _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    assert svc.acknowledge(claim, "applied") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "delivered"
    assert row.receipt_status == "applied"
    assert row.completed_at is not None
    assert row.lease_token is None and row.lease_until is None
    # 终态不再被领取/二次回执
    assert svc.claim_one() is None
    assert svc.acknowledge(claim, "applied") is False


def test_unchanged_without_prior_applied_evidence_is_invalid(db):
    """N1/R9：只有持久记录「上一次被接受的回执=applied」才接受 unchanged。"""
    unit = _unit(db)
    intent = _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    assert claim.previous_receipt_status is None
    assert svc.acknowledge(claim, "unchanged") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.receipt_status is None
    assert row.last_error_code == "invalid_receipt"
    assert row.completed_at is None
    # 第二次 unchanged：持久证据仍非 applied，不得因「上一跳被清空」而放行（N1 多跳）
    _force_due(db, intent.delivery_id)
    second = svc.claim_one()
    assert second is not None and second.previous_receipt_status is None
    assert svc.acknowledge(second, "unchanged") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.receipt_status is None
    assert row.last_error_code == "invalid_receipt"


def test_blocked_then_unchanged_twice_never_reports_delivered(db):
    """N1 最小复现：blocked → unchanged → unchanged 不得误报 delivered。"""
    unit = _unit(db)
    intent = _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    assert svc.acknowledge(claim, "blocked", block_code="missing_formal_image") is True

    _force_due(db, intent.delivery_id)
    first = svc.claim_one()
    assert first is not None and first.previous_receipt_status == "blocked"
    assert svc.acknowledge(first, "unchanged") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.receipt_status == "blocked", "网络/无效回执不得清掉持久 blocked 证据"
    assert row.last_error_code == "invalid_receipt"

    _force_due(db, intent.delivery_id)
    second = svc.claim_one()
    assert second is not None and second.previous_receipt_status == "blocked"
    assert svc.acknowledge(second, "unchanged") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.receipt_status == "blocked"
    assert row.last_error_code == "invalid_receipt"
    assert row.completed_at is None


def test_blocked_then_network_failure_then_unchanged_stays_invalid(db):
    """N1：网络失败保留持久证据；后续 unchanged 仍不得 delivered。"""
    unit = _unit(db)
    intent = _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    assert svc.acknowledge(claim, "blocked", block_code="missing_list_price") is True

    _force_due(db, intent.delivery_id)
    retry = svc.claim_one()
    assert retry is not None and retry.previous_receipt_status == "blocked"
    assert svc.acknowledge(retry, "transport_error") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.block_code is None  # 展示码清空
    assert row.receipt_status == "blocked"  # 持久证据保留
    assert row.last_error_code == "transport_error"

    _force_due(db, intent.delivery_id)
    after = svc.claim_one()
    assert after is not None and after.previous_receipt_status == "blocked"
    assert svc.acknowledge(after, "unchanged") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.receipt_status == "blocked"
    assert row.last_error_code == "invalid_receipt"


def test_unchanged_is_legal_when_persisted_last_receipt_is_applied(db):
    """N1：持久记录 receipt_status=applied 时，unchanged 才可接受为 delivered。"""
    unit = _unit(db)
    intent = _intent(
        db, unit, state="delivering", receipt_status="applied",
        attempts=1,
        lease_token=uuid4(),
        lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    claim = svc.claim_one()
    assert claim is not None
    assert claim.delivery_id == intent.delivery_id
    assert claim.previous_receipt_status == "applied"
    assert svc.acknowledge(claim, "unchanged") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "delivered"
    assert row.receipt_status == "unchanged"
    assert row.completed_at is not None


@pytest.mark.parametrize("prior", [None, "blocked", "applied"])
@pytest.mark.parametrize(
    ("result", "block_code"),
    [
        ("applied", None),
        ("unchanged", None),
        ("blocked", "missing_list_price"),
        ("conflict", None),
        ("transport_error", None),
        ("invalid_receipt", None),
    ],
)
def test_acknowledge_transition_matrix(db, prior, result, block_code):
    """N1 自查矩阵：receipt_status × 回执结果的落库组合（防下一轮再出 P1）。"""
    unit = _unit(db)
    intent = _intent(db, unit, receipt_status=prior)
    if prior is not None and prior != "blocked":
        # 允许「持久证据=applied」的恢复态通过 delivering 过期被重领
        db.execute(
            update(MaintenanceCycleSellIntent)
            .where(MaintenanceCycleSellIntent.delivery_id == intent.delivery_id)
            .values(
                state="delivering",
                attempts=1,
                lease_token=uuid4(),
                lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        db.commit()
    claim = svc.claim_one()
    assert claim is not None, (prior, result)
    assert svc.acknowledge(claim, result, block_code=block_code) is True
    row = _row(db, intent.delivery_id)
    if result == "applied":
        assert row.state == "delivered" and row.receipt_status == "applied"
    elif result == "unchanged":
        if prior == "applied":
            assert row.state == "delivered"
            assert row.receipt_status == "unchanged"
        else:
            assert row.state == "pending"
            assert row.receipt_status == prior
            assert row.last_error_code == "invalid_receipt"
    elif result == "blocked":
        assert row.state == "blocked" and row.receipt_status == "blocked"
        assert row.block_code == block_code
    elif result == "conflict":
        assert row.state == "failed" and row.receipt_status == "conflict"
        assert row.last_error_code == "manual_conflict"
    else:
        assert row.state == "pending"
        assert row.receipt_status == prior, "网络/无效回执不得改写持久证据"
        assert row.last_error_code == result
        assert row.block_code is None


def test_blocked_sets_5min_backoff_and_then_R9_unchanged_is_invalid(db):
    unit = _unit(db)
    intent = _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    before = datetime.now(timezone.utc)
    assert svc.acknowledge(claim, "blocked", block_code="missing_formal_image") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "blocked"
    assert row.receipt_status == "blocked"
    assert row.block_code == "missing_formal_image"
    assert before + timedelta(seconds=250) <= row.available_at <= before + timedelta(seconds=400)

    _force_due(db, intent.delivery_id)
    retry = svc.claim_one()
    assert retry is not None and retry.previous_receipt_status == "blocked"
    # R9/N1：持久证据仍非 applied，商城重放 unchanged → 无效回执，不标 delivered。
    assert svc.acknowledge(retry, "unchanged") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.receipt_status == "blocked"
    assert row.last_error_code == "invalid_receipt"


def test_blocked_backoff_steps_and_cap(db):
    unit = _unit(db)
    intent = _intent(db, unit, attempts=0)
    observed = []
    for expected_seconds in (300, 900, 3600, 21600, 21600):
        _force_due(db, intent.delivery_id)
        claim = svc.claim_one()
        assert claim is not None
        before = datetime.now(timezone.utc)
        assert svc.acknowledge(claim, "blocked", block_code="publish_disabled") is True
        row = _row(db, intent.delivery_id)
        assert row.state == "blocked"
        delay = (row.available_at - before).total_seconds()
        observed.append(delay)
        assert expected_seconds - 60 <= delay <= expected_seconds + 60, observed
    # 第 4/5 次都封顶 6 小时（微秒级抖动不参与严格排序）
    assert abs(observed[-1] - 21600) <= 60
    assert abs(observed[-2] - 21600) <= 60


def test_conflict_marks_failed_without_retry(db):
    unit = _unit(db)
    intent = _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    assert svc.acknowledge(claim, "conflict") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "failed"
    assert row.receipt_status == "conflict"
    assert row.completed_at is not None
    assert svc.claim_one() is None


@pytest.mark.parametrize(
    "result", ["transport_error", "deadline_exceeded", "remote_http_error", "invalid_receipt"]
)
def test_network_failures_return_to_pending_with_backoff_not_blocked(db, result):
    unit = _unit(db)
    intent = _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    before = datetime.now(timezone.utc)
    assert svc.acknowledge(claim, result) is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.receipt_status is None
    assert row.block_code is None
    assert row.last_error_code == result
    delay = (row.available_at - before).total_seconds()
    assert 2 <= delay <= 10  # 5 秒起


def test_stale_lease_ack_is_ignored_and_new_worker_wins(db):
    unit = _unit(db)
    intent = _intent(db, unit)
    stale = svc.claim_one()
    assert stale is not None
    db.execute(
        update(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.delivery_id == intent.delivery_id)
        .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    db.commit()
    fresh = svc.claim_one()
    assert fresh is not None and fresh.lease_token != stale.lease_token
    # 旧工人回执影响 0 行，不覆盖新工人
    assert svc.acknowledge(stale, "applied") is False
    assert svc.acknowledge(fresh, "applied") is True
    assert _row(db, intent.delivery_id).state == "delivered"


# ---------------------------------------------------------------------------
# deliver：mock transport 与回执校验
# ---------------------------------------------------------------------------


def _claim_for(db) -> svc.Claim:
    _unit(db)
    unit = db.scalars(select(MaintenanceCycleUnit)).first()
    _intent(db, unit)
    claim = svc.claim_one()
    assert claim is not None
    return claim


def _receipt(claim: svc.Claim, **overrides) -> dict:
    payload = {
        "schemaVersion": 1,
        "deliveryId": str(claim.delivery_id),
        "unitId": claim.unit_id,
        "partId": claim.part_id,
        "status": "applied",
        "blockCode": None,
    }
    payload.update(overrides)
    return payload


def test_deliver_applied_payload_is_minimal_camel_case(db):
    claim = _claim_for(db)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_receipt(claim))

    result = asyncio.run(
        svc.deliver(claim, _settings(), transport=httpx.MockTransport(handler))
    )
    assert result == ("applied", None)
    assert captured["url"] == URL
    assert captured["json"] == {
        "schemaVersion": 1,
        "deliveryId": str(claim.delivery_id),
        "unitId": claim.unit_id,
        "partId": claim.part_id,
        "conditionBucket": claim.condition_bucket,
        "qty": 1,
    }
    for forbidden in ("projectId", "project_id", "serialNumber", "vendorName",
                      "note", "cost", "price", "customerId"):
        assert forbidden not in captured["json"]
    assert captured["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_deliver_blocked_receipt_returns_block_code(db):
    claim = _claim_for(db)
    result = asyncio.run(
        svc.deliver(
            claim,
            _settings(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json=_receipt(claim, status="blocked",
                                       blockCode="missing_list_price")
                )
            ),
        )
    )
    assert result == ("blocked", "missing_list_price")


@pytest.mark.parametrize(
    "overrides,headers,expected",
    [
        ({"deliveryId": "00000000-0000-0000-0000-000000000000"}, None, "invalid_receipt"),
        ({"unitId": 999999}, None, "invalid_receipt"),
        ({"partId": 999999}, None, "invalid_receipt"),
        ({"projectId": 42}, None, "invalid_receipt"),
        ({"blockCode": "missing_list_price"}, None, "invalid_receipt"),
        ({"status": "blocked", "blockCode": "unknown_code"}, None, "invalid_receipt"),
    ],
)
def test_deliver_rejects_invalid_receipts(db, overrides, headers, expected):
    claim = _claim_for(db)
    response_kwargs = {"json": _receipt(claim, **overrides)}
    if headers:
        response_kwargs["headers"] = headers
    result = asyncio.run(
        svc.deliver(
            claim,
            _settings(),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, **response_kwargs)),
        )
    )
    assert result == (expected, None)


def test_deliver_oversize_and_non_200_and_transport_error(db):
    claim = _claim_for(db)
    # content-encoding 必须 identity：用真实 gzip 字节构造，避免 httpx 构造期解码报错。
    gzip_response = httpx.Response(
        200, content=gzip.compress(b"{}"), headers={"content-encoding": "gzip"}
    )
    assert asyncio.run(
        svc.deliver(
            claim, _settings(),
            transport=httpx.MockTransport(lambda r: gzip_response),
        )
    ) == ("invalid_receipt", None)
    oversize = httpx.Response(200, content=b"x" * (svc.MAX_RECEIPT_BYTES + 1))
    assert asyncio.run(
        svc.deliver(claim, _settings(), transport=httpx.MockTransport(lambda r: oversize))
    ) == ("invalid_receipt", None)
    assert asyncio.run(
        svc.deliver(
            claim, _settings(),
            transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})),
        )
    ) == ("remote_http_error", None)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connection failure", request=request)

    assert asyncio.run(
        svc.deliver(claim, _settings(), transport=httpx.MockTransport(boom))
    ) == ("transport_error", None)


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------


def test_settings_default_false_and_valid_settings_build():
    settings = Settings(_env_file=None)
    assert settings.shop_cycle_sell_enabled is False
    assert settings.shop_cycle_sell_url == ""
    valid = _settings()
    assert valid.shop_cycle_sell_url == URL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"shop_cycle_sell_url": "https://shop.example/api/integrations/catalog/parts/sync"},
        {"shop_cycle_sell_url": "https://shop.example/api/integrations/cycle/sell-intents?x=1"},
        {"shop_cycle_sell_url": "https://user:pw@shop.example/api/integrations/cycle/sell-intents"},
        {"shop_cycle_sell_token": SecretStr("short-token")},
        {
            "shop_cycle_sell_enabled": True,
            "shop_cycle_sell_url": "",
            "shop_cycle_sell_token": SecretStr(TOKEN),
        },
        {
            "shop_cycle_sell_token": SecretStr(""),
            "shop_cycle_sell_url": URL,
            "shop_cycle_sell_enabled": True,
        },
        {
            "shop_cycle_sell_url": "http://shop.example/api/integrations/cycle/sell-intents",
        },
        {"shop_catalog_sync_token": SecretStr(TOKEN)},
    ],
)
def test_settings_reject_invalid_cycle_sell_configuration(kwargs):
    base = {
        "shop_cycle_sell_url": URL,
        "shop_cycle_sell_token": SecretStr(TOKEN),
        "shop_cycle_sell_enabled": True,
    }
    base.update(kwargs)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **base)


def test_settings_allow_loopback_http_in_dev():
    settings = Settings(
        _env_file=None,
        shop_cycle_sell_url="http://127.0.0.1:18080/api/integrations/cycle/sell-intents",
        shop_cycle_sell_token=SecretStr(TOKEN),
        shop_cycle_sell_enabled=True,
    )
    assert settings.shop_cycle_sell_url.startswith("http://127.0.0.1")


# ---------------------------------------------------------------------------
# 端到端：worker 循环（mock deliver）与日志去敏
# ---------------------------------------------------------------------------


def test_run_worker_delivers_pending_intent_and_logs_without_token(db, monkeypatch):
    unit = _unit(db)
    intent = _intent(db, unit)
    settings = _settings()

    async def fake_deliver(claim, settings, *, transport=None):
        return "applied", None

    monkeypatch.setattr(svc, "deliver", fake_deliver)
    log_calls: list[tuple[str, tuple]] = []

    class FakeLog:
        def info(self, message, *args, **kwargs):
            log_calls.append((message, args))

        def warning(self, message, *args, **kwargs):
            log_calls.append((message, args))

    monkeypatch.setattr(svc, "log", FakeLog())

    async def scenario() -> None:
        stop, wake = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(svc.run_worker(settings, stop, wake))
        await asyncio.sleep(0.5)
        stop.set()
        wake.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    row = _row(db, intent.delivery_id)
    assert row.state == "delivered"
    assert row.receipt_status == "applied"
    text = " ".join(f"{message} {args}" for message, args in log_calls)
    assert TOKEN not in text
    assert "cycle_sell_intent" in text
    assert "applied" in text
    # 日志夹具不出现回执 body / traceback
    assert "schemaVersion" not in text
    assert "Traceback" not in text


def test_worker_start_close_with_enabled_settings(db, monkeypatch):
    unit = _unit(db)
    intent = _intent(db, unit)
    settings = _settings()

    async def fake_deliver(claim, settings, *, transport=None):
        return "applied", None

    monkeypatch.setattr(svc, "deliver", fake_deliver)

    async def scenario() -> None:
        worker = svc.SellIntentWorker(settings)
        await worker.start()
        assert worker.task is not None
        await asyncio.sleep(0.4)
        await worker.close()
        assert worker.task is None

    asyncio.run(scenario())
    assert _row(db, intent.delivery_id).state == "delivered"


def test_read_status_contract(db):
    unit = _unit(db)
    intent = _intent(db, unit)
    status = svc.read_status(db, unit.id)
    assert status["unit_id"] == unit.id
    assert status["status"] == "pending"
    assert status["deliveryId"] == str(intent.delivery_id)
    assert status["attempts"] == 0
    assert svc.read_status(db, unit.id + 999999)["status"] == "not_queued"
