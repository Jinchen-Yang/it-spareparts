"""作废勾选：未加库存、409、超时和 missing_operator 延迟入库。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update

from app.models.maintenance_cycle import MaintenanceCycleSellIntent
from app.models.maintenance_repair import MaintenanceRepairRecord
from app.services import cycle_sell_delivery as delivery
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt


def _client(db, username: str, **overrides):
    return client_for(db, username=username, role="readonly", overrides=overrides or {})


def _manager(db):
    return _client(db, "void-rule-mgr", page_cycle_manage=True)


def _submit(client, part_id: int, fixed: int = 1, scrap: int = 0):
    response = client.post(
        "/api/cycle/repair-records",
        json={"part_id": part_id, "fixed_qty": fixed, "scrap_qty": scrap},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _void(client, record: dict, *, checked: bool):
    return client.post(
        f"/api/cycle/repair-records/{record['record_id']}/void",
        json={
            "expected_version": record["version"],
            "reason": "作废规则",
            "shop_stock_manually_reduced": checked,
        },
        headers={"Idempotency-Key": uuid4().hex},
    )


def _intent(db, record_id: str) -> MaintenanceCycleSellIntent:
    db.expire_all()
    row = db.scalar(
        select(MaintenanceCycleSellIntent).where(
            MaintenanceCycleSellIntent.record_id == record_id
        )
    )
    assert row is not None
    return row


def _prepare(db):
    project = _project(db, "VCHK")
    part = _part(db, "VCHK-PN")
    _receipt(db, project, part, "8", condition="坏品")
    db.commit()
    return part


def _mark(db, record_id: str, **values) -> None:
    db.execute(
        update(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.record_id == record_id)
        .values(**values)
    )
    db.commit()


def test_missing_operator_void_rejects_checkbox_and_attest_does_not_change_qty(db):
    part = _prepare(db)
    manager = _manager(db)
    buyer = _client(db, "void-rule-buyer", page_cycle_wip=True)
    first = _submit(manager, part.id)
    _mark(
        db, first["record_id"],
        state="blocked", receipt_status="blocked", block_code="missing_operator",
        attempts=2, last_error_code=None,
    )
    rejected = _void(manager, first, checked=True)
    assert rejected.status_code == 409
    assert "不要勾选" in rejected.json()["detail"]
    assert db.get(MaintenanceRepairRecord, first["record_id"]).status == "active"

    accepted = _void(manager, first, checked=False)
    assert accepted.status_code == 200, accepted.text
    stored = db.get(MaintenanceRepairRecord, accepted.json()["record_id"])
    assert stored.status == "voided"
    assert stored.shop_adjusted_manually is False
    intent = _intent(db, first["record_id"])
    assert intent.state == "failed"
    assert intent.last_error_code == "record_voided"
    assert intent.block_code == "missing_operator"
    detail = manager.get(f"/api/cycle/repair-records/{first['record_id']}").json()
    assert detail["sell_intent"]["shop_closeout"] == "awaiting_shop_reversal"
    pending = buyer.get("/api/cycle/summary", params={"part_ids": str(part.id)}).json()["items"][0]["pending_qty"]
    attested = manager.post(
        f"/api/cycle/repair-records/{first['record_id']}/attest-shop-adjustment",
        json={"expected_version": accepted.json()["version"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert attested.status_code == 200, attested.text
    assert attested.json()["sell_intent"]["shop_closeout"] is None
    assert db.get(MaintenanceRepairRecord, first["record_id"]).shop_adjusted_manually is True
    after = buyer.get("/api/cycle/summary", params={"part_ids": str(part.id)}).json()["items"][0]["pending_qty"]
    assert after == pending


def test_remote_busy_and_preserved_catalog_block_do_not_need_checkbox(db):
    part = _prepare(db)
    manager = _manager(db)
    busy = _submit(manager, part.id)
    _mark(
        db, busy["record_id"],
        state="pending", attempts=2, last_error_code="remote_busy",
        receipt_status=None, block_code=None,
        available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    voided = _void(manager, busy, checked=False)
    assert voided.status_code == 200, voided.text

    blocked = _submit(manager, part.id)
    claim_record = blocked["record_id"]
    _mark(
        db, claim_record,
        state="pending", attempts=2, receipt_status="blocked",
        block_code="missing_catalog_identity", last_error_code="deadline_exceeded",
        available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    kept = _void(manager, blocked, checked=False)
    assert kept.status_code == 200, kept.text


def test_timeout_without_unstocked_evidence_requires_checkbox(db):
    part = _prepare(db)
    manager = _manager(db)
    created = _submit(manager, part.id)
    _mark(
        db, created["record_id"],
        state="pending", attempts=1, receipt_status=None, block_code=None,
        last_error_code="deadline_exceeded",
        available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    missing = _void(manager, created, checked=False)
    assert missing.status_code == 409
    assert "手工减库存" in missing.json()["detail"]
    assert db.get(MaintenanceRepairRecord, created["record_id"]).status == "active"
    accepted = _void(manager, created, checked=True)
    assert accepted.status_code == 200, accepted.text
    assert db.get(MaintenanceRepairRecord, created["record_id"]).shop_adjusted_manually is True


def test_active_delivering_lease_blocks_void(db):
    part = _prepare(db)
    manager = _manager(db)
    created = _submit(manager, part.id)
    intent = _intent(db, created["record_id"])
    claim = delivery.claim_one()
    assert claim is not None and claim.delivery_id == intent.delivery_id
    denied = _void(manager, created, checked=True)
    assert denied.status_code == 409
    assert denied.json()["detail"] == "推送进行中，请稍后再作废"
    assert db.get(MaintenanceRepairRecord, created["record_id"]).status == "active"
