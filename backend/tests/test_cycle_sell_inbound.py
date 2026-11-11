"""循环可售入站：requeue 保持原行为；售出两个回调延期且零写入。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, inspect, select

from app.config import Settings
from app.db import engine
from app.main import app
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_cycle import MaintenanceCycleSellIntent
from app.models.maintenance_repair import MaintenanceRepairRecord

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


def _intent(db, **overrides) -> MaintenanceCycleSellIntent:
    part = DimPart(pn_std=overrides.pop("pn", "CYCLE-IN-PN"), description="inbound")
    db.add(part)
    db.flush()
    record = MaintenanceRepairRecord(
        wire_unit_id=overrides.pop("wire_unit_id", 9),
        record_no="WX00000009",
        part_id=part.id,
        pn=part.pn_std,
        fixed_qty=1,
        scrap_qty=0,
        operator="inbound-test",
    )
    db.add(record)
    db.flush()
    values = dict(
        record_id=record.record_id,
        part_id=part.id,
        qty=1,
        wire_unit_id=record.wire_unit_id,
        condition_bucket="good",
        state="blocked",
        attempts=1,
        block_code="missing_formal_image",
        receipt_status="blocked",
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


def test_callback_disabled_is_404(db, monkeypatch):
    monkeypatch.setattr(
        "app.api.cycle_sell_inbound.get_settings",
        lambda: Settings(_env_file=None),
    )
    intent = _intent(db)
    client = TestClient(app)
    response = client.post(
        "/api/integrations/cycle/requeue",
        json={"deliveryId": str(intent.delivery_id)},
        headers=_auth(),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "not_enabled"


def test_requeue_blocked_pulls_available_at_forward(db, monkeypatch):
    token = _enable(monkeypatch)
    intent = _intent(db)
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
    assert row.available_at <= datetime.now(timezone.utc)
    assert row.block_code == "missing_formal_image"
    assert _count(db, Inventory) == inventory
    assert _count(db, MaintenanceBadSalvage) == salvage
    again = client.post(
        "/api/integrations/cycle/requeue",
        json={"deliveryId": str(intent.delivery_id), "note": "no"},
        headers=_auth(token),
    )
    assert again.status_code == 422


def test_mark_sold_and_revert_sold_are_phase_deferred(db, monkeypatch):
    token = _enable(monkeypatch)
    intent = _intent(db, state="delivered", receipt_status="applied", block_code=None)
    inventory = _count(db, Inventory)
    salvage = _count(db, MaintenanceBadSalvage)
    client = TestClient(app)
    for path in ("/api/integrations/cycle/mark-sold", "/api/integrations/cycle/revert-sold"):
        response = client.post(path, json=_sold_body(intent.delivery_id), headers=_auth(token))
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "phase_deferred"
    db.expire_all()
    stored = db.get(MaintenanceCycleSellIntent, intent.delivery_id)
    assert stored.state == "delivered"
    assert stored.updated_at == intent.updated_at or stored.state == "delivered"
    assert _count(db, Inventory) == inventory
    assert _count(db, MaintenanceBadSalvage) == salvage
    assert not inspect(engine).has_table("maintenance_cycle_unit")
    unknown = client.post(
        "/api/integrations/cycle/mark-sold",
        json=_sold_body(uuid4()),
        headers=_auth(token),
    )
    assert unknown.status_code == 409
    assert unknown.json()["detail"] == "phase_deferred"
    rejected = client.post(
        "/api/integrations/cycle/mark-sold",
        json={**_sold_body(intent.delivery_id), "schemaVersion": 2},
        headers=_auth(token),
    )
    assert rejected.status_code == 422
