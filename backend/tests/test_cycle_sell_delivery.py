"""可售意图 v2：金样例当作 mock 商城，开关默认关闭。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select, update

from app.config import Settings
from app.models.dimensions import DimPart
from app.models.maintenance_cycle import MaintenanceCycleSellIntent
from app.models.maintenance_repair import MaintenanceRepairRecord
from app.services import cycle_sell_delivery as svc

TOKEN = "synthetic-cycle-sell-token-for-tests"
URL = "https://shop.example/api/integrations/cycle/sell-intents"
EXAMPLES = json.loads(
    (Path(__file__).resolve().parents[2] / "docs/integrations/cycle-sell-intent-v2-examples.json").read_text()
)
RECEIPT_KEYS = ["schemaVersion", "deliveryId", "unitId", "partId", "status", "blockCode"]
CASE_IDS = [
    "applied",
    "unchanged",
    "blocked_missing_catalog_identity",
    "blocked_missing_operator",
    "validation_422",
    "merge_409_retryable",
    "applied_v1",
    "conflict_v2_bad",
]


def _settings(**kwargs) -> Settings:
    return Settings(
        _env_file=None,
        shop_cycle_sell_url=URL,
        shop_cycle_sell_token=SecretStr(TOKEN),
        shop_cycle_sell_enabled=True,
        **kwargs,
    )


def _case(case_id: str) -> dict:
    return next(item for item in EXAMPLES["cases"] if item["id"] == case_id)


def _terminal(result: str) -> str:
    if result in {"applied", "unchanged"}:
        return "delivered"
    if result == "blocked":
        return "blocked"
    if result in {"conflict", "invalid_request"}:
        return "failed"
    if result == "remote_busy":
        return "pending"
    return "pending"


def _part(db, part_id: int, pn: str) -> DimPart:
    existing = db.get(DimPart, part_id)
    if existing is not None:
        return existing
    part = DimPart(id=part_id, pn_std=pn, description="sell", status="active")
    db.add(part)
    db.flush()
    return part


def _intent_for_case(db, case: dict, *, condition: str = "good") -> MaintenanceCycleSellIntent:
    request = case["request"]
    part = _part(db, int(request["partId"]), f"SELL-{request['partId']}")
    wire = int(request["unitId"])
    record = MaintenanceRepairRecord(
        wire_unit_id=wire,
        record_no=f"WX{wire:08d}",
        part_id=part.id,
        pn=part.pn_std,
        fixed_qty=int(request["qty"]),
        scrap_qty=0,
        operator="sell-test",
    )
    db.add(record)
    db.flush()
    intent = MaintenanceCycleSellIntent(
        delivery_id=UUID(request["deliveryId"]),
        record_id=record.record_id,
        part_id=part.id,
        qty=int(request["qty"]),
        wire_unit_id=wire,
        condition_bucket=condition,
        state="pending",
    )
    db.add(intent)
    db.commit()
    return intent


def _row(db, delivery_id) -> MaintenanceCycleSellIntent:
    db.expire_all()
    row = db.get(MaintenanceCycleSellIntent, delivery_id)
    assert row is not None
    return row


def _force_due(db, delivery_id) -> None:
    db.execute(
        update(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.delivery_id == delivery_id)
        .values(available_at=datetime.now(timezone.utc) - timedelta(seconds=5))
    )
    db.commit()


def _mock(case: dict, captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["sent"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        body = case.get("responseBody")
        headers = case.get("responseHeaders") or {}
        if body is None:
            return httpx.Response(case["httpStatus"], headers=headers)
        return httpx.Response(case["httpStatus"], json=body, headers=headers)

    return httpx.MockTransport(handler)


def _run(case: dict, claim: svc.Claim) -> dict:
    captured: dict = {}
    result, block = asyncio.run(
        svc.deliver(claim, _settings(), transport=_mock(case, captured))
    )
    captured["result"] = result
    captured["block"] = block
    return captured


def test_examples_file_is_the_eight_case_contract():
    assert EXAMPLES["receiptKeys"] == RECEIPT_KEYS
    assert [item["id"] for item in EXAMPLES["cases"]] == CASE_IDS
    for case in EXAMPLES["cases"]:
        if case["httpStatus"] == 200:
            assert list(case["responseBody"]) == RECEIPT_KEYS


def test_parser_matches_every_example_terminal():
    for case in EXAMPLES["cases"]:
        body = json.dumps(case["responseBody"]).encode() if case.get("responseBody") else b""
        result, block = svc.interpret_shop_response(
            case["request"], case["httpStatus"], body
        )
        assert _terminal(result) == case["itspTerminal"], case["id"]
        if case["id"] == "blocked_missing_operator":
            assert block == "missing_operator"
        if case["id"] == "blocked_missing_catalog_identity":
            assert block == "missing_catalog_identity"
        if case["id"] == "validation_422":
            assert result == "invalid_request"
        if case["id"] == "merge_409_retryable":
            assert result == "remote_busy"
        if case["id"] == "conflict_v2_bad":
            assert result == "conflict"
        if case["id"] == "unchanged":
            assert case["requiresPriorAppliedReceipt"] is False


def test_outbound_payload_is_v2_six_keys_and_examples_drive_the_worker(db):
    for case in EXAMPLES["cases"]:
        if case["id"] == "applied_v1":
            continue
        db.execute(delete(MaintenanceCycleSellIntent))
        db.execute(delete(MaintenanceRepairRecord))
        db.commit()
        condition = "good"
        intent = _intent_for_case(db, case, condition=condition)
        claim = svc.claim_one()
        assert claim is not None
        assert claim.delivery_id == intent.delivery_id
        captured = _run(case, claim)
        sent = captured["sent"]
        assert list(sent) == [
            "schemaVersion", "deliveryId", "unitId", "partId", "conditionBucket", "qty",
        ]
        assert sent["schemaVersion"] == 2
        assert sent["unitId"] == int(intent.wire_unit_id)
        assert sent["qty"] == int(intent.qty)
        assert sent["conditionBucket"] == "good"
        assert "recordId" not in sent
        assert captured["auth"] == f"Bearer {TOKEN}"
        assert svc.acknowledge(claim, captured["result"], block_code=captured["block"]) is True
        row = _row(db, intent.delivery_id)
        assert row.state == case["itspTerminal"]
        if case["id"] == "applied":
            assert row.receipt_status == "applied"
        if case["id"] == "unchanged":
            assert row.state == "delivered"
            assert row.receipt_status == "applied"
        if case["id"] == "blocked_missing_operator":
            assert row.block_code == "missing_operator"
            assert row.receipt_status == "blocked"
            assert row.available_at >= datetime.now(timezone.utc) + timedelta(seconds=290)
        if case["id"] == "validation_422":
            assert row.last_error_code == "invalid_request"
            assert svc.claim_one() is None or _row(db, intent.delivery_id).state == "failed"
        if case["id"] == "merge_409_retryable":
            assert row.state == "pending"
            assert row.last_error_code == "remote_busy"
            assert row.available_at >= datetime.now(timezone.utc) + timedelta(seconds=4)
        if case["id"] == "conflict_v2_bad":
            assert row.state == "failed"
            assert sent["conditionBucket"] != "bad"


def test_v1_echo_to_a_v2_post_is_invalid_and_itsp_does_not_send_v1(db):
    case = _case("applied_v1")
    payload = {
        "schemaVersion": 2,
        "deliveryId": case["request"]["deliveryId"],
        "unitId": case["request"]["unitId"],
        "partId": case["request"]["partId"],
        "conditionBucket": "good",
        "qty": 1,
    }
    echoed = json.dumps(case["responseBody"]).encode()
    result, _block = svc.interpret_shop_response(payload, 200, echoed)
    assert result == "invalid_receipt"
    matched, _block = svc.interpret_shop_response(case["request"], 200, echoed)
    assert matched == "applied"


def test_extra_record_id_or_missing_unit_id_is_invalid_receipt():
    sent = _case("applied")["request"]
    extra = dict(_case("applied")["responseBody"])
    extra["recordId"] = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    result, _block = svc.interpret_shop_response(sent, 200, json.dumps(extra).encode())
    assert result == "invalid_receipt"
    missing = dict(_case("applied")["responseBody"])
    missing.pop("unitId")
    result, _block = svc.interpret_shop_response(sent, 200, json.dumps(missing).encode())
    assert result == "invalid_receipt"


def test_missing_operator_then_unchanged_delivers_without_prior_applied(db):
    case = _case("blocked_missing_operator")
    intent = _intent_for_case(db, case)
    claim = svc.claim_one()
    captured = _run(case, claim)
    assert svc.acknowledge(claim, captured["result"], block_code=captured["block"]) is True
    _force_due(db, intent.delivery_id)
    second = svc.claim_one()
    assert second is not None
    assert second.previous_receipt_status == "blocked"
    unchanged = _case("unchanged")
    unchanged = {
        **unchanged,
        "request": case["request"],
        "responseBody": {**unchanged["responseBody"], **{
            "deliveryId": case["request"]["deliveryId"],
            "unitId": case["request"]["unitId"],
            "partId": case["request"]["partId"],
        }},
    }
    captured = _run(unchanged, second)
    assert captured["result"] == "unchanged"
    assert svc.acknowledge(second, "unchanged") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "delivered"
    assert row.receipt_status == "applied"


def test_catalog_block_survives_timeout(db):
    case = _case("blocked_missing_catalog_identity")
    intent = _intent_for_case(db, case)
    claim = svc.claim_one()
    captured = _run(case, claim)
    assert svc.acknowledge(claim, "blocked", block_code=captured["block"]) is True
    _force_due(db, intent.delivery_id)
    second = svc.claim_one()
    assert svc.acknowledge(second, "deadline_exceeded") is True
    row = _row(db, intent.delivery_id)
    assert row.state == "pending"
    assert row.receipt_status == "blocked"
    assert row.block_code == "missing_catalog_identity"
    assert row.attempts == 2


def test_disabled_worker_does_not_send(db):
    case = _case("applied")
    intent = _intent_for_case(db, case)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    async def scenario() -> None:
        worker = svc.SellIntentWorker(Settings(_env_file=None))
        await worker.start()
        assert worker.task is None
        await worker.close()

    asyncio.run(scenario())
    assert calls["n"] == 0
    assert _row(db, intent.delivery_id).state == "pending"
    assert Settings(_env_file=None).shop_cycle_sell_enabled is False


def test_late_ack_after_failed_changes_nothing(db):
    case = _case("applied")
    intent = _intent_for_case(db, case)
    claim = svc.claim_one()
    db.execute(
        update(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.delivery_id == intent.delivery_id)
        .values(state="failed", lease_token=None, lease_until=None, last_error_code="record_voided")
    )
    db.commit()
    assert svc.acknowledge(claim, "applied") is False
    assert _row(db, intent.delivery_id).state == "failed"
