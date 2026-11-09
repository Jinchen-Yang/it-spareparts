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
from tests.test_maintenance_return_receipts_api import _client as _receipt_client


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
        # F-10：固定期望集合（明文），引擎外壳字段对工人不出现
        business_keys = {"result", "next", "to", "outcome", "condition_bucket"}
        engine_shell_keys = {"version", "assignee_id", "from_user_id", "to_user_id"}
        for event in worker_events.json()["items"]:
            payload_keys = set(event["payload"])
            assert payload_keys <= business_keys, event
            assert payload_keys.isdisjoint(engine_shell_keys), event

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


# ---------------------------------------------------------------------------
# 第二轮复审补修：F-04 mark-sold 遗留意图 / P2-C 互锁实际值 / F-08 claim 权限 / F-13 网络失败
# ---------------------------------------------------------------------------


def _intent_row(db, delivery_id) -> MaintenanceCycleSellIntent:
    db.expire_all()
    row = db.get(MaintenanceCycleSellIntent, delivery_id)
    assert row is not None
    return row


def _force_intent_due(db, delivery_id) -> None:
    db.execute(
        update(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.delivery_id == delivery_id)
        .values(available_at=datetime.now(timezone.utc) - timedelta(seconds=60))
    )
    db.commit()


def _close_to_shop(recycle, part) -> dict:
    unit = _intake(recycle, part)[0]
    ready = recycle.post(
        f"/api/cycle/units/{unit['id']}/self-pass",
        json={"expected_version": unit["version"]},
    )
    assert ready.status_code == 200, ready.text
    closed = recycle.post(
        f"/api/cycle/units/{unit['id']}/close",
        json={
            "expected_version": ready.json()["version"],
            "outcome": "to_shop",
            "condition_bucket": "good",
        },
    )
    assert closed.status_code == 200, closed.text
    return closed.json()


def test_mark_sold_fails_pending_intent_in_same_transaction(db):
    """F-04：人工售出必须终止未投递意图，worker 启动后不得再投递已售单位。"""
    part = _part(db, "CYCLE-FIX-F04A")
    db.commit()
    with _recycle(db, "fix-f04-recycle-a") as recycle, \
            _manager(db, "fix-f04-mgr-a") as manager:
        closed = _close_to_shop(recycle, part)
        pending = db.scalar(
            select(MaintenanceCycleSellIntent).where(
                MaintenanceCycleSellIntent.unit_id == closed["id"]
            )
        )
        assert pending is not None and pending.state == "pending"
        sold = manager.post(
            f"/api/cycle/units/{closed['id']}/mark-sold",
            json={"expected_version": closed["version"]},
        )
        assert sold.status_code == 200, sold.text
        row = _intent_row(db, pending.delivery_id)
        assert row.state == "failed"
        assert row.last_error_code == "unit_sold_manually"
        assert row.completed_at is not None
        assert row.lease_token is None and row.lease_until is None
        assert row.receipt_status is None and row.block_code is None
        # worker 关闭/启动后都不再领取该意图
        assert sell.claim_one() is None
        assert _unit(db, closed["id"]).outcome == "sold"


def test_mark_sold_conflicts_when_intent_delivering(db):
    """F-04：在途 delivering 返回 409（不抢在途 HTTP 的租约），单位保持 to_shop。"""
    part = _part(db, "CYCLE-FIX-F04B")
    db.commit()
    with _recycle(db, "fix-f04-recycle-b") as recycle, \
            _manager(db, "fix-f04-mgr-b") as manager:
        closed = _close_to_shop(recycle, part)
        intent = db.scalar(
            select(MaintenanceCycleSellIntent).where(
                MaintenanceCycleSellIntent.unit_id == closed["id"]
            )
        )
        db.execute(
            update(MaintenanceCycleSellIntent)
            .where(MaintenanceCycleSellIntent.delivery_id == intent.delivery_id)
            .values(
                state="delivering",
                lease_token=uuid4(),
                lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
            )
        )
        db.commit()
        blocked = manager.post(
            f"/api/cycle/units/{closed['id']}/mark-sold",
            json={"expected_version": closed["version"]},
        )
        assert blocked.status_code == 409, blocked.text
        stored_unit = _unit(db, closed["id"])
        assert stored_unit.outcome == "to_shop"
        assert stored_unit.version == closed["version"]
        assert _intent_row(db, intent.delivery_id).state == "delivering"


