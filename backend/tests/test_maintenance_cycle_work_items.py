"""坏件循环 T3：待办接线契约（任务书 §6.4 / §11 T3 / §12 R6）。

覆盖：两个 subject 注册、派工开待办（指派/公共池）、领取入口复用 work_item.claim、
检测转维修结束检测待办并开未指派维修待办、完成动作 complete、委外不开待办、
摘要/事件字段裁剪、待办写入失败时工位回滚。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.maintenance_cycle import MaintenanceCycleEvent, MaintenanceCycleUnit
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.workflow import SubjectEvent, WorkItem
from app.services import maintenance_cycle as cycle_service
from app.services.workflow import work_items as wf
from app.services.workflow.registry import registered
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt


def _client(db, username: str, **overrides):
    return client_for(db, username=username, role="readonly", overrides=overrides or {})


def _recycle(db, username: str):
    return _client(db, username, page_cycle_recycle=True)


def _tester(db, username: str):
    return _client(db, username, page_cycle_test=True)


def _repairer(db, username: str):
    return _client(db, username, page_cycle_repair=True)


def _manager(db, username: str):
    return _client(db, username, page_cycle_manage=True)


def _intake(client, part, qty: int = 1):
    response = client.post(
        "/api/cycle/units/intake-external", json={"part_id": part.id, "qty": qty}
    )
    assert response.status_code == 201, response.text
    return response.json()["items"]


def _dispatch(client, unit: dict, to: str, **extra):
    return client.post(
        f"/api/cycle/units/{unit['id']}/dispatch",
        json={"expected_version": unit["version"], "to": to, **extra},
    )


def _unit(db, unit_id: int) -> MaintenanceCycleUnit:
    db.expire_all()
    unit = db.get(MaintenanceCycleUnit, unit_id)
    assert unit is not None
    return unit


def _active_items(db, unit_id: int) -> list[WorkItem]:
    db.expire_all()
    return list(
        db.scalars(
            select(WorkItem)
            .where(
                WorkItem.subject_type.in_(("cycle_test", "cycle_repair")),
                WorkItem.subject_id == unit_id,
                WorkItem.status.in_(("pending", "in_progress")),
            )
            .order_by(WorkItem.id)
        ).all()
    )


def _all_items(db, unit_id: int) -> list[WorkItem]:
    db.expire_all()
    return list(
        db.scalars(
            select(WorkItem)
            .where(
                WorkItem.subject_type.in_(("cycle_test", "cycle_repair")),
                WorkItem.subject_id == unit_id,
            )
            .order_by(WorkItem.id)
        ).all()
    )


def _pool_item(client, unit_id: int) -> dict:
    response = client.get("/api/work-items", params={"tab": "pool", "page_size": 100})
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        if item["subject_id"] == unit_id:
            return item
    raise AssertionError(f"公共池没有 unit {unit_id} 的待办：{response.json()}")


# ---------------------------------------------------------------------------
# W-01 subject 注册
# ---------------------------------------------------------------------------


def test_cycle_subjects_registered_with_expected_permissions():
    specs = registered()
    assert "cycle_test" in specs and "cycle_repair" in specs
    test_spec = specs["cycle_test"]
    repair_spec = specs["cycle_repair"]
    assert test_spec.pool_key == "cycle_test"
    assert test_spec.pool_permission == "page_cycle_test"
    assert test_spec.supervisor_permission == "page_cycle_manage"
    assert repair_spec.pool_key == "cycle_repair"
    assert repair_spec.pool_permission == "page_cycle_repair"
    assert repair_spec.supervisor_permission == "page_cycle_manage"


# ---------------------------------------------------------------------------
# W-02/W-03 派工产生待办；公共池领取
# ---------------------------------------------------------------------------


def test_dispatch_opens_assigned_and_pool_work_items_but_not_for_outsource(db):
    part = _part(db, "CYCLE-WI-A")
    db.commit()
    with _recycle(db, "wi-recycle-a") as recycle, _tester(db, "wi-tester-a") as tester:
        assigned_unit = _intake(recycle, part)[0]
        pooled_unit = _intake(recycle, part)[0]
        outward_unit = _intake(recycle, part)[0]

        assert _dispatch(
            recycle, assigned_unit, "testing", assignee_username="wi-tester-a"
        ).status_code == 200
        assert _dispatch(recycle, pooled_unit, "testing").status_code == 200
        assert _dispatch(
            recycle, outward_unit, "outsource", vendor_name="委外点WI"
        ).status_code == 200

        assigned_items = _active_items(db, assigned_unit["id"])
        assert len(assigned_items) == 1
        assert assigned_items[0].subject_type == "cycle_test"
        assert assigned_items[0].status == "pending"
        assert assigned_items[0].assignee_id is not None

        pooled_items = _active_items(db, pooled_unit["id"])
        assert len(pooled_items) == 1
        assert pooled_items[0].assignee_id is None
        assert pooled_items[0].status == "pending"

        # 委外不打开工人待办（W-02/A-28）
        assert _active_items(db, outward_unit["id"]) == []

        # 同一单位最多一张未结束待办（W-06）
        assert len(_active_items(db, pooled_unit["id"])) == 1


def test_pool_item_summary_fields_and_claim_flow(db):
    part = _part(db, "CYCLE-WI-B")
    db.commit()
    with _recycle(db, "wi-recycle-b") as recycle, \
            _tester(db, "wi-tester-b1") as tester_a, \
            _tester(db, "wi-tester-b2") as tester_b:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "testing")
        assert dispatched.status_code == 200, dispatched.text
        item = _pool_item(tester_a, unit["id"])
        # W-08：摘要只有 pn/part_id/station/unit_uid
        assert set(item["summary"]) == {"pn", "part_id", "station", "unit_uid"}
        assert item["summary"]["station"] == "testing"
        assert "vendor_name" not in item["summary"]
        assert "source_receipt_id" not in item["summary"]

        claimed = tester_a.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={
                "expected_version": dispatched.json()["version"],
                "item_version": item["version"],
            },
        )
        assert claimed.status_code == 200, claimed.text
        body = claimed.json()
        assert body["assignee_username"] == "wi-tester-b1"
        assert body["version"] == dispatched.json()["version"] + 1
        stored_item = _active_items(db, unit["id"])[0]
        assert stored_item.status == "in_progress"
        assert stored_item.claimed_by is not None

        # W-03：第二个工人不能重复领取（单位已指派 → 409）
        fresh_unit = _unit(db, unit["id"])
        second = tester_b.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={
                "expected_version": fresh_unit.version,
                "item_version": stored_item.version,
            },
        )
        assert second.status_code == 409, second.text


def test_claim_rejects_stale_item_version(db):
    part = _part(db, "CYCLE-WI-C")
    db.commit()
    with _recycle(db, "wi-recycle-c") as recycle, _tester(db, "wi-tester-c") as tester:
        unit = _intake(recycle, part)[0]
        assert _dispatch(recycle, unit, "testing").status_code == 200
        stale = tester.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={"expected_version": unit["version"], "item_version": 99},
        )
        assert stale.status_code == 409, stale.text
        # 领取失败零副作用：单位仍未指派、待办仍 pending
        assert _unit(db, unit["id"]).assignee_username is None
        assert _active_items(db, unit["id"])[0].status == "pending"


def test_cross_station_claim_is_forbidden_by_pool_permission(db):
    part = _part(db, "CYCLE-WI-D")
    db.commit()
    with _recycle(db, "wi-recycle-d") as recycle, _tester(db, "wi-tester-d") as tester:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "repairing")
        assert dispatched.status_code == 200, dispatched.text
        item = _active_items(db, unit["id"])[0]
        denied = tester.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={
                "expected_version": dispatched.json()["version"],
                "item_version": item.version,
            },
        )
        # 检测岗看不到维修工位单位：可见性优先 → 404（不能让测试岗越权领取）
        assert denied.status_code == 404, denied.text


# ---------------------------------------------------------------------------
# W-05/W-06/W-07 检测转维修与完成联动
# ---------------------------------------------------------------------------


def test_complete_test_good_completes_work_item(db):
    part = _part(db, "CYCLE-WI-E")
    db.commit()
    with _recycle(db, "wi-recycle-e") as recycle, _tester(db, "wi-tester-e") as tester:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(
            recycle, unit, "testing", assignee_username="wi-tester-e"
        ).json()
        item = _active_items(db, unit["id"])[0]
        item_id = item.id
        item_version_before = item.version
        done = tester.post(
            f"/api/cycle/units/{unit['id']}/complete-test",
            json={"expected_version": dispatched["version"], "result": "good"},
        )
        assert done.status_code == 200, done.text
        assert done.json()["station"] == "ready"
        assert _active_items(db, unit["id"]) == []
        db.expire_all()
        stored = db.get(WorkItem, item_id)
        assert stored.status == "done"
        assert stored.version == item_version_before + 1


def test_detection_to_repair_ends_test_item_and_opens_unassigned_repair_item(db):
    part = _part(db, "CYCLE-WI-F")
    db.commit()
    with _recycle(db, "wi-recycle-f") as recycle, _tester(db, "wi-tester-f") as tester:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(
            recycle, unit, "testing", assignee_username="wi-tester-f"
        ).json()
        test_item = _active_items(db, unit["id"])[0]
        bad = tester.post(
            f"/api/cycle/units/{unit['id']}/complete-test",
            json={
                "expected_version": dispatched["version"],
                "result": "bad",
                "next": "repairing",
            },
        )
        assert bad.status_code == 200, bad.text
        body = bad.json()
        assert body["station"] == "repairing"
        assert body["assignee_username"] is None  # R6
        stored_unit = _unit(db, unit["id"])
        assert stored_unit.repair_round == 1

        db.expire_all()
        old_item = db.get(WorkItem, test_item.id)
        assert old_item.status == "done"
        repair_items = _active_items(db, unit["id"])
        assert len(repair_items) == 1
        assert repair_items[0].subject_type == "cycle_repair"
        assert repair_items[0].assignee_id is None
        assert repair_items[0].status == "pending"


def test_complete_repair_ends_repair_item_and_reopens_test_item(db):
    part = _part(db, "CYCLE-WI-G")
    db.commit()
    with _recycle(db, "wi-recycle-g") as recycle, _repairer(db, "wi-repairer-g") as repairer:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "repairing").json()
        item = _pool_item(repairer, unit["id"])
        claimed = repairer.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={"expected_version": dispatched["version"], "item_version": item["version"]},
        )
        assert claimed.status_code == 200, claimed.text
        fixed = repairer.post(
            f"/api/cycle/units/{unit['id']}/complete-repair",
            json={"expected_version": claimed.json()["version"], "result": "fixed",
                  "next": "testing"},
        )
        assert fixed.status_code == 200, fixed.text
        assert fixed.json()["station"] == "testing"
        db.expire_all()
        assert db.get(WorkItem, item["id"]).status == "done"
        active = _active_items(db, unit["id"])
        assert len(active) == 1
        assert active[0].subject_type == "cycle_test"
        assert active[0].assignee_id is None


def test_outsource_return_to_station_opens_pool_item(db):
    part = _part(db, "CYCLE-WI-H")
    db.commit()
    with _recycle(db, "wi-recycle-h") as recycle:
        unit = _intake(recycle, part)[0]
        out = _dispatch(recycle, unit, "outsource", vendor_name="委外点WI-H").json()
        returned = recycle.post(
            f"/api/cycle/units/{unit['id']}/outsource-return",
            json={"expected_version": out["version"], "next": "testing"},
        )
        assert returned.status_code == 200, returned.text
        active = _active_items(db, unit["id"])
        assert len(active) == 1
        assert active[0].subject_type == "cycle_test"
        assert active[0].assignee_id is None


# ---------------------------------------------------------------------------
# W-08 摘要/事件字段裁剪
# ---------------------------------------------------------------------------


def test_work_item_summary_and_subject_events_hide_sensitive_fields(db):
    project = _project(db, "WI-LEAK")
    part = _part(db, "CYCLE-WI-I")
    receipt = _receipt(db, project, part, "1", condition="坏品")
    db.commit()
    with _recycle(db, "wi-recycle-i") as recycle, \
            _tester(db, "wi-tester-i") as tester, \
            _manager(db, "wi-mgr-i") as manager:
        unit = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
                "serial_numbers": ["WI-SN-1"],
            },
        ).json()["items"][0]
        dispatched = _dispatch(
            recycle, unit, "testing", assignee_username="wi-tester-i"
        ).json()

        # 待办列表摘要（assigned tab）只有四个字段
        assigned = tester.get("/api/work-items", params={"tab": "assigned"})
        assert assigned.status_code == 200, assigned.text
        row = [
            item for item in assigned.json()["items"] if item["subject_id"] == unit["id"]
        ][0]
        assert set(row["summary"]) == {"pn", "part_id", "station", "unit_uid"}
        rendered = str(assigned.json())
        for forbidden in ("vendor_name", "serial_number", "source_receipt_id",
                          "project_id", "成本", "价格"):
            assert forbidden not in rendered

        # 待办时间线（subject events）工人 payload
        # F-10/N2：固定期望集合为任务书 §6.4 四字段 pn/part_id/station/unit_uid；
        # 引擎外壳（version/assignee_id/from_user_id/to_user_id）与自由文本不返回。
        expected_fields = {"pn", "part_id", "station", "unit_uid"}
        engine_shell_keys = {"version", "assignee_id", "from_user_id", "to_user_id"}
        worker_events = tester.get(f"/api/subjects/cycle_test/{unit['id']}/events")
        assert worker_events.status_code == 200, worker_events.text
        assert worker_events.json()["items"], worker_events.text
        for event in worker_events.json()["items"]:
            payload = event["payload"]
            assert set(payload) == expected_fields, event
            assert set(payload).isdisjoint(engine_shell_keys), event
            assert payload["pn"] == part.pn_std
            assert payload["part_id"] == part.id
            assert payload["station"] == "testing"
        assert "WI-SN-1" not in str(worker_events.json())

        # 主管可见事件审计字段（引擎外壳保留），但同样不含供应商/SN/收货单
        manager_events = manager.get(f"/api/subjects/cycle_test/{unit['id']}/events")
        assert manager_events.status_code == 200, manager_events.text
        assert any(
            "version" in event["payload"] for event in manager_events.json()["items"]
        )
        assert "WI-SN-1" not in str(manager_events.json())
        assert "vendor_name" not in str(manager_events.json())


# ---------------------------------------------------------------------------
# W-10 待办写入失败 → 工位回滚
# ---------------------------------------------------------------------------


def test_work_item_open_failure_rolls_back_dispatch(db, monkeypatch):
    part = _part(db, "CYCLE-WI-J")
    db.commit()
    with _recycle(db, "wi-recycle-j") as recycle:
        unit = _intake(recycle, part)[0]
        version_before = unit["version"]

        def boom(*_args, **_kwargs):
            raise RuntimeError("synthetic work item failure")

        monkeypatch.setattr(cycle_service.work_items, "open_items", boom)
        with pytest.raises(RuntimeError):
            _dispatch(recycle, unit, "testing")
        stored = _unit(db, unit["id"])
        assert stored.station == "pool"
        assert stored.version == version_before
        assert _active_items(db, unit["id"]) == []
        db.expire_all()
        assert db.scalar(
            select(func.count())
            .select_from(MaintenanceCycleEvent)
            .where(
                MaintenanceCycleEvent.unit_id == unit["id"],
                MaintenanceCycleEvent.event_type == "dispatch",
            )
        ) == 0


def test_work_item_complete_failure_rolls_back_transition(db, monkeypatch):
    part = _part(db, "CYCLE-WI-K")
    db.commit()
    with _recycle(db, "wi-recycle-k") as recycle, _tester(db, "wi-tester-k") as tester:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(
            recycle, unit, "testing", assignee_username="wi-tester-k"
        ).json()
        item = _active_items(db, unit["id"])[0]

        def boom(*_args, **_kwargs):
            raise RuntimeError("synthetic work item complete failure")

        monkeypatch.setattr(cycle_service.work_items, "complete", boom)
        with pytest.raises(RuntimeError):
            tester.post(
                f"/api/cycle/units/{unit['id']}/complete-test",
                json={"expected_version": dispatched["version"], "result": "good"},
            )
        stored = _unit(db, unit["id"])
        assert stored.station == "testing"
        assert stored.version == dispatched["version"]
        db.expire_all()
        assert db.get(WorkItem, item.id).status == "pending"
        assert db.scalar(
            select(func.count())
            .select_from(MaintenanceCycleEvent)
            .where(
                MaintenanceCycleEvent.unit_id == unit["id"],
                MaintenanceCycleEvent.event_type == "complete_test",
            )
        ) == 0


# ---------------------------------------------------------------------------
# 收尾：待办不写敏感字段（payload 内容级）
# ---------------------------------------------------------------------------


def test_work_item_events_carry_no_sensitive_values(db):
    project = _project(db, "WI-PAYLOAD")
    part = _part(db, "CYCLE-WI-L")
    receipt = _receipt(db, project, part, "1", condition="成品")
    db.commit()
    with _recycle(db, "wi-recycle-l") as recycle, _tester(db, "wi-tester-l") as tester:
        unit = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
                "serial_numbers": ["WI-SN-L"],
            },
        ).json()["items"][0]
        dispatched = _dispatch(recycle, unit, "testing").json()
        item = _active_items(db, unit["id"])[0]
        claimed = tester.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={
                "expected_version": dispatched["version"],
                "item_version": item.version,
            },
        )
        assert claimed.status_code == 200, claimed.text
        assert tester.post(
            f"/api/cycle/units/{unit['id']}/complete-test",
            json={"expected_version": claimed.json()["version"], "result": "good"},
        ).status_code == 200

        db.expire_all()
        events = list(
            db.scalars(
                select(SubjectEvent).where(
                    SubjectEvent.subject_type.in_(("cycle_test", "cycle_repair")),
                    SubjectEvent.subject_id == unit["id"],
                )
            ).all()
        )
        assert events
        blob = str([event.payload for event in events])
        for forbidden in ("WI-SN-L", "vendor", "receipt", "CYCLE-WI-L", "成本", "价格"):
            assert forbidden not in blob
        # 引擎事件仅含版本/指派等审计键
        for event in events:
            assert set(event.payload) <= {"version", "assignee_id", "from_user_id",
                                          "to_user_id", "reason"}
