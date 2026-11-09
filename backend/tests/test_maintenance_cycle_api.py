"""坏件循环池 T2 契约：入池、状态机转移、字段裁剪、意图同事务、mark-sold 兜底。

对应任务书 §6.1–6.3 / §11 T2 / §12 R1–R6 与 PM 验收清单 A-01..A-28、P-01..P-12。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import permissions
from app.models.inventory import Inventory
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_cycle import (
    MaintenanceCycleEvent,
    MaintenanceCycleSellIntent,
    MaintenanceCycleUnit,
)
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt

# ---------------------------------------------------------------------------
# 共享装置
# ---------------------------------------------------------------------------


def _client(db, username: str, **overrides):
    return client_for(db, username=username, role="readonly", overrides=overrides or {})


def _recycle(db, username: str):
    return _client(db, username, page_cycle_recycle=True)


def _manager(db, username: str):
    return _client(db, username, page_cycle_manage=True)


def _tester(db, username: str):
    return _client(db, username, page_cycle_test=True)


def _repairer(db, username: str):
    return _client(db, username, page_cycle_repair=True)


def _intake(client, part, qty: int = 1, **extra):
    payload = {"part_id": part.id, "qty": qty, **extra}
    return client.post("/api/cycle/units/intake-external", json=payload)


def _unit(db, unit_id: int) -> MaintenanceCycleUnit:
    db.expire_all()
    unit = db.get(MaintenanceCycleUnit, unit_id)
    assert unit is not None
    return unit


def _intents(db, unit_id: int) -> list[MaintenanceCycleSellIntent]:
    db.expire_all()
    return list(
        db.scalars(
            select(MaintenanceCycleSellIntent).where(
                MaintenanceCycleSellIntent.unit_id == unit_id
            )
        ).all()
    )


def _count(db, model) -> int:
    db.expire_all()
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _dispatch(client, unit: dict, to: str, **extra):
    body = {"expected_version": unit["version"], "to": to, **extra}
    return client.post(f"/api/cycle/units/{unit['id']}/dispatch", json=body)


def _transition(client, unit: dict, action: str, **extra):
    body = {"expected_version": unit["version"], **extra}
    return client.post(f"/api/cycle/units/{unit['id']}/{action}", json=body)


# ---------------------------------------------------------------------------
# P-01：五页键默认 false（含 admin），未授权 403
# ---------------------------------------------------------------------------


def test_cycle_page_keys_default_false_for_every_builtin_role_including_admin():
    for role in ("admin", "boss", "readonly", "sales", "purchaser"):
        graph = permissions.effective(role, None)
        for key in permissions.CYCLE_PAGE_KEYS:
            assert graph[key] is False, f"{role}.{key} 必须默认 false"


def test_admin_without_explicit_grant_is_forbidden(db):
    client = client_for(db, username="plain-admin", role="admin")
    assert client.get("/api/cycle/units").status_code == 403
    assert client.get("/api/cycle/wip", params={"part_ids": "1"}).status_code == 403


# ---------------------------------------------------------------------------
# 入池
# ---------------------------------------------------------------------------


def test_intake_external_creates_pool_units_and_writes_no_inventory_or_salvage(db):
    part = _part(db, "CYCLE-PN-A")
    db.commit()
    inventory_before = _count(db, Inventory)
    salvage_before = _count(db, MaintenanceBadSalvage)
    with _recycle(db, "cycle-recycle-a") as recycle:
        created = _intake(recycle, part, 2, intake_condition="成品", note="外部回收")
        assert created.status_code == 201, created.text
        items = created.json()["items"]
        assert len(items) == 2
        for item in items:
            assert item["station"] == "pool"
            assert item["outcome"] is None
            assert item["intake_condition"] == "成品"
            assert item["version"] == 1
            # 非主管看不到主管字段（P-05/P-12）；source_kind 属主管字段
            for hidden in ("vendor_name", "source_receipt_id", "serial_number", "note",
                           "source_kind"):
                assert hidden not in item
        # 库里来源仍是 external
        assert _unit(db, items[0]["id"]).source_kind == "external"
    assert _count(db, Inventory) == inventory_before
    assert _count(db, MaintenanceBadSalvage) == salvage_before


def test_intake_external_resolves_pn_and_rejects_invalid_identity(db):
    part = _part(db, "CYCLE-PN-PN-1")
    db.commit()
    with _recycle(db, "cycle-recycle-pn") as recycle:
        by_pn = recycle.post(
            "/api/cycle/units/intake-external", json={"pn": "CYCLE-PN-PN-1", "qty": 1}
        )
        assert by_pn.status_code == 201, by_pn.text
        missing = recycle.post(
            "/api/cycle/units/intake-external", json={"part_id": 99999999, "qty": 1}
        )
        assert missing.status_code == 400, missing.text
        no_identity = recycle.post(
            "/api/cycle/units/intake-external", json={"qty": 1}
        )
        assert no_identity.status_code == 400, no_identity.text
        bad_qty = recycle.post(
            "/api/cycle/units/intake-external", json={"part_id": part.id, "qty": 0}
        )
        assert bad_qty.status_code == 400, bad_qty.text


def test_handoff_from_receipt_copies_condition_and_stays_pool(db):
    project = _project(db, "CYC")
    part = _part(db, "CYCLE-PN-B")
    receipt = _receipt(db, project, part, "2", condition="成品")
    db.commit()
    with _recycle(db, "cycle-recycle-b") as recycle:
        created = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 2,
                "expected_receipt_version": receipt.version,
            },
        )
        assert created.status_code == 201, created.text
        items = created.json()["items"]
        assert len(items) == 2
        assert all(item["station"] == "pool" for item in items)
        assert all(item["outcome"] is None for item in items)
        assert all("source_kind" not in item for item in items)
        assert all(
            _unit(db, item["id"]).source_kind == "receipt" for item in items
        )
        assert all(item["intake_condition"] == "成品" for item in items)
        # 成品不自动去电商（A-04/A-26）
        assert all(item["condition_bucket"] is None for item in items)
        assert _intents(db, items[0]["id"]) == []


def test_handoff_quantity_cannot_exceed_receipt_and_counts_closed_units(db):
    project = _project(db, "CYC")
    part = _part(db, "CYCLE-PN-C")
    receipt = _receipt(db, project, part, "2", condition="坏品")
    db.commit()
    with _recycle(db, "cycle-recycle-c") as recycle, _manager(db, "cycle-mgr-c") as manager:
        first = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 2,
                "expected_receipt_version": receipt.version,
            },
        )
        assert first.status_code == 201, first.text
        over = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
            },
        )
        assert over.status_code == 409, over.text
        # 报废/去电商仍占用收货件数（A-07）：关闭一件后也不能再入池。
        unit = first.json()["items"][0]
        closed = manager.post(
            f"/api/cycle/units/{unit['id']}/close",
            json={"expected_version": unit["version"], "outcome": "scrap"},
        )
        assert closed.status_code == 200, closed.text
        still_over = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
            },
        )
        assert still_over.status_code == 409, still_over.text


def test_handoff_serial_numbers_must_match_qty_and_be_unique(db):
    project = _project(db, "CYC")
    part = _part(db, "CYCLE-PN-SN")
    receipt = _receipt(db, project, part, "3", condition="坏品")
    db.commit()
    with _recycle(db, "cycle-recycle-sn") as recycle:
        mismatch = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 2,
                "expected_receipt_version": receipt.version,
                "serial_numbers": ["SN-ONLY-1"],
            },
        )
        assert mismatch.status_code == 400, mismatch.text
        duplicate = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 2,
                "expected_receipt_version": receipt.version,
                "serial_numbers": ["SN-DUP", "SN-DUP"],
            },
        )
        assert duplicate.status_code == 400, duplicate.text
        ok = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 2,
                "expected_receipt_version": receipt.version,
                "serial_numbers": ["SN-1", "SN-2"],
            },
        )
        assert ok.status_code == 201, ok.text
        reused = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
                "serial_numbers": ["SN-1"],
            },
        )
        assert reused.status_code == 409, reused.text


def test_handoff_concurrent_intake_only_one_succeeds(db):
    project = _project(db, "CYC")
    part = _part(db, "CYCLE-PN-RACE")
    receipt = _receipt(db, project, part, "2", condition="坏品")
    db.commit()
    clients = [_recycle(db, "cycle-race-1"), _recycle(db, "cycle-race-2")]
    payload = {
        "receipt_id": receipt.rkd_line_id,
        "qty": 2,
        "expected_receipt_version": receipt.version,
    }

    def attempt(client):
        return client.post("/api/cycle/units/handoff-from-receipt", json=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, clients))
    assert sorted(response.status_code for response in results) == [201, 409], [
        (response.status_code, response.text) for response in results
    ]
    db.expire_all()
    assert _count(db, MaintenanceCycleUnit) == 2


# ---------------------------------------------------------------------------
# 工位转移与版本 CAS
# ---------------------------------------------------------------------------


def test_dispatch_self_pass_and_version_conflict_zero_write(db):
    part = _part(db, "CYCLE-PN-D")
    db.commit()
    with _recycle(db, "cycle-recycle-d") as recycle:
        unit = _intake(recycle, part, 1).json()["items"][0]
        stale = recycle.post(
            f"/api/cycle/units/{unit['id']}/dispatch",
            json={"expected_version": 99, "to": "testing"},
        )
        assert stale.status_code == 409, stale.text
        assert _unit(db, unit["id"]).station == "pool"
        assert _unit(db, unit["id"]).version == unit["version"]

        dispatched = _dispatch(recycle, unit, "testing")
        assert dispatched.status_code == 200, dispatched.text
        body = dispatched.json()
        assert body["station"] == "testing"
        assert body["version"] == unit["version"] + 1

        invalid_from = recycle.post(
            f"/api/cycle/units/{unit['id']}/self-pass",
            json={"expected_version": body["version"]},
        )
        assert invalid_from.status_code == 400, invalid_from.text

        # 委外必须带供应商名称（A-08）
        outsource_unit = _intake(recycle, part, 1).json()["items"][0]
        missing_vendor = _dispatch(recycle, outsource_unit, "outsource")
        assert missing_vendor.status_code == 400, missing_vendor.text
        with_vendor = _dispatch(recycle, outsource_unit, "outsource", vendor_name="维修点A")
        assert with_vendor.status_code == 200, with_vendor.text
        # 非主管响应不回显供应商（P-05）
        assert "vendor_name" not in with_vendor.json()


def test_self_pass_moves_pool_to_ready_good_but_not_withdrawn(db):
    part = _part(db, "CYCLE-PN-E")
    db.commit()
    with _recycle(db, "cycle-recycle-e") as recycle:
        unit = _intake(recycle, part, 1).json()["items"][0]
        ready = _transition(recycle, unit, "self-pass")
        assert ready.status_code == 200, ready.text
        assert ready.json()["station"] == "ready"
        assert ready.json()["condition_bucket"] == "good"


def test_complete_test_good_clears_assignee_and_requires_ready(db):
    part = _part(db, "CYCLE-PN-F")
    db.commit()
    with _recycle(db, "cycle-recycle-f") as recycle, _tester(db, "cycle-tester-f") as tester:
        unit = _intake(recycle, part, 1).json()["items"][0]
        dispatched = _dispatch(recycle, unit, "testing", assignee_username="cycle-tester-f")
        assert dispatched.status_code == 200, dispatched.text
        assigned = dispatched.json()
        assert assigned["assignee_username"] == "cycle-tester-f"

        wrong = _transition(tester, assigned, "complete-test", result="good", next="scrap")
        assert wrong.status_code == 400, wrong.text
        done = _transition(tester, assigned, "complete-test", result="good")
        assert done.status_code == 200, done.text
        body = done.json()
        assert body["station"] == "ready"
        assert body["condition_bucket"] == "good"
        assert body["assignee_username"] is None


def test_complete_test_bad_repairing_clears_assignee_and_scrap_closes(db):
    part = _part(db, "CYCLE-PN-G")
    db.commit()
    with _recycle(db, "cycle-recycle-g") as recycle, _tester(db, "cycle-tester-g") as tester:
        unit = _intake(recycle, part, 1).json()["items"][0]
        testing = _dispatch(recycle, unit, "testing", assignee_username="cycle-tester-g").json()
        missing_next = _transition(tester, testing, "complete-test", result="bad")
        assert missing_next.status_code == 400, missing_next.text
        repairing = _transition(
            tester, testing, "complete-test", result="bad", next="repairing"
        )
        assert repairing.status_code == 200, repairing.text
        body = repairing.json()
        assert body["station"] == "repairing"
        assert body["assignee_username"] is None  # R6：检测人不变维修负责人

        scrap_unit = _intake(recycle, part, 1).json()["items"][0]
        testing2 = _dispatch(recycle, scrap_unit, "testing").json()
        scrap = _transition(tester, testing2, "complete-test", result="bad", next="scrap")
        assert scrap.status_code == 200, scrap.text
        assert scrap.json()["station"] == "closed"
        assert scrap.json()["outcome"] == "scrap"


def test_complete_repair_fixed_both_paths_and_unfixed_only_scrap(db):
    part = _part(db, "CYCLE-PN-H")
    db.commit()
    with _recycle(db, "cycle-recycle-h") as recycle, _repairer(db, "cycle-repairer-h") as repairer:
        def repairing_unit():
            unit = _intake(recycle, part, 1).json()["items"][0]
            return _dispatch(recycle, unit, "repairing").json()

        retest = repairing_unit()
        missing = _transition(repairer, retest, "complete-repair", result="fixed")
        assert missing.status_code == 400, missing.text
        to_testing = _transition(
            repairer, retest, "complete-repair", result="fixed", next="testing"
        )
        assert to_testing.status_code == 200, to_testing.text
        assert to_testing.json()["station"] == "testing"

        no_retest = repairing_unit()
        to_ready = _transition(
            repairer, no_retest, "complete-repair", result="fixed", next="ready"
        )
        assert to_ready.status_code == 200, to_ready.text
        assert to_ready.json()["station"] == "ready"
        assert to_ready.json()["condition_bucket"] == "good"

        unfixable = repairing_unit()
        illegal = _transition(
            repairer, unfixable, "complete-repair", result="unfixed", next="testing"
        )
        assert illegal.status_code == 400, illegal.text
        scrapped = _transition(repairer, unfixable, "complete-repair", result="unfixed")
        assert scrapped.status_code == 200, scrapped.text
        assert scrapped.json()["outcome"] == "scrap"


def test_outsource_return_five_destinations_and_required_bucket(db):
    part = _part(db, "CYCLE-PN-I")
    db.commit()
    with _recycle(db, "cycle-recycle-i") as recycle:
        def outsource_unit():
            unit = _intake(recycle, part, 1).json()["items"][0]
            returned = _dispatch(recycle, unit, "outsource", vendor_name="委外点I")
            assert returned.status_code == 200, returned.text
            return returned.json()

        for destination in ("testing", "repairing"):
            unit = outsource_unit()
            result = _transition(recycle, unit, "outsource-return", next=destination)
            assert result.status_code == 200, result.text
            assert result.json()["station"] == destination
            # 供应商是主管字段：工人响应不回显，但库里保留审计值
            assert "vendor_name" not in result.json()
            assert _unit(db, unit["id"]).vendor_name == "委外点I"

        unit = outsource_unit()
        missing_bucket = _transition(recycle, unit, "outsource-return", next="to_shop")
        assert missing_bucket.status_code == 400, missing_bucket.text
        to_shop = _transition(
            recycle, unit, "outsource-return", next="to_shop", condition_bucket="bad"
        )
        assert to_shop.status_code == 200, to_shop.text
        assert to_shop.json()["outcome"] == "to_shop"
        intents = _intents(db, unit["id"])
        assert len(intents) == 1 and intents[0].state == "pending"

        for destination in ("to_company", "scrap"):
            unit = outsource_unit()
            result = _transition(recycle, unit, "outsource-return", next=destination)
            assert result.status_code == 200, result.text
            assert result.json()["outcome"] == destination
            assert _intents(db, unit["id"]) == []


def test_close_to_shop_creates_exactly_one_pending_intent_without_extra_fields(db):
    part = _part(db, "CYCLE-PN-J")
    db.commit()
    with _recycle(db, "cycle-recycle-j") as recycle:
        unit = _intake(recycle, part, 1).json()["items"][0]
        missing_bucket = _transition(recycle, unit, "close", outcome="to_shop")
        assert missing_bucket.status_code == 400, missing_bucket.text
        assert _intents(db, unit["id"]) == []
        closed = _transition(recycle, unit, "close", outcome="to_shop", condition_bucket="good")
        assert closed.status_code == 200, closed.text
        intents = _intents(db, unit["id"])
        assert len(intents) == 1
        intent = intents[0]
        assert intent.state == "pending"
        assert intent.part_id == part.id
        assert intent.qty == Decimal(1)
        assert intent.condition_bucket == "good"
        assert str(intent.delivery_id)
        # 意图行不含项目/SN/成本/价格/供应商等字段（列级白名单，A-22）
        forbidden = {"project_id", "serial_number", "vendor_name", "note", "cost", "price"}
        assert forbidden.isdisjoint(set(intent.__table__.columns.keys()))


def test_mark_sold_requires_manager_and_is_idempotent(db):
    part = _part(db, "CYCLE-PN-K")
    db.commit()
    with _recycle(db, "cycle-recycle-k") as recycle, _manager(db, "cycle-mgr-k") as manager:
        unit = _intake(recycle, part, 1).json()["items"][0]
        closed = _transition(recycle, unit, "close", outcome="to_shop", condition_bucket="good")
        assert closed.status_code == 200, closed.text
        sold_body = closed.json()

        denied = _transition(recycle, sold_body, "mark-sold")
        assert denied.status_code == 403, denied.text

        sold = _transition(manager, sold_body, "mark-sold")
        assert sold.status_code == 200, sold.text
        assert sold.json()["outcome"] == "sold"
        assert sold.json()["station"] == "closed"

        # 重复调用幂等：旧版本号仍返回当前状态（不重复改版本）
        again = _transition(manager, sold_body, "mark-sold")
        assert again.status_code == 200, again.text
        assert again.json()["outcome"] == "sold"
        assert _unit(db, unit["id"]).version == sold.json()["version"]


def test_mark_sold_rejects_non_shop_outcome(db):
    part = _part(db, "CYCLE-PN-L")
    db.commit()
    with _recycle(db, "cycle-recycle-l") as recycle, _manager(db, "cycle-mgr-l") as manager:
        unit = _intake(recycle, part, 1).json()["items"][0]
        closed = _transition(recycle, unit, "close", outcome="scrap")
        assert closed.status_code == 200, closed.text
        conflict = _transition(manager, closed.json(), "mark-sold")
        assert conflict.status_code == 409, conflict.text


def test_withdraw_only_pool_hides_from_list_and_releases_consumed(db):
    project = _project(db, "CYC")
    part = _part(db, "CYCLE-PN-M")
    receipt = _receipt(db, project, part, "2", condition="坏品")
    db.commit()
    with _recycle(db, "cycle-recycle-m") as recycle, _manager(db, "cycle-mgr-m") as manager:
        items = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 2,
                "expected_receipt_version": receipt.version,
            },
        ).json()["items"]
        withdrawn = _transition(recycle, items[0], "withdraw")
        assert withdrawn.status_code == 200, withdrawn.text
        again = _transition(recycle, withdrawn.json(), "withdraw")
        assert again.status_code == 409, again.text

        listing = recycle.get("/api/cycle/units", params={"station": "pool"})
        assert listing.status_code == 200, listing.text
        ids = {row["id"] for row in listing.json()["items"]}
        assert items[0]["id"] not in ids
        assert items[1]["id"] in ids
        # 撤回国仍保持在 pool 工位（DB 约束），但工人不可见（404）
        assert _unit(db, items[0]["id"]).station == "pool"
        assert _unit(db, items[0]["id"]).withdrawn_at is not None
        hidden = recycle.get(f"/api/cycle/units/{items[0]['id']}")
        assert hidden.status_code == 404, hidden.text
        manager_view = manager.get(f"/api/cycle/units/{items[0]['id']}")
        assert manager_view.status_code == 200, manager_view.text
        assert manager_view.json()["withdrawn_at"] is not None
        # 撤回件数释放后，同收货行可以重新入池 1 件
        redo = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
            },
        )
        assert redo.status_code == 201, redo.text


# ---------------------------------------------------------------------------
# 字段裁剪 / 工位交集 / 事件白名单
# ---------------------------------------------------------------------------


def test_station_intersection_and_invisible_detail_404(db):
    project = _project(db, "CYC")
    part = _part(db, "CYCLE-PN-N")
    receipt = _receipt(db, project, part, "1", condition="坏品")
    db.commit()
    with _recycle(db, "cycle-recycle-n") as recycle, _tester(db, "cycle-tester-n") as tester:
        unit = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
            },
        ).json()["items"][0]
        _dispatch(recycle, unit, "outsource", vendor_name="委外点N")

        # R4：检测岗请求 outsource,closed → 交集为空，返回空列表
        listing = tester.get("/api/cycle/units", params={"station": "outsource,closed"})
        assert listing.status_code == 200, listing.text
        assert listing.json()["items"] == []
        assert listing.json()["total"] == 0
        # A-20：存在但不可见 → 404
        detail = tester.get(f"/api/cycle/units/{unit['id']}")
        assert detail.status_code == 404, detail.text

        unknown = tester.get("/api/cycle/units", params={"station": "bogus"})
        assert unknown.status_code == 400, unknown.text


def test_worker_serialization_and_event_payload_whitelist(db):
    project = _project(db, "CYC")
    part = _part(db, "CYCLE-PN-O")
    receipt = _receipt(db, project, part, "1", condition="坏品")
    db.commit()
    with _recycle(db, "cycle-recycle-o") as recycle, _tester(db, "cycle-tester-o") as tester, \
            _manager(db, "cycle-mgr-o") as manager:
        unit = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
                "serial_numbers": ["SN-O-1"],
            },
        ).json()["items"][0]
        testing = _dispatch(recycle, unit, "testing").json()

        worker_detail = tester.get(f"/api/cycle/units/{testing['id']}")
        assert worker_detail.status_code == 200, worker_detail.text
        body = worker_detail.json()
        for hidden in ("vendor_name", "source_receipt_id", "serial_number", "note",
                       "created_by", "updated_by", "withdrawn_at"):
            assert hidden not in body
        for name in ("id", "unit_uid", "part_id", "pn", "station", "outcome",
                     "condition_bucket", "intake_condition", "repair_round",
                     "assignee_username", "version", "created_at", "updated_at", "closed_at"):
            assert name in body

        worker_events = tester.get(f"/api/cycle/units/{testing['id']}/events")
        assert worker_events.status_code == 200, worker_events.text
        allowed = {"result", "next", "to", "outcome", "condition_bucket"}
        for event in worker_events.json()["items"]:
            assert set(event["payload"]) <= allowed, event

        manager_detail = manager.get(f"/api/cycle/units/{testing['id']}")
        assert manager_detail.status_code == 200, manager_detail.text
        manager_body = manager_detail.json()
        assert "vendor_name" in manager_body
        assert "source_receipt_id" in manager_body
        assert manager_body["serial_number"] == "SN-O-1"
        manager_events = manager.get(f"/api/cycle/units/{testing['id']}/events")
        payloads = [event["payload"] for event in manager_events.json()["items"]]
        assert any("receipt_id" in payload for payload in payloads)
        # 主管也不放成本/价格/token
        serialized = str(manager_events.json())
        for forbidden in ("cost", "price", "token"):
            assert forbidden not in serialized


def test_inventory_and_bad_salvage_untouched_by_cycle_transitions(db):
    part = _part(db, "CYCLE-PN-P")
    db.commit()
    inventory_before = _count(db, Inventory)
    salvage_before = _count(db, MaintenanceBadSalvage)
    with _recycle(db, "cycle-recycle-p2") as recycle, _manager(db, "cycle-mgr-p") as manager:
        for outcome, bucket in (("to_shop", "good"), ("to_company", None), ("scrap", None)):
            unit = _intake(recycle, part, 1).json()["items"][0]
            closed = _transition(recycle, unit, "close", outcome=outcome,
                                 condition_bucket=bucket)
            assert closed.status_code == 200, closed.text
        unit = _intake(recycle, part, 1).json()["items"][0]
        withdrawn = _transition(recycle, unit, "withdraw")
        assert withdrawn.status_code == 200, withdrawn.text
    assert _count(db, Inventory) == inventory_before
    assert _count(db, MaintenanceBadSalvage) == salvage_before


# ---------------------------------------------------------------------------
# WIP 在制只读
# ---------------------------------------------------------------------------


def test_wip_counts_stations_and_validates_part_ids(db):
    part = _part(db, "CYCLE-PN-Q")
    db.commit()
    with _recycle(db, "cycle-recycle-q") as recycle, _manager(db, "cycle-mgr-q") as manager, \
            _client(db, "cycle-wip-q", page_cycle_wip=True) as wip_reader:
        testing = _intake(recycle, part, 1).json()["items"][0]
        _dispatch(recycle, testing, "testing")
        ready = _intake(recycle, part, 1).json()["items"][0]
        _transition(recycle, ready, "self-pass")
        outsource = _intake(recycle, part, 1).json()["items"][0]
        _dispatch(recycle, outsource, "outsource", vendor_name="委外点Q")

        response = wip_reader.get("/api/cycle/wip", params={"part_ids": str(part.id)})
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        assert item["part_id"] == part.id
        assert item["testing"] == 1
        assert item["repairing"] == 0
        assert item["outsource"] == 1
        assert item["ready"] == 1
        # 主管也可读 WIP
        assert manager.get("/api/cycle/wip", params={"part_ids": str(part.id)}).status_code == 200
        # recycle 无 wip 键 → 403
        assert recycle.get("/api/cycle/wip", params={"part_ids": str(part.id)}).status_code == 403
        for bad in ("0", "abc", ""):
            invalid = wip_reader.get("/api/cycle/wip", params={"part_ids": bad})
            assert invalid.status_code in (400, 422), invalid.text


# ---------------------------------------------------------------------------
# 权限边界
# ---------------------------------------------------------------------------


def test_unrelated_page_key_cannot_read_or_write_cycle(db):
    # page_maintenance（稳定版维保）不是坏件循环页键：不能读写循环单位。
    part = _part(db, "CYCLE-PN-R")
    db.commit()
    client = _client(db, "cycle-unrelated-r", page_maintenance=True)
    assert client.get("/api/cycle/units").status_code == 403
    blocked = client.post(
        "/api/cycle/units/intake-external", json={"part_id": part.id, "qty": 1}
    )
    assert blocked.status_code == 403, blocked.text


def test_tester_cannot_mutate_pool_unit(db):
    part = _part(db, "CYCLE-PN-S")
    db.commit()
    with _recycle(db, "cycle-recycle-s") as recycle, _tester(db, "cycle-tester-s") as tester:
        unit = _intake(recycle, part, 1).json()["items"][0]
        for action, body in (
            ("dispatch", {"to": "testing"}),
            ("self-pass", {}),
            ("close", {"outcome": "scrap"}),
        ):
            response = tester.post(
                f"/api/cycle/units/{unit['id']}/{action}",
                json={"expected_version": unit["version"], **body},
            )
            assert response.status_code == 403, (action, response.text)
        assert _unit(db, unit["id"]).station == "pool"


def test_station_visibility_follows_page_key_mapping(db):
    part = _part(db, "CYCLE-PN-T")
    db.commit()
    with _recycle(db, "cycle-vis-recycle") as recycle, _manager(db, "cycle-vis-mgr") as manager:
        pool = _intake(recycle, part, 1).json()["items"][0]
        testing = _intake(recycle, part, 1).json()["items"][0]
        assert _dispatch(recycle, testing, "testing").status_code == 200
        ready = _intake(recycle, part, 1).json()["items"][0]
        assert _transition(recycle, ready, "self-pass").status_code == 200
        outsource = _intake(recycle, part, 1).json()["items"][0]
        assert _dispatch(recycle, outsource, "outsource", vendor_name="V-T").status_code == 200
        closed = _intake(recycle, part, 1).json()["items"][0]
        assert _transition(recycle, closed, "close", outcome="scrap").status_code == 200

        # P-02：回收页 = pool/outsource/ready，不含 testing/closed
        recycle_ids = {row["id"] for row in recycle.get("/api/cycle/units").json()["items"]}
        assert {pool["id"], ready["id"], outsource["id"]} <= recycle_ids
        assert testing["id"] not in recycle_ids
        assert closed["id"] not in recycle_ids
        # P-02：主管全览含 closed
        manager_ids = {row["id"] for row in manager.get("/api/cycle/units").json()["items"]}
        assert {pool["id"], testing["id"], ready["id"], outsource["id"],
                closed["id"]} <= manager_ids
        # A-20：不存在的单位 404
        assert recycle.get("/api/cycle/units/999999999").status_code == 404


def test_assignee_scope_limits_test_and_repair_station_rows(db):
    part = _part(db, "CYCLE-PN-U")
    db.commit()
    with _recycle(db, "cycle-scope-recycle") as recycle, \
            _tester(db, "cycle-scope-tester-a") as tester_a, \
            _tester(db, "cycle-scope-tester-b") as tester_b:
        unit = _intake(recycle, part, 1).json()["items"][0]
        assert _dispatch(
            recycle, unit, "testing", assignee_username="cycle-scope-tester-b"
        ).status_code == 200

        ids_a = {row["id"] for row in tester_a.get("/api/cycle/units").json()["items"]}
        ids_b = {row["id"] for row in tester_b.get("/api/cycle/units").json()["items"]}
        assert unit["id"] not in ids_a  # P-04：负责他人的检测件不可见
        assert unit["id"] in ids_b
        assert tester_a.get(f"/api/cycle/units/{unit['id']}").status_code == 404


def test_close_to_shop_intent_is_part_of_the_same_transaction(db):
    """A-23/R1：意图与结案同一事务；事务回滚后单位仍在 pool、无意图。"""
    from app.db import SessionLocal
    from app.security import UserContext
    from app.services import maintenance_cycle as cycle_service

    part = _part(db, "CYCLE-PN-V")
    db.commit()
    recycle = _recycle(db, "cycle-tx-recycle")
    unit = _intake(recycle, part, 1).json()["items"][0]
    ctx = UserContext(
        user_id="cycle-tx-recycle",
        role="readonly",
        permissions={
            **permissions.effective("readonly", None),
            "page_cycle_recycle": True,
            "page_cycle_manage": True,
        },
        is_authenticated=True,
    )
    with SessionLocal() as session:
        cycle_service.close(
            session,
            unit_id=unit["id"],
            expected_version=unit["version"],
            outcome="to_shop",
            condition_bucket="good",
            note=None,
            actor="cycle-tx-recycle",
            ctx=ctx,
        )
        session.flush()
        assert session.scalar(
            select(func.count())
            .select_from(MaintenanceCycleSellIntent)
            .where(MaintenanceCycleSellIntent.unit_id == unit["id"])
        ) == 1
        session.rollback()
    db.expire_all()
    stored = db.get(MaintenanceCycleUnit, unit["id"])
    assert stored.station == "pool"
    assert stored.version == unit["version"]
    assert _intents(db, unit["id"]) == []
