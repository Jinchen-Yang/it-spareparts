"""T11 复审修复回归（PR #528 / D-0057）：F1–F5。

- F1：通用 `/api/work-items/*` 写入口对 cycle subject 一律 409；空负责人检测/维修单位
  必须有「可领取活待办」才可见；完成检测/维修必须存在活待办。
- F2：`filter_items` 对非主管清空 last_reason；事件业务 payload 按 §6.4 收紧。
- F3：mark-sold 人工路径先做 expected_version 校验。
- F4：入池收货 NotFound/Conflict/Validation 映射为 404/409/400。
- F5：回执 CAS 用数据库实时时钟比较租约截止。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app import permissions
from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.models.maintenance_cycle import MaintenanceCycleSellIntent, MaintenanceCycleUnit
from app.models.system import SysUser
from app.models.workflow import SubjectEvent, WorkItem
from app.security import UserContext
from app.services import cycle_sell_delivery as sell
from app.services import maintenance_cycle as cycle_service
from app.services import maintenance_return_receipts as receipts
from app.services.workflow import work_items as wf
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt


def _client(db, username: str, **overrides):
    return client_for(db, username=username, role="readonly", overrides=overrides or {})


def _recycle(db, username: str):
    return _client(db, username, page_cycle_recycle=True)


def _tester(db, username: str):
    return _client(db, username, page_cycle_test=True)


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


def _manager_ctx(db, username: str) -> UserContext:
    """为直接调用 work_item 服务构造与登录 token 同口径的主管上下文。"""
    base = permissions.effective("readonly", None)
    graph = permissions.effective_from_snapshot(
        base, {"page_cycle_manage": True, "page_cycle_test": True,
               "page_cycle_repair": True}
    )
    return UserContext(user_id=username, role="readonly", permissions=graph,
                       is_authenticated=True)


# ---------------------------------------------------------------------------
# F1：通用待办写入口对 cycle subject 一律拒绝
# ---------------------------------------------------------------------------


def _generic_action_calls(db, tester_client, manager_client, item, tester_id):
    return [
        ("claim", tester_client, {"expected_version": item.version}),
        ("start", tester_client, {"expected_version": item.version}),
        ("complete", tester_client, {"expected_version": item.version}),
        ("release", tester_client, {"expected_version": item.version, "reason": "x"}),
        ("decline", tester_client, {"expected_version": item.version, "reason": "x"}),
        (
            "transfer",
            tester_client,
            {"expected_version": item.version, "reason": "x", "to_user_id": tester_id},
        ),
        (
            "reassign",
            manager_client,
            {"expected_version": item.version, "reason": "x", "to_user_id": None},
        ),
    ]


def test_generic_work_item_actions_rejected_for_cycle_subjects(db):
    part = _part(db, "CYCLE-FIX-F1A")
    db.commit()
    with _recycle(db, "fix-recycle-a") as recycle, \
            _tester(db, "fix-tester-a") as tester, \
            _manager(db, "fix-mgr-a") as manager:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "testing")
        assert dispatched.status_code == 200, dispatched.text
        item = _active_items(db, unit["id"])[0]
        tester_user = db.scalar(
            select(SysUser).where(SysUser.username == "fix-tester-a")
        )
        before_version = item.version
        db.expire_all()
        before_events = db.scalar(
            select(func.count())
            .select_from(SubjectEvent)
            .where(
                SubjectEvent.subject_type == "cycle_test",
                SubjectEvent.subject_id == unit["id"],
            )
        )
        for action, client, body in _generic_action_calls(
            db, tester, manager, item, tester_user.id
        ):
            response = client.post(f"/api/work-items/{item.id}/{action}", json=body)
            assert response.status_code == 409, (action, response.text)
            assert "/api/cycle" in response.json()["detail"]
        stored_unit = _unit(db, unit["id"])
        assert stored_unit.station == "testing"
        assert stored_unit.assignee_username is None
        assert stored_unit.version == dispatched.json()["version"]
        stored_item = _active_items(db, unit["id"])[0]
        assert stored_item.status == "pending"
        assert stored_item.version == before_version
        assert stored_item.assignee_id is None
        db.expire_all()
        after_events = db.scalar(
            select(func.count())
            .select_from(SubjectEvent)
            .where(
                SubjectEvent.subject_type == "cycle_test",
                SubjectEvent.subject_id == unit["id"],
            )
        )
        assert after_events == before_events  # 通用动作被拒绝，不得追加待办事件


def test_mixed_generic_and_cycle_entrypoints_do_not_split_assignee(db):
    """混用回归：通用入口被拒绝后，循环领取仍能正确联动单位负责人。"""
    part = _part(db, "CYCLE-FIX-F1B")
    db.commit()
    with _recycle(db, "fix-recycle-b") as recycle, \
            _tester(db, "fix-tester-b") as tester:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "testing").json()
        item = _active_items(db, unit["id"])[0]
        assert tester.post(
            f"/api/work-items/{item.id}/claim",
            json={"expected_version": item.version},
        ).status_code == 409
        claimed = tester.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={
                "expected_version": dispatched["version"],
                "item_version": item.version,
            },
        )
        assert claimed.status_code == 200, claimed.text
        stored_unit = _unit(db, unit["id"])
        assert stored_unit.assignee_username == "fix-tester-b"
        stored_item = _active_items(db, unit["id"])[0]
        assert stored_item.status == "in_progress"
        tester_b_user = db.scalar(
            select(SysUser).where(SysUser.username == "fix-tester-b")
        )
        # 领取后通用 release/transfer 仍必须拒绝，防止待办归属与单位分裂
        for action, body in (
            ("release", {"expected_version": stored_item.version, "reason": "x"}),
            (
                "transfer",
                {
                    "expected_version": stored_item.version,
                    "reason": "x",
                    "to_user_id": tester_b_user.id,
                },
            ),
        ):
            response = tester.post(
                f"/api/work-items/{stored_item.id}/{action}", json=body
            )
            assert response.status_code == 409, response.text
        assert _unit(db, unit["id"]).assignee_username == "fix-tester-b"


def test_generic_claim_race_with_cycle_claim_only_cycle_path_wins(db):
    part = _part(db, "CYCLE-FIX-F1C")
    db.commit()
    with _recycle(db, "fix-recycle-c") as recycle, \
            _tester(db, "fix-tester-c1") as tester_a, \
            _tester(db, "fix-tester-c2") as tester_b:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "testing").json()
        item = _active_items(db, unit["id"])[0]
        generic_body = {"expected_version": item.version}
        cycle_body = {
            "expected_version": dispatched["version"],
            "item_version": item.version,
        }

        def generic_call():
            return tester_b.post(
                f"/api/work-items/{item.id}/claim", json=generic_body
            )

        def cycle_call():
            return tester_a.post(
                f"/api/cycle/units/{unit['id']}/claim", json=cycle_body
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            generic_future = pool.submit(generic_call)
            cycle_future = pool.submit(cycle_call)
            generic_response = generic_future.result(timeout=15)
            cycle_response = cycle_future.result(timeout=15)
        assert generic_response.status_code == 409, generic_response.text
        assert cycle_response.status_code == 200, cycle_response.text
        assert _unit(db, unit["id"]).assignee_username == "fix-tester-c1"


def test_unassigned_unit_without_claimable_item_is_invisible_and_not_completable(db):
    part = _part(db, "CYCLE-FIX-F1D")
    db.commit()
    with _recycle(db, "fix-recycle-d") as recycle, \
            _tester(db, "fix-tester-d") as tester, \
            _manager(db, "fix-mgr-d") as manager:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "testing").json()
        item = _active_items(db, unit["id"])[0]
        # 模拟“待办已被错误关闭/丢失”的异常态：没有可领取活待办
        db.execute(
            update(WorkItem).where(WorkItem.id == item.id).values(status="cancelled")
        )
        db.commit()

        listing = tester.get("/api/cycle/units")
        assert listing.status_code == 200, listing.text
        assert unit["id"] not in {row["id"] for row in listing.json()["items"]}
        assert tester.get(f"/api/cycle/units/{unit['id']}").status_code == 404
        blocked = tester.post(
            f"/api/cycle/units/{unit['id']}/complete-test",
            json={"expected_version": dispatched["version"], "result": "good"},
        )
        assert blocked.status_code == 404, blocked.text
        # 主管可见但“无活待办”不得静默放行完成
        manager_blocked = manager.post(
            f"/api/cycle/units/{unit['id']}/complete-test",
            json={"expected_version": dispatched["version"], "result": "good"},
        )
        assert manager_blocked.status_code == 409, manager_blocked.text
        stored = _unit(db, unit["id"])
        assert stored.station == "testing"
        assert stored.version == dispatched["version"]


def test_unassigned_unit_with_claimable_item_visible_and_claimable(db):
    part = _part(db, "CYCLE-FIX-F1E")
    db.commit()
    with _recycle(db, "fix-recycle-e") as recycle, _tester(db, "fix-tester-e") as tester:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "testing").json()
        item = _active_items(db, unit["id"])[0]
        listing = tester.get("/api/cycle/units")
        assert listing.status_code == 200
        assert unit["id"] in {row["id"] for row in listing.json()["items"]}
        assert tester.get(f"/api/cycle/units/{unit['id']}").status_code == 200
        claimed = tester.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={"expected_version": dispatched["version"],
                  "item_version": item.version},
        )
        assert claimed.status_code == 200, claimed.text


# ---------------------------------------------------------------------------
# F2：自由文本 last_reason 与事件业务 payload 白名单
# ---------------------------------------------------------------------------

SENSITIVE_REASON = "SN-PRIVATE vendor-PRIVATE project-PRIVATE"


def test_worker_work_item_list_and_events_do_not_leak_reason(db):
    part = _part(db, "CYCLE-FIX-F2")
    db.commit()
    with _recycle(db, "fix-recycle-f2") as recycle, \
            _tester(db, "fix-tester-f2") as tester, \
            _manager(db, "fix-mgr-f2") as manager:
        unit = _intake(recycle, part)[0]
        assert _dispatch(recycle, unit, "testing").status_code == 200
        item = _active_items(db, unit["id"])[0]
        manager_ctx = _manager_ctx(db, "fix-mgr-f2")
        # 走引擎主管 reassign 写入自由文本（绕开 API 守卫，模拟历史/内部数据）
        wf.reassign(
            db,
            item.id,
            manager_ctx,
            to_user_id=None,
            reason=SENSITIVE_REASON,
            expected_version=item.version,
        )
        db.add(
            SubjectEvent(
                subject_type="cycle_test",
                subject_id=unit["id"],
                kind="synthetic",
                actor=None,
                payload={"result": "good", "reason": SENSITIVE_REASON, "version": 9},
            )
        )
        db.commit()

        worker_list = tester.get(
            "/api/work-items", params={"tab": "pool", "subject_type": "cycle_test"}
        )
        assert worker_list.status_code == 200, worker_list.text
        rendered = str(worker_list.json())
        assert SENSITIVE_REASON not in rendered
        assert "PRIVATE" not in rendered
        row = [
            entry
            for entry in worker_list.json()["items"]
            if entry["id"] == item.id
        ][0]
        assert row["last_reason"] is None

        worker_events = tester.get(f"/api/subjects/cycle_test/{unit['id']}/events")
        assert worker_events.status_code == 200, worker_events.text
        assert "PRIVATE" not in str(worker_events.json())
        for event in worker_events.json()["items"]:
            assert set(event["payload"]) <= cycle_service.WORKER_SUBJECT_EVENT_KEYS

        # 主管审计保留：列表仍可见 last_reason 原文
        manager_list = manager.get(
            "/api/work-items", params={"tab": "all", "subject_type": "cycle_test"}
        )
        assert manager_list.status_code == 200, manager_list.text
        manager_row = [
            entry
            for entry in manager_list.json()["items"]
            if entry["id"] == item.id
        ][0]
        assert manager_row["last_reason"] == SENSITIVE_REASON
        manager_events = manager.get(f"/api/subjects/cycle_test/{unit['id']}/events")
        payloads = [event["payload"] for event in manager_events.json()["items"]]
        assert any(payload.get("reason") == SENSITIVE_REASON for payload in payloads)


# ---------------------------------------------------------------------------
# F3：mark-sold 人工路径版本校验前置
# ---------------------------------------------------------------------------


def test_mark_sold_stale_version_conflicts_even_when_already_sold(db):
    part = _part(db, "CYCLE-FIX-F3")
    db.commit()
    with _recycle(db, "fix-recycle-f3") as recycle, _manager(db, "fix-mgr-f3") as manager:
        unit = _intake(recycle, part)[0]
        ready = recycle.post(
            f"/api/cycle/units/{unit['id']}/self-pass",
            json={"expected_version": unit["version"]},
        ).json()
        closed = recycle.post(
            f"/api/cycle/units/{unit['id']}/close",
            json={"expected_version": ready["version"], "outcome": "to_shop",
                  "condition_bucket": "good"},
        )
        assert closed.status_code == 200, closed.text
        closed_body = closed.json()
        sold = manager.post(
            f"/api/cycle/units/{unit['id']}/mark-sold",
            json={"expected_version": closed_body["version"]},
        )
        assert sold.status_code == 200, sold.text
        sold_version = sold.json()["version"]

        stale = manager.post(
            f"/api/cycle/units/{unit['id']}/mark-sold",
            json={"expected_version": closed_body["version"]},
        )
        assert stale.status_code == 409, stale.text
        current = manager.post(
            f"/api/cycle/units/{unit['id']}/mark-sold",
            json={"expected_version": sold_version},
        )
        assert current.status_code == 200, current.text
        assert current.json()["outcome"] == "sold"
        assert _unit(db, unit["id"]).version == sold_version


# ---------------------------------------------------------------------------
# F4：入池收货异常映射（不存在/版本冲突/已作废/归属竞争）
# ---------------------------------------------------------------------------


def _handoff(client, receipt_id: str, version: int, qty: int = 1):
    return client.post(
        "/api/cycle/units/handoff-from-receipt",
        json={
            "receipt_id": receipt_id,
            "qty": qty,
            "expected_receipt_version": version,
        },
    )


def test_handoff_missing_receipt_is_404(db):
    with _recycle(db, "fix-recycle-f4a") as recycle:
        response = _handoff(recycle, str(uuid4()), 1)
        assert response.status_code == 404, response.text
    assert db.scalar(select(MaintenanceCycleUnit)) is None


def test_handoff_stale_receipt_version_is_409(db):
    project = _project(db, "FIX-F4B")
    part = _part(db, "CYCLE-FIX-F4B")
    receipt = _receipt(db, project, part, "2", condition="坏品")
    db.commit()
    with _recycle(db, "fix-recycle-f4b") as recycle:
        response = _handoff(recycle, receipt.rkd_line_id, receipt.version + 1)
        assert response.status_code == 409, response.text
    assert db.scalar(select(MaintenanceCycleUnit)) is None


def test_handoff_voided_receipt_is_409(db):
    project = _project(db, "FIX-F4C")
    part = _part(db, "CYCLE-FIX-F4C")
    receipt = _receipt(db, project, part, "1", condition="坏品", status="voided")
    db.commit()
    with _recycle(db, "fix-recycle-f4c") as recycle:
        response = _handoff(recycle, receipt.rkd_line_id, receipt.version)
        assert response.status_code == 409, response.text
    assert db.scalar(select(MaintenanceCycleUnit)) is None


def test_handoff_receipt_ownership_conflict_is_409(db, monkeypatch):
    project = _project(db, "FIX-F4D")
    part = _part(db, "CYCLE-FIX-F4D")
    receipt = _receipt(db, project, part, "1", condition="坏品")
    db.commit()

    def conflict(*_args, **_kwargs):
        raise receipts.ReturnReceiptConflict("返还记录归属已变化，请刷新后重试")

    monkeypatch.setattr(receipts, "lock_receipt_row", conflict)
    with _recycle(db, "fix-recycle-f4d") as recycle:
        response = _handoff(recycle, receipt.rkd_line_id, receipt.version)
        assert response.status_code == 409, response.text
    assert db.scalar(select(MaintenanceCycleUnit)) is None


# ---------------------------------------------------------------------------
# F5：回执 CAS 用数据库实时时钟（跨租约到期点不回写）
# ---------------------------------------------------------------------------


def test_ack_cas_fails_when_lock_wait_crosses_lease_expiry(db):
    part = _part(db, "CYCLE-FIX-F5")
    db.flush()
    unit = MaintenanceCycleUnit(
        part_id=part.id,
        pn=part.pn_std,
        station="closed",
        outcome="to_shop",
        condition_bucket="good",
        source_kind="external",
        closed_at=datetime.now(timezone.utc),
        created_by="fix-f5",
        updated_by="fix-f5",
    )
    db.add(unit)
    db.flush()
    intent = MaintenanceCycleSellIntent(
        unit_id=unit.id,
        part_id=part.id,
        qty=Decimal(1),
        condition_bucket="good",
        state="pending",
        attempts=0,
    )
    db.add(intent)
    db.commit()
    claim = sell.claim_one()
    assert claim is not None
    # 把租约压到 0.35 秒后到期；随后另一个事务短暂持锁，ack 在取锁等待中跨过到期点。
    db.execute(
        update(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.delivery_id == claim.delivery_id)
        .values(lease_until=datetime.now(timezone.utc) + timedelta(seconds=0.35))
    )
    db.commit()

    started = monotonic()
    with SessionLocal() as blocker:
        blocker.execute(
            select(MaintenanceCycleSellIntent)
            .where(
                MaintenanceCycleSellIntent.delivery_id == claim.delivery_id
            )
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sell.acknowledge, claim, "applied")
            # 确认 ack 正在等待行锁（未完成）
            while future.running() and monotonic() - started < 0.2:
                sleep(0.01)
            assert future.running(), "ack 应阻塞在行锁上"
            # 持锁等到租约（0.35s）过期后再放锁
            sleep(max(0.0, 0.5 - (monotonic() - started)))
            blocker.rollback()
            result = future.result(timeout=5)
    assert result is False
    db.expire_all()
    stored = db.get(MaintenanceCycleSellIntent, claim.delivery_id)
    assert stored.state == "delivering"
    assert stored.receipt_status is None
    assert stored.completed_at is None
