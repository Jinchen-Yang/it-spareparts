"""返还收货 × 坏件循环互锁（D-0057 §3.3 / §6.5 / T2 A-24）。

约束回顾：
- 数量：未撤回单位数 ≤ 收货数量；报废/去电商/回公司/售出仍占用件数。
- 只要存在任一未撤回单位工位不是 pool，或已有可售意图：改 qty/pn/part_id 或作废 → 409，
  收货行与单位零写入。
- 全部未撤回单位仍在 pool 且无意向时，作废必须在同一事务撤回这些单位。
- 只认 source_receipt_id（rkd_line_id），同 PN 的另一张收货行及其单位不受影响。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.models.maintenance_cycle import MaintenanceCycleEvent, MaintenanceCycleUnit
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt
from tests.test_maintenance_return_receipts_api import _client as _receipt_client


def _cycle(db, username: str, **overrides):
    return client_for(db, username=username, role="readonly", overrides=overrides or {})


def _recycle(db, username: str):
    return _cycle(db, username, page_cycle_recycle=True)


def _manager(db, username: str):
    return _cycle(db, username, page_cycle_manage=True)


def _handoff(client, receipt, qty: int, **extra):
    return client.post(
        "/api/cycle/units/handoff-from-receipt",
        json={
            "receipt_id": receipt.rkd_line_id,
            "qty": qty,
            "expected_receipt_version": receipt.version,
            **extra,
        },
    )


def _dispatch(client, unit: dict, to: str, **extra):
    return client.post(
        f"/api/cycle/units/{unit['id']}/dispatch",
        json={"expected_version": unit["version"], "to": to, **extra},
    )


def _patch_receipt(client, receipt_id: str, version: int, **changes):
    return client.patch(
        f"/api/maintenance/return-receipts/{receipt_id}",
        json={"version": version, "reason": "合成互锁调整", **changes},
    )


def _void_receipt(client, receipt_id: str, version: int):
    return client.post(
        f"/api/maintenance/return-receipts/{receipt_id}/void",
        json={"version": version, "reason": "合成互锁作废"},
    )


def _receipt_row(db, receipt_id: str) -> MaintenanceRkdReturnLine:
    db.expire_all()
    row = db.get(MaintenanceRkdReturnLine, receipt_id)
    assert row is not None
    return row


def _units(db, receipt_id: str) -> list[MaintenanceCycleUnit]:
    db.expire_all()
    return list(
        db.scalars(
            select(MaintenanceCycleUnit)
            .where(MaintenanceCycleUnit.source_receipt_id == receipt_id)
            .order_by(MaintenanceCycleUnit.id)
        ).all()
    )


def _setup(db, tag: str, qty: str = "3", condition: str = "坏品"):
    project = _project(db, f"INT-{tag}")
    part = _part(db, f"CYCLE-INT-{tag}")
    receipt = _receipt(db, project, part, qty, condition=condition)
    db.commit()
    return project, part, receipt


# ---------------------------------------------------------------------------
# 数量规则
# ---------------------------------------------------------------------------


def test_receipt_qty_editable_while_units_all_in_pool_but_not_below_consumed(db):
    project, part, receipt = _setup(db, "QTY", qty="5")
    recycle = _recycle(db, "int-recycle-qty")
    admin = _receipt_client(db, username="int-receipt-admin-qty")
    created = _handoff(recycle, receipt, 3)
    assert created.status_code == 201, created.text

    raised = _patch_receipt(admin, receipt.rkd_line_id, receipt.version, qty=4)
    assert raised.status_code == 200, raised.text
    assert raised.json()["qty"] == "4.000"
    row = _receipt_row(db, receipt.rkd_line_id)
    assert row.version == 2

    below = _patch_receipt(admin, receipt.rkd_line_id, row.version, qty=2)
    assert below.status_code == 409, below.text
    row = _receipt_row(db, receipt.rkd_line_id)
    assert row.qty == Decimal("4.000")
    assert row.version == 2


def test_receipt_qty_increase_while_pool_is_allowed(db):
    project, part, receipt = _setup(db, "INC", qty="1")
    recycle = _recycle(db, "int-recycle-inc")
    admin = _receipt_client(db, username="int-receipt-admin-inc")
    assert _handoff(recycle, receipt, 1).status_code == 201
    increased = _patch_receipt(admin, receipt.rkd_line_id, receipt.version, qty=3)
    assert increased.status_code == 200, increased.text
    # 容量随后可用：还能再入池 2 件
    more = _handoff(recycle, _receipt_row(db, receipt.rkd_line_id), 2)
    assert more.status_code == 201, more.text


# ---------------------------------------------------------------------------
# 离开 pool / 意向 → 409 零写入
# ---------------------------------------------------------------------------


def test_identity_change_blocked_after_unit_leaves_pool(db):
    project, part, receipt = _setup(db, "OUT", qty="3")
    other_part = _part(db, "CYCLE-INT-OTHER")
    db.commit()
    recycle = _recycle(db, "int-recycle-out")
    admin = _receipt_client(db, username="int-receipt-admin-out")
    unit = _handoff(recycle, receipt, 1).json()["items"][0]
    dispatched = _dispatch(recycle, unit, "testing")
    assert dispatched.status_code == 200, dispatched.text

    for changes in (
        {"qty": 2},
        {"pn": "CYCLE-INT-RENAMED"},
        # 改备件身份必须同时给合法 PN，否则先被既有校验以 422 拒绝；
        # 这里用合法组合验证互锁的 409 分支。
        {"pn": other_part.pn_std, "part_id": other_part.id},
    ):
        response = _patch_receipt(admin, receipt.rkd_line_id, receipt.version, **changes)
        assert response.status_code == 409, (changes, response.text)

    row = _receipt_row(db, receipt.rkd_line_id)
    assert row.version == 1
    assert row.qty == Decimal("3.000")
    unit_row = _units(db, receipt.rkd_line_id)[0]
    assert unit_row.station == "testing"
    assert unit_row.version == 2


def test_void_blocked_when_any_unit_left_pool(db):
    project, part, receipt = _setup(db, "BUSY", qty="2")
    recycle = _recycle(db, "int-recycle-busy")
    admin = _receipt_client(db, username="int-receipt-admin-busy")
    unit = _handoff(recycle, receipt, 1).json()["items"][0]
    assert _dispatch(recycle, unit, "repairing").status_code == 200

    blocked = _void_receipt(admin, receipt.rkd_line_id, receipt.version)
    assert blocked.status_code == 409, blocked.text
    row = _receipt_row(db, receipt.rkd_line_id)
    assert row.line_status == "active"
    assert row.version == 1
    assert _units(db, receipt.rkd_line_id)[0].station == "repairing"


def test_void_blocked_when_sell_intent_exists_even_from_closed_unit(db):
    project, part, receipt = _setup(db, "INTENT", qty="1")
    recycle = _recycle(db, "int-recycle-intent")
    admin = _receipt_client(db, username="int-receipt-admin-intent")
    unit = _handoff(recycle, receipt, 1).json()["items"][0]
    closed = recycle.post(
        f"/api/cycle/units/{unit['id']}/close",
        json={"expected_version": unit["version"], "outcome": "to_shop",
              "condition_bucket": "good"},
    )
    assert closed.status_code == 200, closed.text
    blocked = _void_receipt(admin, receipt.rkd_line_id, receipt.version)
    assert blocked.status_code == 409, blocked.text
    assert _receipt_row(db, receipt.rkd_line_id).line_status == "active"


# ---------------------------------------------------------------------------
# 作废连带撤回（同一事务）
# ---------------------------------------------------------------------------


def test_void_withdraws_all_pool_units_in_same_transaction(db):
    project, part, receipt = _setup(db, "VOID", qty="2")
    recycle = _recycle(db, "int-recycle-void")
    admin = _receipt_client(db, username="int-receipt-admin-void")
    created = _handoff(recycle, receipt, 2)
    assert created.status_code == 201, created.text
    unit_ids = [item["id"] for item in created.json()["items"]]

    voided = _void_receipt(admin, receipt.rkd_line_id, receipt.version)
    assert voided.status_code == 200, voided.text
    row = _receipt_row(db, receipt.rkd_line_id)
    assert row.line_status == "voided"
    assert row.version == 2

    units = _units(db, receipt.rkd_line_id)
    assert len(units) == 2
    for unit in units:
        assert unit.withdrawn_at is not None
        assert unit.station == "pool"
        assert unit.outcome is None
        assert unit.version == 2
        assert unit.id in unit_ids
    db.expire_all()
    events = list(
        db.scalars(
            select(MaintenanceCycleEvent).where(
                MaintenanceCycleEvent.unit_id.in_(unit_ids),
                MaintenanceCycleEvent.event_type == "withdraw",
            )
        ).all()
    )
    assert len(events) == 2
    assert all(event.payload.get("reason") == "receipt_voided" for event in events)

    # 已作废收货行不能再入池
    again = _handoff(recycle, row, 1)
    assert again.status_code == 409, again.text


def test_void_allowed_when_only_withdrawn_units_remain(db):
    project, part, receipt = _setup(db, "WDRW", qty="1")
    recycle = _recycle(db, "int-recycle-wdrw")
    admin = _receipt_client(db, username="int-receipt-admin-wdrw")
    unit = _handoff(recycle, receipt, 1).json()["items"][0]
    assert recycle.post(
        f"/api/cycle/units/{unit['id']}/withdraw",
        json={"expected_version": unit["version"]},
    ).status_code == 200
    voided = _void_receipt(admin, receipt.rkd_line_id, receipt.version)
    assert voided.status_code == 200, voided.text
    assert _receipt_row(db, receipt.rkd_line_id).line_status == "voided"
    assert _units(db, receipt.rkd_line_id)[0].withdrawn_at is not None


# ---------------------------------------------------------------------------
# 同 PN 另一张收货行不受影响
# ---------------------------------------------------------------------------


def test_same_pn_other_receipt_unaffected(db):
    project = _project(db, "INT-SAME")
    part = _part(db, "CYCLE-INT-SAME")
    first = _receipt(db, project, part, "1", condition="坏品")
    second = _receipt(db, project, part, "1", condition="坏品")
    db.commit()
    recycle = _recycle(db, "int-recycle-same")
    admin = _receipt_client(db, username="int-receipt-admin-same")

    unit = _handoff(recycle, first, 1).json()["items"][0]
    assert _dispatch(recycle, unit, "testing").status_code == 200

    # 第二张收货行没有循环单位：可正常改数量、可作废
    patched = _patch_receipt(admin, second.rkd_line_id, second.version, qty=2)
    assert patched.status_code == 200, patched.text
    second_row = _receipt_row(db, second.rkd_line_id)
    voided = _void_receipt(admin, second.rkd_line_id, second_row.version)
    assert voided.status_code == 200, voided.text

    # 第一张行保持原状：未被连带作废/改数，单位工位不变
    first_row = _receipt_row(db, first.rkd_line_id)
    assert first_row.line_status == "active"
    assert first_row.qty == Decimal("1.000")
    assert first_row.version == 1
    assert _units(db, first.rkd_line_id)[0].station == "testing"
    assert _units(db, second.rkd_line_id) == []
    blocked = _void_receipt(admin, first.rkd_line_id, first_row.version)
    assert blocked.status_code == 409, blocked.text


# ---------------------------------------------------------------------------
# 件况只影响以后快照
# ---------------------------------------------------------------------------


def test_condition_change_never_rewrites_in_cycle_intake_snapshot(db):
    project, part, receipt = _setup(db, "COND", qty="1", condition="成品")
    recycle = _recycle(db, "int-recycle-cond")
    admin = _receipt_client(db, username="int-receipt-admin-cond")
    unit = _handoff(recycle, receipt, 1).json()["items"][0]
    assert unit["intake_condition"] == "成品"
    # 入池后离开 pool，改件况仍允许（不改身份/数量），但不回写单位快照
    assert _dispatch(recycle, unit, "testing").status_code == 200
    changed = _patch_receipt(admin, receipt.rkd_line_id, receipt.version, condition="坏品")
    assert changed.status_code == 200, changed.text
    unit_row = _units(db, receipt.rkd_line_id)[0]
    assert unit_row.intake_condition == "成品"
    assert _receipt_row(db, receipt.rkd_line_id).test_result == "坏品"
