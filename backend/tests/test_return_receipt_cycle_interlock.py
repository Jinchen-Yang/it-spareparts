"""返还收货变更不得让合格件数小于有效维修扣数（D-0058）。"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import inspect, select

from app.db import engine
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_repair import MaintenanceRepairRecordSource
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt
from tests.test_maintenance_return_receipts_api import _client as _receipt_client

DETAIL = "该返还收货已有维修扣数，变更后将少于已扣件数"


def _repairer(db, username: str):
    return client_for(
        db, username=username, role="readonly", overrides={"page_cycle_repair": True}
    )


def _patch(client, receipt_id: str, version: int, **changes):
    return client.patch(
        f"/api/maintenance/return-receipts/{receipt_id}",
        json={"version": version, "reason": "合成互锁调整", **changes},
    )


def _void(client, receipt_id: str, version: int):
    return client.post(
        f"/api/maintenance/return-receipts/{receipt_id}/void",
        json={"version": version, "reason": "合成互锁作废"},
    )


def _row(db, receipt_id: str) -> MaintenanceRkdReturnLine:
    db.expire_all()
    row = db.get(MaintenanceRkdReturnLine, receipt_id)
    assert row is not None
    return row


def _deduct(db, receipt, part, qty: int):
    repairer = _repairer(db, f"lock-{receipt.rkd_line_id[:8]}")
    created = repairer.post(
        "/api/cycle/repair-records",
        json={"part_id": part.id, "fixed_qty": qty, "scrap_qty": 0},
        headers={"Idempotency-Key": f"lock-{receipt.rkd_line_id}"},
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_shrink_void_condition_and_part_change_are_rejected_when_deducted(db):
    project = _project(db, "LOCK")
    part = _part(db, "LOCK-PN")
    other = _part(db, "LOCK-OTHER")
    receipt = _receipt(db, project, part, "4", condition="坏品")
    db.commit()
    admin = _receipt_client(db, username="lock-admin")
    _deduct(db, receipt, part, 3)
    row = _row(db, receipt.rkd_line_id)

    shrunk = _patch(admin, receipt.rkd_line_id, row.version, qty=2)
    assert shrunk.status_code == 409
    assert shrunk.json()["detail"] == DETAIL
    row = _row(db, receipt.rkd_line_id)
    assert row.qty == Decimal("4.000")
    assert row.version == 1

    finished = _patch(admin, receipt.rkd_line_id, row.version, condition="成品")
    assert finished.status_code == 409
    assert _row(db, receipt.rkd_line_id).test_result == "坏品"

    moved = _patch(admin, receipt.rkd_line_id, row.version, part_id=other.id, pn=other.pn_std)
    assert moved.status_code == 409
    assert _row(db, receipt.rkd_line_id).part_id == part.id

    voided = _void(admin, receipt.rkd_line_id, row.version)
    assert voided.status_code == 409
    assert _row(db, receipt.rkd_line_id).line_status == "active"


def test_other_line_of_same_pn_can_still_change(db):
    project = _project(db, "OTHER")
    part = _part(db, "OTHER-PN")
    first = _receipt(
        db, project, part, "2", condition="坏品",
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    second = _receipt(
        db, project, part, "5", condition="坏品",
        occurred_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    db.commit()
    admin = _receipt_client(db, username="other-admin")
    _deduct(db, first, part, 2)
    second_row = _row(db, second.rkd_line_id)
    changed = _patch(admin, second.rkd_line_id, second_row.version, qty=4)
    assert changed.status_code == 200, changed.text
    assert _row(db, second.rkd_line_id).qty == Decimal("4.000")
    assert _row(db, first.rkd_line_id).qty == Decimal("2.000")


def test_void_without_deduction_does_not_create_units_and_condition_leaves_pool(db):
    project = _project(db, "FREE")
    part = _part(db, "FREE-PN")
    part.description = "风扇"
    receipt = _receipt(db, project, part, "2", condition="坏品")
    db.commit()
    admin = _receipt_client(db, username="free-admin")
    repairer = _repairer(db, "free-repairer")
    assert repairer.get("/api/cycle/pool").json()["items"][0]["pending_qty"] == 2
    left = _patch(admin, receipt.rkd_line_id, receipt.version, condition="成品")
    assert left.status_code == 200, left.text
    assert repairer.get("/api/cycle/pool").json()["total"] == 0
    row = _row(db, receipt.rkd_line_id)
    voided = _void(admin, receipt.rkd_line_id, row.version)
    assert voided.status_code == 200, voided.text
    assert not inspect(engine).has_table("maintenance_cycle_unit")
    assert db.scalar(select(MaintenanceRepairRecordSource)) is None