def test_receipt_update_only_note_after_unit_left_pool_allowed(db):
    """P2-C：前端回填 pn/part_id 但值未变时，仅改 note/件况不得 409。"""
    project = _project(db, "FIX-P2C")
    part = _part(db, "CYCLE-FIX-P2C")
    receipt = _receipt(db, project, part, "3", condition="坏品")
    db.commit()
    with _recycle(db, "fix-p2c-recycle") as recycle:
        created = recycle.post(
            "/api/cycle/units/handoff-from-receipt",
            json={
                "receipt_id": receipt.rkd_line_id,
                "qty": 1,
                "expected_receipt_version": receipt.version,
            },
        )
        assert created.status_code == 201, created.text
        unit = created.json()["items"][0]
        dispatched = recycle.post(
            f"/api/cycle/units/{unit['id']}/dispatch",
            json={"expected_version": unit["version"], "to": "testing"},
        )
        assert dispatched.status_code == 200, dispatched.text

    admin = _receipt_client(db, username="fix-p2c-admin")
    # pn/part_id 与现值相同（前端总回填）+ 仅改 note → 允许
    patched = admin.patch(
        f"/api/maintenance/return-receipts/{receipt.rkd_line_id}",
        json={
            "version": receipt.version,
            "reason": "仅改备注",
            "pn": part.pn_std,
            "part_id": part.id,
            "note": "只改备注不碰件数",
        },
    )
    assert patched.status_code == 200, patched.text
    db.expire_all()
    stored = db.get(type(receipt), receipt.rkd_line_id)
    assert stored.version == 2
    assert stored.note == "只改备注不碰件数"
    assert _unit(db, unit["id"]).station == "testing"
    # 真正改 qty（离开 pool 后）→ 409 零写入
    conflict = admin.patch(
        f"/api/maintenance/return-receipts/{receipt.rkd_line_id}",
        json={"version": 2, "reason": "改数量", "qty": 2},
    )
    assert conflict.status_code == 409, conflict.text
    db.expire_all()
    assert db.get(type(receipt), receipt.rkd_line_id).qty == Decimal("3.000")


def test_manager_without_pool_key_claim_is_403_consistent(db):
    """F-08：只有 page_cycle_manage、无对应 pool 键的主管 claim → 403（API/引擎口径一致）。"""
    part = _part(db, "CYCLE-FIX-F08")
    db.commit()
    with _recycle(db, "fix-f08-recycle") as recycle, \
            _manager(db, "fix-f08-mgr") as manager:
        unit = _intake(recycle, part)[0]
        dispatched = _dispatch(recycle, unit, "testing")
        assert dispatched.status_code == 200, dispatched.text
        item = _active_items(db, unit["id"])[0]
        response = manager.post(
            f"/api/cycle/units/{unit['id']}/claim",
            json={
                "expected_version": dispatched.json()["version"],
                "item_version": item.version,
            },
        )
        assert response.status_code == 403, response.text
        assert "无权领取" in response.json()["detail"]
        stored_unit = _unit(db, unit["id"])
        assert stored_unit.assignee_username is None
        assert stored_unit.version == dispatched.json()["version"]
        assert _active_items(db, unit["id"])[0].status == "pending"


def test_network_failure_after_blocked_clears_block_code_and_unchanged_recovers(db):
    """F-13：blocked 后网络失败回 pending 必须清 block_code，R9 不永久 invalid。"""
    part = _part(db, "CYCLE-FIX-F13")
    db.commit()
    with _recycle(db, "fix-f13-recycle") as recycle:
        closed = _close_to_shop(recycle, part)
    intent = db.scalar(
        select(MaintenanceCycleSellIntent).where(
            MaintenanceCycleSellIntent.unit_id == closed["id"]
        )
    )
    first = sell.claim_one()
    assert first is not None and first.delivery_id == intent.delivery_id
    assert sell.acknowledge(first, "blocked", block_code="missing_formal_image") is True
    blocked_row = _intent_row(db, intent.delivery_id)
    assert blocked_row.state == "blocked"

    _force_intent_due(db, intent.delivery_id)
    retry = sell.claim_one()
    assert retry is not None and retry.previous_blocked is True
    assert sell.acknowledge(retry, "transport_error") is True
    pending_row = _intent_row(db, intent.delivery_id)
    assert pending_row.state == "pending"
    assert pending_row.last_error_code == "transport_error"
    assert pending_row.block_code is None
    assert pending_row.receipt_status is None
    status = sell.read_status(db, closed["id"])
    assert status["status"] == "pending"
    assert status["blockCode"] is None

    _force_intent_due(db, intent.delivery_id)
    recovered = sell.claim_one()
    assert recovered is not None and recovered.previous_blocked is False
    assert sell.acknowledge(recovered, "unchanged") is True
    delivered_row = _intent_row(db, intent.delivery_id)
    assert delivered_row.state == "delivered"
    assert delivered_row.receipt_status == "unchanged"
