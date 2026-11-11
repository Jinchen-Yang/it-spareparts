"""坏件循环待修池 API（D-0058 契约第 4 节）。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, text

from app import permissions
from app.auth import _make_token
from app.config import Settings
from app.main import app
from app.models.inventory import Inventory
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_cycle import MaintenanceCycleSellIntent
from app.models.maintenance_repair import (
    MaintenanceRepairCommand,
    MaintenanceRepairRecord,
    MaintenanceRepairRecordSource,
)
from fastapi.testclient import TestClient
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt

FORBIDDEN = {
    "receipt_id", "project_id", "delivery_id", "deliveryId", "price", "cost",
    "unit_price", "token", "vendor_name", "serial_number", "customer",
    "sources", "sell_intent", "note", "supplier",
}
CREATE_KEYS = {
    "record_id", "record_no", "part_id", "pn", "description", "fixed_qty",
    "scrap_qty", "status", "version", "created_at", "operator",
}
LIST_KEYS = CREATE_KEYS | {"voided_at", "void_reason"}
LEDGER_TABLES = (
    "inventory",
    "maintenance_front_stock",
    "maintenance_bad_salvage",
    "f_purchase_order",
    "f_sales_order",
)


def _client(db, username: str, **overrides):
    return client_for(db, username=username, role="readonly", overrides=overrides or {})


def _repairer(db, username: str):
    return _client(db, username, page_cycle_repair=True)


def _manager(db, username: str):
    return _client(db, username, page_cycle_manage=True)


def _buyer(db, username: str):
    return _client(db, username, page_cycle_wip=True)


def _key() -> str:
    return uuid4().hex


def _submit(client, part_id: int, fixed: int, scrap: int, key: str | None = None):
    return client.post(
        "/api/cycle/repair-records",
        json={"part_id": part_id, "fixed_qty": fixed, "scrap_qty": scrap},
        headers={"Idempotency-Key": key or _key()},
    )


def _void(client, record_id: str, version: int, reason: str = "填反了", checked: bool = False, key: str | None = None):
    return client.post(
        f"/api/cycle/repair-records/{record_id}/void",
        json={
            "expected_version": version,
            "reason": reason,
            "shop_stock_manually_reduced": checked,
        },
        headers={"Idempotency-Key": key or _key()},
    )


def _count(db, model) -> int:
    db.expire_all()
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _ledger(db) -> dict[str, int]:
    db.expire_all()
    return {
        name: int(db.scalar(text(f"SELECT count(*) FROM {name}")) or 0)
        for name in LEDGER_TABLES
    }


def _walk_keys(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_keys(item)


def _assert_repairer_payload(payload: dict, *, keys: set[str]):
    assert set(payload) == keys
    assert not (set(_walk_keys(payload)) & FORBIDDEN)


def test_page_keys_stay_closed_including_admin():
    for role in ("admin", "boss", "readonly", "sales", "purchaser"):
        graph = permissions.effective(role, None)
        for key in permissions.CYCLE_PAGE_KEYS:
            assert graph[key] is False


def test_shared_password_and_recycle_key_are_forbidden(db):
    perms = permissions.runtime_safe(permissions.effective("admin", None))
    perms["page_cycle_repair"] = True
    token, _exp = _make_token("admin", "admin", "共享", perms=perms, authn="shared", fallback=True)
    shared = TestClient(app)
    shared.headers["Authorization"] = f"Bearer {token}"
    denied = shared.get("/api/cycle/pool")
    assert denied.status_code == 403
    assert denied.json()["detail"] == "坏件循环操作需要实名系统账号"

    with _client(db, "cycle-recycle-only", page_cycle_recycle=True) as recycle, \
            _client(db, "cycle-test-only", page_cycle_test=True) as tester:
        for client in (recycle, tester):
            response = client.get("/api/cycle/pool")
            assert response.status_code == 403
            assert response.json()["detail"] == "无权访问坏件循环页面"


def test_bad_return_appears_by_pn_and_other_conditions_do_not(db):
    project = _project(db, "POOL")
    part = _part(db, "POOL-PN-1")
    part.description = "固态硬盘"
    other = _part(db, "POOL-PN-GOOD")
    _receipt(db, project, part, "4", condition="坏品")
    _receipt(db, project, part, "2", condition="成品")
    _receipt(db, project, part, "9", condition="废品")
    blank = _receipt(db, project, part, "3", condition=None)
    fractional = _receipt(db, project, other, "1.500", condition="坏品")
    fractional.qty = fractional.qty  # 非整数不进待修
    db.commit()
    before = _ledger(db)
    with _repairer(db, "pool-repairer") as repairer, _buyer(db, "pool-buyer") as buyer:
        assert buyer.get("/api/cycle/pool").status_code == 403
        listed = repairer.get("/api/cycle/pool")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item == {
            "part_id": part.id,
            "pn": "POOL-PN-1",
            "description": "固态硬盘",
            "pending_qty": 4,
        }
        assert other.id not in {row["part_id"] for row in body["items"]}
        blank.test_result = "坏件"
        db.commit()
        again = repairer.get("/api/cycle/pool").json()["items"][0]
        assert again["pending_qty"] == 7
    assert _ledger(db) == before
    assert _count(db, Inventory) == before["inventory"]
    assert _count(db, MaintenanceBadSalvage) == before["maintenance_bad_salvage"]


def test_submit_moves_three_counts_and_leaves_the_rest(db):
    project = _project(db, "SUB")
    part = _part(db, "SUB-PN")
    part.description = "内存"
    _receipt(db, project, part, "5", condition="坏品")
    db.commit()
    before = _ledger(db)
    with _repairer(db, "sub-repairer") as repairer, _buyer(db, "sub-buyer") as buyer, \
            _manager(db, "sub-manager") as manager:
        assert buyer.post(
            "/api/cycle/repair-records",
            json={"part_id": part.id, "fixed_qty": 1, "scrap_qty": 0},
            headers={"Idempotency-Key": _key()},
        ).status_code == 403
        created = _submit(repairer, part.id, 2, 1)
        assert created.status_code == 201, created.text
        _assert_repairer_payload(created.json(), keys=CREATE_KEYS)
        assert created.json()["fixed_qty"] == 2
        assert created.json()["scrap_qty"] == 1
        assert created.json()["operator"] == "sub-repairer"
        assert created.json()["record_no"].startswith("WX")
        assert created.json()["status"] == "active"
        pool = repairer.get("/api/cycle/pool").json()["items"][0]
        assert pool["pending_qty"] == 2
        assert repairer.get("/api/cycle/summary").status_code == 403
        summary = buyer.get("/api/cycle/summary").json()["items"]
        row = next(item for item in summary if item["part_id"] == part.id)
        assert row["pending_qty"] == 2
        assert row["shop_qty"] == 2
        assert row["scrap_qty"] == 1
        assert set(row) == {
            "part_id", "pn", "description", "pending_qty", "shop_qty", "scrap_qty",
        }
        intent = db.scalar(select(MaintenanceCycleSellIntent))
        assert intent is not None
        assert intent.state == "pending"
        assert intent.qty == 2
        assert intent.condition_bucket == "good"
        assert intent.wire_unit_id == db.scalar(select(MaintenanceRepairRecord.wire_unit_id))
        assert Settings(_env_file=None).shop_cycle_sell_enabled is False
        detail = manager.get(f"/api/cycle/repair-records/{created.json()['record_id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["sources"]
        assert detail.json()["sell_intent"]["unit_id"] == intent.wire_unit_id
        assert "recordId" not in detail.json()["sell_intent"]
        own = repairer.get(f"/api/cycle/repair-records/{created.json()['record_id']}")
        _assert_repairer_payload(own.json(), keys=LIST_KEYS)
        assert repairer.get(
            f"/api/cycle/repair-records/{created.json()['record_id']}/sell-intent"
        ).status_code == 403
    assert _ledger(db) == before


def test_repairer_cannot_see_another_operators_record(db):
    project = _project(db, "HID")
    part = _part(db, "HID-PN")
    _receipt(db, project, part, "2", condition="故障")
    db.commit()
    with _repairer(db, "hid-a") as first, _repairer(db, "hid-b") as second, \
            _manager(db, "hid-mgr") as manager:
        created = _submit(first, part.id, 1, 0)
        assert created.status_code == 201, created.text
        record_id = created.json()["record_id"]
        hidden = second.get(f"/api/cycle/repair-records/{record_id}")
        assert hidden.status_code == 404
        assert hidden.json()["detail"] == "记录不存在"
        listed = second.get("/api/cycle/repair-records").json()
        assert listed["total"] == 0
        assert manager.get(f"/api/cycle/repair-records/{record_id}").status_code == 200
        assert manager.get(f"/api/cycle/repair-records/{record_id}").json()["sources"][0]["project_id"]


def test_request_three_when_two_remain_is_rejected_unchanged(db):
    project = _project(db, "SHORT")
    part = _part(db, "SHORT-PN")
    _receipt(db, project, part, "2", condition="坏品")
    db.commit()
    with _repairer(db, "short-repairer") as repairer:
        rejected = _submit(repairer, part.id, 3, 0)
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "修好数与废品数之和超过当前待修件数"
        assert _count(db, MaintenanceRepairRecord) == 0
        assert repairer.get("/api/cycle/pool").json()["items"][0]["pending_qty"] == 2
        zero = repairer.post(
            "/api/cycle/repair-records",
            json={"part_id": part.id, "fixed_qty": 0, "scrap_qty": 0},
            headers={"Idempotency-Key": _key()},
        )
        assert zero.status_code == 422


def test_same_day_void_restores_pool_and_next_day_needs_supervisor(db):
    project = _project(db, "VOID")
    part = _part(db, "VOID-PN")
    _receipt(db, project, part, "3", condition="坏品")
    db.commit()
    with _repairer(db, "void-repairer") as repairer, _manager(db, "void-mgr") as manager:
        created = _submit(repairer, part.id, 2, 1)
        assert created.status_code == 201, created.text
        body = created.json()
        voided = _void(repairer, body["record_id"], body["version"])
        assert voided.status_code == 200, voided.text
        _assert_repairer_payload(voided.json(), keys=LIST_KEYS)
        assert voided.json()["status"] == "voided"
        assert repairer.get("/api/cycle/pool").json()["items"][0]["pending_qty"] == 3
        summary = manager.get("/api/cycle/summary", params={"part_ids": str(part.id)}).json()
        assert summary["items"][0]["shop_qty"] == 0
        assert summary["items"][0]["scrap_qty"] == 0
        again = _submit(repairer, part.id, 1, 0)
        assert again.status_code == 201, again.text
        record = db.get(MaintenanceRepairRecord, again.json()["record_id"])
        record.created_at = datetime.now(UTC) - timedelta(days=2)
        db.commit()
        late = _void(repairer, again.json()["record_id"], again.json()["version"])
        assert late.status_code == 403
        assert late.json()["detail"] == "已超过当天，只有主管可以作废"
        supervised = _void(manager, again.json()["record_id"], again.json()["version"], reason="过期作废")
        assert supervised.status_code == 200, supervised.text
        assert supervised.json()["sources"]


def test_idempotency_replay_conflict_and_distinct_void_targets(db):
    project = _project(db, "IDEM")
    part = _part(db, "IDEM-PN")
    _receipt(db, project, part, "6", condition="坏品")
    db.commit()
    with _repairer(db, "idem-repairer") as repairer:
        key = _key()
        first = _submit(repairer, part.id, 1, 1, key)
        assert first.status_code == 201, first.text
        replay = _submit(repairer, part.id, 1, 1, key)
        assert replay.status_code == 201
        assert replay.headers.get("Idempotent-Replayed") == "true"
        assert replay.json()["record_id"] == first.json()["record_id"]
        assert _count(db, MaintenanceRepairRecord) == 1
        assert int(db.scalar(select(func.sum(MaintenanceRepairRecordSource.qty))) or 0) == 2
        conflict = _submit(repairer, part.id, 1, 0, key)
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "幂等键已被用于不同的请求"
        missing = repairer.post(
            "/api/cycle/repair-records",
            json={"part_id": part.id, "fixed_qty": 1, "scrap_qty": 0},
        )
        assert missing.status_code == 400
        second = _submit(repairer, part.id, 1, 0)
        assert second.status_code == 201, second.text
        shared = _key()
        void_key_body = {
            "expected_version": 1,
            "reason": "填反了",
            "shop_stock_manually_reduced": False,
        }
        first_void = repairer.post(
            f"/api/cycle/repair-records/{first.json()['record_id']}/void",
            json=void_key_body,
            headers={"Idempotency-Key": shared},
        )
        assert first_void.status_code == 200, first_void.text
        second_void = repairer.post(
            f"/api/cycle/repair-records/{second.json()['record_id']}/void",
            json=void_key_body,
            headers={"Idempotency-Key": shared},
        )
        assert second_void.status_code == 409
        stored = db.get(MaintenanceRepairRecord, second.json()["record_id"])
        assert stored.status == "active"
        assert _count(db, MaintenanceRepairCommand) >= 2


def test_parallel_same_key_inserts_one_record(db):
    project = _project(db, "RACEKEY")
    part = _part(db, "RACEKEY-PN")
    _receipt(db, project, part, "4", condition="坏品")
    db.commit()
    first = _repairer(db, "racekey-same")
    second = TestClient(app)
    second.headers["Authorization"] = first.headers["Authorization"]
    key = _key()
    part_id = part.id

    def attempt(client):
        return _submit(client, part_id, 1, 0, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [first, second]))
    assert sorted(response.status_code for response in results) == [201, 201]
    assert any(response.headers.get("Idempotent-Replayed") == "true" for response in results)
    assert results[0].json()["record_id"] == results[1].json()["record_id"]
    assert _count(db, MaintenanceRepairRecord) == 1


def test_summary_part_ids_keep_request_order_and_zeros(db):
    project = _project(db, "SUM")
    first = _part(db, "SUM-B")
    second = _part(db, "SUM-A")
    _receipt(db, project, first, "2", condition="坏品")
    db.commit()
    with _repairer(db, "sum-repairer") as repairer, _buyer(db, "sum-buyer") as buyer:
        assert _submit(repairer, first.id, 1, 0).status_code == 201
        ordered = buyer.get(
            "/api/cycle/summary",
            params={"part_ids": f"{second.id},{first.id}"},
        )
        assert ordered.status_code == 200, ordered.text
        items = ordered.json()["items"]
        assert [item["part_id"] for item in items] == [second.id, first.id]
        assert items[0]["pending_qty"] == 0
        assert items[1]["shop_qty"] == 1
        assert items[0]["scrap_qty"] == 0
        invalid = buyer.get("/api/cycle/summary", params={"part_ids": "abc"})
        assert invalid.status_code == 422
        assert invalid.json()["detail"] == "part_ids 必须为 1–100 个正整数"


def test_submit_when_pending_is_already_zero_is_400(db):
    project = _project(db, "ZERO")
    part = _part(db, "ZERO-PN")
    _receipt(db, project, part, "1", condition="坏品")
    db.commit()
    with _repairer(db, "zero-repairer") as repairer:
        assert _submit(repairer, part.id, 1, 0).status_code == 201
        again = _submit(repairer, part.id, 1, 0)
        assert again.status_code == 400
        assert again.json()["detail"] == "该型号当前没有待修件数"


def test_commit_time_deduction_trigger_returns_contract_409(db, monkeypatch):
    project = _project(db, "TRIG")
    part = _part(db, "TRIG-PN")
    _receipt(db, project, part, "1", condition="坏品")
    db.commit()

    def over_allocate(lines, deducted, qty):
        return [(lines[0].rkd_line_id, 5)]

    monkeypatch.setattr("app.services.maintenance_repair.allocate_fifo", over_allocate)
    with _repairer(db, "trig-repairer") as repairer:
        response = _submit(repairer, part.id, 1, 0)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "修好数与废品数之和超过当前待修件数"}
    assert _count(db, MaintenanceRepairRecord) == 0


def test_lock_parts_restores_the_callers_lock_timeout(db):
    from app.services.maintenance_repair import lock_parts

    before = db.scalar(text("SELECT current_setting('lock_timeout')"))
    lock_parts(db, [1])
    assert db.scalar(text("SELECT current_setting('lock_timeout')")) == before
    db.rollback()
