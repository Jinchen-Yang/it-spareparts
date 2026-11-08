"""D-0057 坏件循环池 API 契约。"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select

from app.models.inventory import Inventory
from app.models.maintenance_cycle import (
    MaintenanceCycleSellIntent,
    MaintenanceCycleUnit,
)
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt


def _cycle_client(db, username: str, **overrides):
    return client_for(db, username=username, role="readonly", overrides=overrides)


def test_external_intake_to_shop_creates_sell_intent_without_inventory_write(db):
    part = _part(db, "CYCLE-PN-A")
    before = db.scalar(select(func.count()).select_from(Inventory)) or 0
    with _cycle_client(
        db,
        "cycle-recycle-1",
        page_cycle_recycle=True,
        page_cycle_manage=True,
    ) as client:
        created = client.post(
            "/api/cycle/units/intake-external",
            json={"part_id": part.id, "qty": 2, "intake_condition": "成品"},
        )
        assert created.status_code == 201, created.text
        items = created.json()["items"]
        assert len(items) == 2
        assert items[0]["station"] == "pool"
        assert "vendor_name" in items[0]  # manage 开着

        unit_id = items[0]["id"]
        version = items[0]["version"]
        passed = client.post(
            f"/api/cycle/units/{unit_id}/self-pass",
            json={"expected_version": version},
        )
        assert passed.status_code == 200, passed.text
        assert passed.json()["station"] == "ready"
        closed = client.post(
            f"/api/cycle/units/{unit_id}/close",
            json={
                "expected_version": passed.json()["version"],
                "outcome": "to_shop",
                "condition_bucket": "good",
            },
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["outcome"] == "to_shop"
        assert closed.json()["station"] == "closed"

    intent = db.scalar(
        select(MaintenanceCycleSellIntent).where(
            MaintenanceCycleSellIntent.unit_id == unit_id
        )
    )
    assert intent is not None
    assert intent.condition_bucket == "good"
    assert intent.qty == Decimal(1)
    assert intent.state == "pending"
    after = db.scalar(select(func.count()).select_from(Inventory)) or 0
    assert after == before


def test_worker_response_hides_vendor_and_receipt(db):
    part = _part(db, "CYCLE-PN-B")
    with _cycle_client(db, "cycle-mgr", page_cycle_manage=True) as manage:
        created = manage.post(
            "/api/cycle/units/intake-external",
            json={"part_id": part.id, "qty": 1},
        )
        unit = created.json()["items"][0]
        dispatched = manage.post(
            f"/api/cycle/units/{unit['id']}/dispatch",
            json={
                "expected_version": unit["version"],
                "to": "testing",
                "assignee_username": "cycle-tester",
            },
        )
        assert dispatched.status_code == 200, dispatched.text

    with _cycle_client(db, "cycle-tester", page_cycle_test=True) as tester:
        detail = tester.get(f"/api/cycle/units/{unit['id']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert "vendor_name" not in body
        assert "source_receipt_id" not in body
        assert body["station"] == "testing"

        listed = tester.get("/api/cycle/units", params={"assignee": "me"})
        assert listed.status_code == 200
        assert listed.json()["total"] >= 1
        assert all("vendor_name" not in row for row in listed.json()["items"])


def test_repair_may_skip_retest_and_outsource_can_go_shop(db):
    part = _part(db, "CYCLE-PN-C")
    with _cycle_client(
        db, "cycle-ops", page_cycle_recycle=True, page_cycle_repair=True, page_cycle_manage=True
    ) as client:
        unit = client.post(
            "/api/cycle/units/intake-external",
            json={"part_id": part.id, "qty": 1, "intake_condition": "坏品"},
        ).json()["items"][0]
        unit = client.post(
            f"/api/cycle/units/{unit['id']}/dispatch",
            json={
                "expected_version": unit["version"],
                "to": "repairing",
                "assignee_username": "cycle-ops",
            },
        ).json()
        unit = client.post(
            f"/api/cycle/units/{unit['id']}/complete-repair",
            json={
                "expected_version": unit["version"],
                "result": "fixed",
                "next": "ready",
            },
        ).json()
        assert unit["station"] == "ready"
        assert unit["condition_bucket"] == "good"

        unit2 = client.post(
            "/api/cycle/units/intake-external",
            json={"part_id": part.id, "qty": 1},
        ).json()["items"][0]
        unit2 = client.post(
            f"/api/cycle/units/{unit2['id']}/dispatch",
            json={
                "expected_version": unit2["version"],
                "to": "outsource",
                "vendor_name": "秘密维修点",
            },
        ).json()
        assert unit2["vendor_name"] == "秘密维修点"
        closed = client.post(
            f"/api/cycle/units/{unit2['id']}/outsource-return",
            json={
                "expected_version": unit2["version"],
                "next": "to_shop",
                "condition_bucket": "bad",
            },
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["outcome"] == "to_shop"


def test_handoff_from_receipt_requires_part_id(db):
    project = _project(db, "CYCLE-PROJ")
    part = _part(db, "CYCLE-PN-D")
    receipt = _receipt(db, project, part, "3", condition="成品")
    db.commit()
    with _cycle_client(db, "cycle-handoff", page_cycle_recycle=True) as client:
        ok = client.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 2,
                "expected_receipt_version": receipt.version,
            },
        )
        assert ok.status_code == 201, ok.text
        assert len(ok.json()["items"]) == 2
        assert all(row["source_kind"] == "receipt" for row in ok.json()["items"])
        assert all("vendor_name" not in row for row in ok.json()["items"])


def test_wip_counts_by_station(db):
    part = _part(db, "CYCLE-PN-E")
    with _cycle_client(
        db, "cycle-wip-ops", page_cycle_recycle=True, page_cycle_manage=True
    ) as client:
        a = client.post(
            "/api/cycle/units/intake-external", json={"part_id": part.id, "qty": 1}
        ).json()["items"][0]
        client.post(
            f"/api/cycle/units/{a['id']}/dispatch",
            json={"expected_version": a["version"], "to": "testing"},
        )
        b = client.post(
            "/api/cycle/units/intake-external", json={"part_id": part.id, "qty": 1}
        ).json()["items"][0]
        client.post(
            f"/api/cycle/units/{b['id']}/self-pass",
            json={"expected_version": b["version"]},
        )

    with _cycle_client(db, "cycle-wip-reader", page_cycle_wip=True) as reader:
        wip = reader.get("/api/cycle/wip", params={"part_ids": str(part.id)})
        assert wip.status_code == 200, wip.text
        item = wip.json()["items"][0]
        assert item["testing"] == 1
        assert item["ready"] == 1
        assert item["repairing"] == 0


def test_version_conflict_on_close(db):
    part = _part(db, "CYCLE-PN-F")
    with _cycle_client(db, "cycle-cas", page_cycle_recycle=True) as client:
        unit = client.post(
            "/api/cycle/units/intake-external", json={"part_id": part.id, "qty": 1}
        ).json()["items"][0]
        first = client.post(
            f"/api/cycle/units/{unit['id']}/self-pass",
            json={"expected_version": unit["version"]},
        )
        assert first.status_code == 200
        stale = client.post(
            f"/api/cycle/units/{unit['id']}/close",
            json={
                "expected_version": unit["version"],
                "outcome": "scrap",
            },
        )
        assert stale.status_code == 409
