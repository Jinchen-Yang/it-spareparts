"""循环可售入站 §10：requeue、mark-sold、revert-sold、主管查询。不连商城。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from app.config import Settings
from app.main import app
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_cycle import (
    MaintenanceCycleEvent,
    MaintenanceCycleSellIntent,
    MaintenanceCycleUnit,
)
from tests.boss_board_helpers import client_for

TOKEN = "synthetic-cycle-callback-token-32"


def _digest(token: str = TOKEN) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _enable(monkeypatch, token: str = TOKEN) -> str:
    digest = _digest(token)

    def fake():
        return Settings(
            _env_file=None,
            cycle_sell_callback_token_sha256=SecretStr(digest),
        )

    monkeypatch.setattr("app.api.cycle_sell_inbound.get_settings", fake)
    return token


def _unit(db, **overrides) -> MaintenanceCycleUnit:
    part = DimPart(pn_std=overrides.pop("pn", "CYCLE-IN-PN"), description="inbound")
    db.add(part)
    db.flush()
    values = dict(
        part_id=part.id,
        pn=part.pn_std,
        station="closed",
        outcome="to_shop",
        condition_bucket="good",
        source_kind="external",
        serial_number="SN-HIDDEN",
        closed_at=datetime.now(timezone.utc),
        created_by="inbound-test",
        updated_by="inbound-test",
    )
    values.update(overrides)
    unit = MaintenanceCycleUnit(**values)
    db.add(unit)
    db.flush()
    return unit


def _intent(db, unit: MaintenanceCycleUnit, **overrides) -> MaintenanceCycleSellIntent:
    values = dict(
        unit_id=unit.id,
        part_id=unit.part_id,
        qty=Decimal(1),
        condition_bucket="good",
        state="delivered",
        attempts=1,
        receipt_status="applied",
        available_at=datetime.now(timezone.utc) + timedelta(hours=6),
    )
    values.update(overrides)
    row = MaintenanceCycleSellIntent(**values)
    db.add(row)
    db.commit()
    return row


def _count(db, model) -> int:
    db.expire_all()
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _events(db, unit_id: int, event_type: str) -> list[MaintenanceCycleEvent]:
    db.expire_all()
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


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sold_body(delivery_id, line: str = "line-1") -> dict:
    return {
        "schemaVersion": 1,
        "deliveryId": str(delivery_id),
        "shopOrderLineId": line,
        "qty": 1,
    }


def test_callback_hash_rejects_outbound_token_reuse():
    token = "o" * 32
    with pytest.raises(ValidationError, match="不能复用出站凭据"):
        Settings(
            _env_file=None,
            shop_cycle_sell_token=SecretStr(token),
            cycle_sell_callback_token_sha256=SecretStr(_digest(token)),
        )


def test_callback_disabled_is_404_and_does_not_write(db, monkeypatch):
    monkeypatch.setattr(
        "app.api.cycle_sell_inbound.get_settings",
        lambda: Settings(_env_file=None),
    )
    unit = _unit(db)
    intent = _intent(db, unit, state="blocked", block_code="missing_list_price")
    before = intent.available_at
    client = TestClient(app)
    response = client.post(
        "/api/integrations/cycle/requeue",
        json={"deliveryId": str(intent.delivery_id)},
        headers=_auth(),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "not_enabled"
    db.expire_all()
    row = db.get(MaintenanceCycleSellIntent, intent.delivery_id)
    assert row.available_at == before
    assert row.state == "blocked"


def test_requeue_blocked_pulls_available_at_forward(db, monkeypatch):
    token = _enable(monkeypatch)
    unit = _unit(db)
    intent = _intent(db, unit, state="blocked", block_code="missing_formal_image")
    inventory = _count(db, Inventory)
    salvage = _count(db, MaintenanceBadSalvage)
    client = TestClient(app)
    response = client.post(
        "/api/integrations/cycle/requeue",
        json={"deliveryId": str(intent.delivery_id)},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requeued"] is True
    assert body["status"] == "blocked"
    assert body["schemaVersion"] == 1
    db.expire_all()
    row = db.get(MaintenanceCycleSellIntent, intent.delivery_id)
    assert row.block_code == "missing_formal_image"
    assert row.available_at <= datetime.now(timezone.utc)
    assert _count(db, Inventory) == inventory
    assert _count(db, MaintenanceBadSalvage) == salvage

    again = client.post(
        "/api/integrations/cycle/requeue",
        json={"deliveryId": str(intent.delivery_id), "note": "no"},
        headers=_auth(token),
    )
    assert again.status_code == 422


def test_requeue_delivered_is_idempotent(db, monkeypatch):
    token = _enable(monkeypatch)
    unit = _unit(db)
    intent = _intent(db, unit, state="delivered")
    stamp = intent.available_at
    client = TestClient(app)
    response = client.post(
        "/api/integrations/cycle/requeue",
        json={"deliveryId": str(intent.delivery_id)},
        headers=_auth(token),
    )
    assert response.status_code == 200
    assert response.json()["requeued"] is False
    assert response.json()["nextAttemptAt"] is None
    db.expire_all()
    row = db.get(MaintenanceCycleSellIntent, intent.delivery_id)
    assert row.available_at == stamp
    missing = client.post(
        "/api/integrations/cycle/requeue",
        json={"deliveryId": "00000000-0000-0000-0000-000000000000"},
        headers=_auth(token),
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "not_found"
    wrong = client.post(
        "/api/integrations/cycle/requeue",
        json={"deliveryId": str(intent.delivery_id)},
        headers=_auth("x" * 32),
    )
    assert wrong.status_code == 401


def test_mark_sold_callback_is_idempotent_and_revert_restores_outcome(db, monkeypatch):
    token = _enable(monkeypatch)
    unit = _unit(db)
    intent = _intent(db, unit, state="delivered")
    client = TestClient(app)
    first = client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(intent.delivery_id),
        headers=_auth(token),
    )
    assert first.status_code == 200, first.text
    assert first.json()["idempotent"] is False
    assert first.json()["outcome"] == "sold"
    second = client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(intent.delivery_id),
        headers=_auth(token),
    )
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    assert len(_events(db, unit.id, "mark_sold")) == 1
    db.expire_all()
    assert db.get(MaintenanceCycleUnit, unit.id).outcome == "sold"
    assert db.get(MaintenanceCycleSellIntent, intent.delivery_id).state == "delivered"

    reverted = client.post(
        "/api/integrations/cycle/revert-sold",
        json=_sold_body(intent.delivery_id),
        headers=_auth(token),
    )
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["outcome"] == "to_shop"
    db.expire_all()
    restored = db.get(MaintenanceCycleUnit, unit.id)
    assert restored.station == "closed"
    assert restored.outcome == "to_shop"
    again = client.post(
        "/api/integrations/cycle/revert-sold",
        json=_sold_body(intent.delivery_id),
        headers=_auth(token),
    )
    assert again.status_code == 200
    assert again.json()["idempotent"] is True
    assert len(_events(db, unit.id, "revert_sold")) == 1
    conflict = client.post(
        "/api/integrations/cycle/revert-sold",
        json=_sold_body(intent.delivery_id, "other-line"),
        headers=_auth(token),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "remote_conflict"


def test_mark_sold_rejects_delivering_failed_and_manual_sale(db, monkeypatch):
    token = _enable(monkeypatch)
    client = TestClient(app)
    delivering = _intent(
        db,
        _unit(db, pn="IN-DELIVER"),
        state="delivering",
        lease_token=uuid4(),
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    response = client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(delivering.delivery_id),
        headers=_auth(token),
    )
    assert response.status_code == 409
    db.expire_all()
    assert db.get(MaintenanceCycleUnit, delivering.unit_id).outcome == "to_shop"

    failed = _intent(db, _unit(db, pn="IN-FAILED"), state="failed")
    assert client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(failed.delivery_id),
        headers=_auth(token),
    ).status_code == 409

    manual = _unit(db, pn="IN-MANUAL", outcome="sold")
    intent = _intent(db, manual, state="delivered")
    db.add(MaintenanceCycleEvent(
        unit_id=manual.id,
        event_type="mark_sold",
        payload={"outcome": "sold"},
        actor="manager",
    ))
    db.commit()
    manual_response = client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(intent.delivery_id),
        headers=_auth(token),
    )
    assert manual_response.status_code == 409

    pending = _intent(db, _unit(db, pn="IN-PENDING"), state="pending", receipt_status=None)
    pending_response = client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(pending.delivery_id, "pending-line"),
        headers=_auth(token),
    )
    assert pending_response.status_code == 200, pending_response.text
    db.expire_all()
    row = db.get(MaintenanceCycleSellIntent, pending.delivery_id)
    assert row.state == "failed"
    assert row.last_error_code == "unit_sold_by_callback"


def test_revert_of_callback_sale_accepts_a_later_order_line(db, monkeypatch):
    token = _enable(monkeypatch)
    unit = _unit(db, pn="IN-LOST-RECEIPT")
    intent = _intent(db, unit, state="pending", receipt_status=None)
    client = TestClient(app)
    sold = client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(intent.delivery_id, "first-line"),
        headers=_auth(token),
    )
    assert sold.status_code == 200, sold.text
    db.expire_all()
    failed = db.get(MaintenanceCycleSellIntent, intent.delivery_id)
    assert failed.state == "failed"
    assert failed.last_error_code == "unit_sold_by_callback"

    reverted = client.post(
        "/api/integrations/cycle/revert-sold",
        json=_sold_body(intent.delivery_id, "first-line"),
        headers=_auth(token),
    )
    assert reverted.status_code == 200, reverted.text
    db.expire_all()
    restored = db.get(MaintenanceCycleSellIntent, intent.delivery_id)
    assert restored.state == "delivered"
    assert restored.last_error_code is None
    assert db.get(MaintenanceCycleUnit, unit.id).outcome == "to_shop"

    again = client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(intent.delivery_id, "second-line"),
        headers=_auth(token),
    )
    assert again.status_code == 200, again.text
    db.expire_all()
    assert db.get(MaintenanceCycleUnit, unit.id).outcome == "sold"
    assert len(_events(db, unit.id, "mark_sold")) == 2


def test_sell_intent_status_is_manager_only_and_hides_serial_from_worker(db):
    unit = _unit(db, station="pool", outcome=None, closed_at=None)
    intent = _intent(db, unit, state="blocked", block_code="publish_disabled")
    manager = client_for(db, username="cycle-in-manager", role="readonly", overrides={
        "page_cycle_manage": True,
    })
    worker = client_for(db, username="cycle-in-recycle", role="readonly", overrides={
        "page_cycle_recycle": True,
    })
    hidden = worker.get(f"/api/cycle/units/{unit.id}")
    assert hidden.status_code == 200
    assert "serial_number" not in hidden.json()
    denied = worker.get(f"/api/cycle/units/{unit.id}/sell-intent")
    assert denied.status_code == 403
    status = manager.get(f"/api/cycle/units/{unit.id}/sell-intent")
    assert status.status_code == 200, status.text
    assert status.json()["deliveryId"] == str(intent.delivery_id)
    assert status.json()["status"] == "blocked"
    assert status.json()["blockCode"] == "publish_disabled"
    admin = client_for(db, username="cycle-in-admin", role="admin")
    assert admin.get(f"/api/cycle/units/{unit.id}/sell-intent").status_code == 403
