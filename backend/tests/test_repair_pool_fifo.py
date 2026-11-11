"""待修扣数按坏返行先进先出，并发合计不得超过收货数。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.models.maintenance_repair import (
    MaintenanceRepairRecord,
    MaintenanceRepairRecordSource,
)
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt


def _repairer(db, username: str):
    return client_for(
        db, username=username, role="readonly", overrides={"page_cycle_repair": True}
    )


def _submit(client, part_id: int, fixed: int, scrap: int = 0):
    return client.post(
        "/api/cycle/repair-records",
        json={"part_id": part_id, "fixed_qty": fixed, "scrap_qty": scrap},
        headers={"Idempotency-Key": uuid4().hex},
    )


def test_earlier_return_line_is_consumed_first(db):
    project = _project(db, "FIFO")
    part = _part(db, "FIFO-PN")
    older = _receipt(
        db, project, part, "2", condition="坏品",
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    newer = _receipt(
        db, project, part, "3", condition="坏件",
        occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    db.commit()
    with _repairer(db, "fifo-repairer") as repairer:
        created = _submit(repairer, part.id, 3)
        assert created.status_code == 201, created.text
        record_id = created.json()["record_id"]
    db.expire_all()
    sources = list(
        db.scalars(
            select(MaintenanceRepairRecordSource).where(
                MaintenanceRepairRecordSource.record_id == record_id
            )
        ).all()
    )
    taken = {row.receipt_id: row.qty for row in sources}
    assert taken[older.rkd_line_id] == 2
    assert taken[newer.rkd_line_id] == 1
    assert sum(taken.values()) == 3


def test_concurrent_submits_cannot_exceed_received_qty(db):
    project = _project(db, "RACE")
    part = _part(db, "RACE-PN")
    receipt = _receipt(db, project, part, "4", condition="坏品")
    db.commit()
    clients = [_repairer(db, "race-a"), _repairer(db, "race-b")]
    part_id = part.id

    def attempt(client):
        return _submit(client, part_id, 4)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, clients))
    assert sorted(response.status_code for response in results) == [201, 409]
    db.expire_all()
    deducted = int(
        db.scalar(select(func.coalesce(func.sum(MaintenanceRepairRecordSource.qty), 0)))
        or 0
    )
    assert deducted == 4
    assert deducted <= int(receipt.qty)
    assert db.scalar(select(func.count()).select_from(MaintenanceRepairRecord)) == 1


def test_request_above_remainder_does_not_shrink(db):
    project = _project(db, "REM")
    part = _part(db, "REM-PN")
    _receipt(db, project, part, "2", condition="坏品")
    db.commit()
    with _repairer(db, "rem-repairer") as repairer:
        rejected = _submit(repairer, part.id, 3)
        assert rejected.status_code == 409
        assert db.scalar(select(func.count()).select_from(MaintenanceRepairRecord)) == 0
        assert repairer.get("/api/cycle/pool").json()["items"][0]["pending_qty"] == 2
