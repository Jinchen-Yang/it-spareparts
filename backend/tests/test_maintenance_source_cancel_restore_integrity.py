"""D-0022 第 1 轮返工：快照完整性/摘要绑定/坏回执隔离/幂等键作用域/库存往返。

对应验收必改项：
- 快照缺成员（领用行/需求行/单头 before 等）必须整次拒绝、零写入，不能半恢复；
- 摘要必须绑定完整恢复目标（含 before），预览后篡改快照不得再用旧摘要执行；
- 锁内必须重新读取确认回执，锁前复制出的 dict 不可复用（sol 报告 M3）；
- 坏格式回执返回明确 4xx，且单条坏回执不能拖垮整个恢复清单；
- 幂等键绑定来源单：同操作人同键用于不同来源单必须 409 零写入，
  每次成功恢复都要有自己的 restored 墓碑事件；
- already_restored 回执必须带 warnings（前端契约）；
- R24「及库存」：取消前/取消后/恢复后按真实读模型断言库存口径。
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandDeleteEvent,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_project_operations import MaintenanceSiteIssue as SiteIssue
from app.models.maintenance_project_operations import MaintenanceSiteIssueLine as SiteIssueLine
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch
from app.services import inventory, maintenance_front_stock as front
from app.services import maintenance_recovery as recovery
from app.services import maintenance_return_metrics as metrics

from tests.boss_board_helpers import assign, client_for, make_project
from tests.test_maintenance_source_cancel_cascade import setup, upload
from tests.test_maintenance_source_cancel_concurrency import _business_snapshot
from tests.test_maintenance_source_cancel_restore import (
    confirm,
    list_url,
    preview,
    restore,
    restore_audits,
    restore_body,
    restore_url,
)

SECOND_ORDER_ID = "RESTORE-R1-ORDER2"


def edit_receipt(db, batch, change):
    """直接改持久化回执（模拟库内快照被篡改/损坏）；JSONB 需整体换新对象才脏。"""
    stored = db.get(SysImportBatch, batch.id)
    report = deepcopy(stored.report_json)
    entry = next(iter(report["_source_cancel_confirmations"].values()))
    change(entry)
    stored.report_json = report
    db.commit()
    db.expire_all()


def confirm_second_order(db, tmp_path, client, project, url):
    """同项目再导入并确认取消另一张来源单（不同导入批次、不同确认回执）。"""
    from tests.wbdd_fixtures import make_rows

    rows = make_rows(orders=1, lines_per_order=2)
    for index, row in enumerate(rows):
        row["数据ID(不可修改)"] = SECOND_ORDER_ID
        row["需求单号"] = "WBDD-20260609-0002"
        row["需求明细.数据ID(不可修改)"] = f"{SECOND_ORDER_ID}-line{index}"
        row["销售订单"] = ""
    upload(db, tmp_path, rows)
    second = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == SECOND_ORDER_ID))
    assign(db, second, project)
    for row in rows:
        row["数据状态"] = "已取消"
    batch = upload(db, tmp_path, rows)
    detail_url = f"{url}/{batch.id}/{second.raw_order_id}"
    confirm(db, client, detail_url)
    return second, batch


def restored_events(db):
    return list(db.scalars(select(MaintenanceDemandDeleteEvent).where(
        MaintenanceDemandDeleteEvent.event_type == "restored")))


def _wait_blocked(db, owner_pid, future):
    """等到 future 的恢复请求真的在 PostgreSQL 上等 owner_pid 的锁。"""
    deadline = monotonic() + 15
    while monotonic() < deadline:
        waiting = db.scalars(text(
            "SELECT DISTINCT pid FROM pg_locks WHERE NOT granted "
            "AND :p = ANY(pg_blocking_pids(pid))"), {"p": owner_pid}).all()
        if waiting:
            return
        if future.done():
            pytest.fail(f"恢复请求未阻塞：{future.result().status_code} {future.result().text}")
        sleep(0.01)
    pytest.fail("15 秒内未观察到真实锁等待")


# ---- 必须改 1：快照缺成员 → 整次拒绝、零写入 --------------------------------


@pytest.mark.parametrize("tamper", [
    "empty_site_lines", "empty_demand_lines", "drop_site_issues",
    "drop_site_lines", "drop_demand_lines", "drop_issue_before",
])
def test_incomplete_snapshot_refuses_whole_restore_zero_write(db, tmp_path, tamper):
    client, project, _rows, parent, lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)

    def change(entry):
        restoration = entry["restoration"]
        if tamper == "empty_site_lines":
            restoration["site_lines"] = []
        elif tamper == "empty_demand_lines":
            restoration["demand_lines"] = []
        elif tamper == "drop_site_issues":
            restoration.pop("site_issues")
        elif tamper == "drop_site_lines":
            restoration.pop("site_lines")
        elif tamper == "drop_demand_lines":
            restoration.pop("demand_lines")
        else:
            restoration["site_issues"][0].pop("before")

    edit_receipt(db, batch, change)
    expected = _business_snapshot(db)

    stale = client.get(restore_url(project, parent.raw_order_id))
    assert stale.status_code == 409, stale.text
    payload = stale.json()["detail"]
    assert payload["code"] == "source_cancellation_restore_unavailable"
    assert any("快照" in item["reason"] for item in payload["conflicts"])

    response = client.post(restore_url(project, parent.raw_order_id) + "/restore",
                           json={"expected_digest": "f" * 64, "reason": "想恢复",
                                 "idempotency_key": str(uuid4())})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert _business_snapshot(db) == expected
    assert all(not line.is_active for line in lines)
    assert not exact.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert restore_audits(db) == []
    assert restored_events(db) == []
    # 清单里该单条标「不可恢复」，其余照常
    items = client.get(list_url(project)).json()["items"]
    assert items[0]["state"] == "unavailable"
    assert any("快照" in item["reason"] for item in items[0]["conflicts"])


# ---- 必须改 2：摘要绑定完整恢复目标（含 before） ------------------------------


def test_tampered_before_after_preview_invalidates_digest_zero_write(db, tmp_path):
    client, project, _rows, parent, lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    body = restore_body(preview(client, project, parent))  # 合法预览摘要
    edit_receipt(db, batch, lambda entry:
                 entry["restoration"]["site_issues"][0]["before"].__setitem__(
                     "normalized_status", "draft"))
    expected = _business_snapshot(db)

    response = restore(db, client, project, parent, body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_cancellation_restore_conflict"
    db.expire_all()
    assert _business_snapshot(db) == expected
    # 不落成篡改值 draft，仍停在作废态；整体零写入
    issue = db.get(SiteIssue, exact.issue_id)
    assert issue.normalized_status == "void" and issue.voided_at is not None
    assert all(not line.is_active for line in lines) and not exact.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert restore_audits(db) == []


def test_receipt_mutated_while_restore_waits_for_locks_is_rejected_zero_write(db, tmp_path):
    """锁前读到的回执不可复用：等行锁期间快照被改，恢复必须按新内容拒绝。"""
    from app.db import SessionLocal

    client, project, _rows, parent, lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    body = restore_body(preview(client, project, parent))
    endpoint = restore_url(project, parent.raw_order_id) + "/restore"
    db.commit()
    with SessionLocal() as changing:
        # 另一真实 PG 会话先锁住确认批次行：恢复会在这里排队等行锁
        changing.scalar(select(SysImportBatch).where(
            SysImportBatch.id == batch.id).with_for_update())
        owner_pid = changing.scalar(text("SELECT pg_backend_pid()"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, endpoint, json=body)
            try:
                _wait_blocked(db, owner_pid, future)
                held = changing.get(SysImportBatch, batch.id)
                report = deepcopy(held.report_json)
                next(iter(report["_source_cancel_confirmations"].values())
                     )["restoration"]["schema"] = 999
                held.report_json = report
                changing.flush()
                expected = _business_snapshot(changing)
                changing.commit()
                response = future.result(timeout=30)
            finally:
                changing.rollback()
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_cancellation_restore_unavailable"
    db.expire_all()
    assert _business_snapshot(db) == expected
    assert all(not line.is_active for line in lines)
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert restore_audits(db) == []
    assert restored_events(db) == []


# ---- 必须改 3：坏格式回执明确 4xx，不拖垮清单 ---------------------------------


@pytest.mark.parametrize("tamper", [
    "non_object", "missing_before", "missing_site_lines_key",
    "bad_before_value", "bad_site_line_issue_id", "bad_assignment_id",
])
def test_malformed_snapshot_returns_4xx_and_list_still_works(db, tmp_path, tamper):
    client, project, _rows, parent, lines, _exact, _unknown, _receipt, batch, url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    second, _second_batch = confirm_second_order(db, tmp_path, client, project, url)

    def change(entry):
        if tamper == "non_object":
            entry["restoration"] = ["not-a-snapshot"]
        elif tamper == "missing_before":
            entry["restoration"]["site_issues"][0].pop("before")
        elif tamper == "missing_site_lines_key":
            entry["restoration"].pop("site_lines")
        elif tamper == "bad_before_value":
            entry["restoration"]["site_issues"][0]["before"]["voided_at"] = "not-a-date"
        elif tamper == "bad_site_line_issue_id":
            entry["restoration"]["site_lines"][0]["issue_id"] = ["unhashable"]
        else:
            entry["restoration"]["assignments"][0]["assignment_id"] = {"unhashable": True}

    edit_receipt(db, batch, change)
    expected = _business_snapshot(db)

    stale = client.get(restore_url(project, parent.raw_order_id))
    assert stale.status_code == 409, stale.text  # 明确 4xx，不是 500
    assert stale.json()["detail"]["code"] == "source_cancellation_restore_unavailable"
    response = client.post(restore_url(project, parent.raw_order_id) + "/restore",
                           json={"expected_digest": "f" * 64, "reason": "想恢复",
                                 "idempotency_key": str(uuid4())})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert _business_snapshot(db) == expected
    assert all(not line.is_active for line in lines)

    # 一条坏回执不能拖垮整个清单：坏单标不可恢复，其余照常列出
    items = client.get(list_url(project)).json()["items"]
    by_order = {item["source_order_id"]: item for item in items}
    assert by_order[parent.raw_order_id]["state"] == "unavailable"
    assert any("快照" in item["reason"] for item in by_order[parent.raw_order_id]["conflicts"])
    assert by_order[second.raw_order_id]["state"] == "restorable"


# ---- 必须改 4：幂等键绑定来源单；每次成功恢复都有自己的墓碑事件 ----------------


def test_idempotency_key_is_bound_to_source_order_across_restores(db, tmp_path):
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, _batch, url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    second, _second_batch = confirm_second_order(db, tmp_path, client, project, url)
    key = str(uuid4())
    first = restore(db, client, project, parent,
                    restore_body(preview(client, project, parent), key=key))
    assert first.status_code == 200, first.text
    db.expire_all()
    expected = _business_snapshot(db)

    # 同操作人同键用于另一张来源单：必须 409 且零写入（不能借旧键再恢复一次）
    response = restore(db, client, project, second,
                       restore_body(preview(client, project, second), key=key))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_cancellation_restore_conflict"
    assert "幂等键" in response.json()["detail"]["message"]
    db.expire_all()
    assert _business_snapshot(db) == expected
    # 只有第一单有一次恢复事件，第二单的恢复事件不可能被静默吞掉后再次写入
    assert [event.source_order_id for event in restored_events(db)] == [parent.raw_order_id]


def test_each_successful_restore_writes_its_own_restored_event(db, tmp_path):
    client, project, _rows, parent, lines, _exact, _unknown, _receipt, _batch, url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    second, _second_batch = confirm_second_order(db, tmp_path, client, project, url)
    assert restore(db, client, project, parent,
                   restore_body(preview(client, project, parent), key="r1-event-key")).status_code == 200
    # 旧实现允许同键跨单再恢复一次，且第二个 restored 事件被同键静默吞掉：
    # 「每次成功恢复必有完整墓碑事件」要成立，同键跨单必须先被拒绝
    rejected = restore(db, client, project, second,
                       restore_body(preview(client, project, second), key="r1-event-key"))
    assert rejected.status_code == 409, rejected.text
    assert restore(db, client, project, second,
                   restore_body(preview(client, project, second))).status_code == 200
    db.expire_all()
    events = sorted(restored_events(db), key=lambda row: row.source_order_id)
    assert [event.source_order_id for event in events] == sorted(
        [parent.raw_order_id, SECOND_ORDER_ID])
    for event in events:
        assert event.event_type == "restored" and event.operated_by == "cancel-admin"
        assert event.reason  # 恢复原因完整保留
        payload = event.payload_json
        assert payload["status"] == "restored"
        assert payload["source_order_id"] == event.source_order_id
        assert payload["restored_lines"] == len(lines)
        assert payload["restored_at"]
    # 事件键必须与回执来源单同作用域：两单各自一条、互不覆盖
    assert len({event.idempotency_key for event in events}) == 2
    for event in events:
        assert event.idempotency_key.startswith(f"cancel-restore:{event.source_order_id}:")
    assert all(db.get(MaintenanceDemandTombstone, order_id).restored_at is not None
               for order_id in (parent.raw_order_id, SECOND_ORDER_ID))


# ---- 必须改 5：already_restored 回执的前端契约（warnings 必须存在） ------------


def test_already_restored_response_carries_warnings_for_the_frontend(db, tmp_path):
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, _batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    first = restore(db, client, project, parent,
                    restore_body(preview(client, project, parent)))
    assert first.status_code == 200, first.text
    again = restore(db, client, project, parent,
                    restore_body(preview(client, project, parent)))
    assert again.status_code == 200, again.text
    payload = again.json()
    assert payload["status"] == "already_restored" and payload["replayed"] is False
    # 前端直接渲染 warnings 列表：两类真实回执的字段契约必须统一且内容一致
    assert isinstance(payload.get("warnings"), list)
    assert payload["warnings"] == first.json()["warnings"]
    assert payload["warnings"]  # setup 的未精确关联领用会产生确认警告，不能被吞
    assert payload["restored_via"] == "cancel_restore"
    assert payload["restored_by"] == "cancel-admin"
    assert payload["restored_at"]


# ---- 必须改 6：R24「及库存」——真实读模型三点断言 ------------------------------


@pytest.mark.parametrize("active_only", [True, False])
def test_cancel_restore_roundtrip_keeps_inventory_read_models_identical(
        db, tmp_path, monkeypatch, active_only):
    from app import config

    monkeypatch.setattr(config, "ACTIVE_STATUS_ONLY", active_only)
    client, project, _rows, parent, lines, exact, unknown, receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    part = db.get(DimPart, lines[0].part_id)
    # 领用日期错开：取消后「最近领用日期」应回退到未作废的那张领用单
    db.get(SiteIssue, unknown.issue_id).issue_date = date(2026, 9, 1)
    # 大库：期初 100 + 采购入 20 − 销售出 10 − 维保需求净量（同型号仅 1 行 ×（3−0），
    # 第二行 PN 不同，被 dynamic_stock_map 的 part 过滤排除）
    db.add(Inventory(raw_inventory_id="r1-stock", part_id=part.id, pn_std=part.pn_std,
                     warehouse="大库-合成", source_qty=100, snapshot_date=date(2026, 6, 1),
                     unit_cost=17, inventory_value=1700))
    po = FPurchaseOrder(raw_order_id="r1-po", order_no="CG-R1", order_date=date(2026, 6, 3),
                        data_status="已生效", import_batch_id=batch.id)
    so = FSalesOrder(raw_order_id="r1-so", order_no="XS-R1", order_date=date(2026, 6, 4),
                     data_status="已生效", import_batch_id=batch.id)
    db.add_all([po, so])
    db.flush()
    db.add(FPurchaseLine(raw_line_id="r1-pl", order_id=po.id, part_id=part.id,
                         qty=20, import_batch_id=batch.id))
    db.add(FSalesLine(raw_line_id="r1-sl", order_id=so.id, part_id=part.id,
                      qty=10, import_batch_id=batch.id))
    # 前置库：发入 20、好件返库出 3（独立账本，不随取消/恢复变化）
    for kind, source_type, qty in [("shipment_in", "ckd_shipment_line", 20),
                                   ("return_out", "return_order_line", 3)]:
        front.apply_movement(db, project_id=project.project_id, part_id=part.id,
                             kind=kind, source_type=source_type, source_ref=f"r1-{kind}",
                             qty=qty, unit_cost_ex_tax=17, unit_cost_inc_tax=19.21,
                             occurred_at=datetime(2026, 6, 5, tzinfo=UTC), operated_by="r1")
    db.commit()

    def stock_snapshot():
        db.expire_all()
        return {
            "dynamic": inventory.dynamic_stock_map(db, [part.id])[part.id],
            "warehouse": inventory.list_inventory(db, None, part.pn_std, 1, 20),
            "front_balance": front.balance_rows(db, project.project_id),
            "front_ledger": front.ledger_entries(db, project.project_id),
            "recovery": recovery.recovery_summary(db, project.project_id),
            "usage": metrics.project_totals(db, project_ids=[project.project_id]),
        }

    before = stock_snapshot()
    # 基线自检：合成事实确实进入各读模型，避免空断言
    assert before["dynamic"]["in_qty"] == 20 and before["dynamic"]["out_sales"] == 10
    assert before["front_balance"][0]["qty"] == 17
    assert before["recovery"]["good_returned_total_qty"] == 3
    assert before["usage"][project.project_id]["issued_qty"] == 2 + 9
    assert before["usage"][project.project_id]["returned_qty"] == 8
    maint_before = before["dynamic"]["out_maint"]
    # 默认口径只计已生效来源单（此时来源单已是「已取消」，不计）；放开口径后同型号行计入
    assert maint_before == (0 if active_only else 3)
    assert before["front_balance"][0]["last_consumed_at"].startswith("2026-09-10")

    confirm(db, client, detail_url)
    cancelled = stock_snapshot()
    # 取消后按预期变化：维保净出归零 → 动态库存回升；领用统计掉精确行；前置库账本不动
    assert cancelled["dynamic"]["out_maint"] == 0
    assert cancelled["dynamic"]["dynamic_qty"] == before["dynamic"]["dynamic_qty"] + maint_before
    assert cancelled["front_balance"][0]["qty"] == 17
    assert cancelled["front_balance"][0]["last_consumed_at"].startswith("2026-09-01")
    assert cancelled["usage"][project.project_id]["issued_qty"] == 9
    assert cancelled["usage"][project.project_id]["returned_qty"] == 8
    assert cancelled["recovery"]["good_returned_total_qty"] == 3

    response = restore(db, client, project, parent,
                       restore_body(preview(client, project, parent)))
    assert response.status_code == 200, response.text
    # 恢复后与取消前完全一致（动态库存/前置库/领用/返还全部口径）
    assert stock_snapshot() == before


# ---- 对抗评审加固：快照成员取值/归属/代际不能只靠键存在 ----------------------


@pytest.mark.parametrize("tamper", [
    "demand_before_inactive", "demand_before_void_mark", "demand_after_active",
    "demand_after_time", "site_line_before_inactive", "site_line_after_active",
    "site_line_after_issue", "site_issue_before_void", "site_issue_after_draft",
    "site_issue_after_time",
])
def test_tampered_before_values_are_rejected_before_preview(db, tmp_path, tamper):
    """预览前就伪造 before/after 取值：新预览摘要也不得为它背书（sol M1 变体）。"""
    client, project, _rows, parent, lines, exact, unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)

    def change(entry):
        restoration = entry["restoration"]
        if tamper == "demand_before_inactive":
            restoration["demand_lines"][0]["before"]["is_active"] = False
        elif tamper == "demand_before_void_mark":
            restoration["demand_lines"][0]["before"]["voided_by"] = "someone-else"
        elif tamper == "demand_after_active":
            restoration["demand_lines"][0]["after"]["is_active"] = True
        elif tamper == "demand_after_time":
            restoration["demand_lines"][0]["after"]["voided_at"] = "2020-01-01T00:00:00+00:00"
        elif tamper == "site_line_before_inactive":
            restoration["site_lines"][0]["before"]["is_active"] = False
        elif tamper == "site_line_after_active":
            restoration["site_lines"][0]["after"]["is_active"] = True
        elif tamper == "site_line_after_issue":
            restoration["site_lines"][0]["after"]["issue_id"] = unknown.issue_id
        elif tamper == "site_issue_before_void":
            restoration["site_issues"][0]["before"]["normalized_status"] = "void"
        elif tamper == "site_issue_after_draft":
            restoration["site_issues"][0]["after"]["normalized_status"] = "draft"
        else:
            restoration["site_issues"][0]["after"]["voided_at"] = "2020-01-01T00:00:00+00:00"

    edit_receipt(db, batch, change)
    expected = _business_snapshot(db)

    stale = client.get(restore_url(project, parent.raw_order_id))
    assert stale.status_code == 409, stale.text
    payload = stale.json()["detail"]
    assert payload["code"] == "source_cancellation_restore_unavailable"
    assert any("快照" in item["reason"] for item in payload["conflicts"])
    response = client.post(restore_url(project, parent.raw_order_id) + "/restore",
                           json={"expected_digest": "f" * 64, "reason": "想恢复",
                                 "idempotency_key": str(uuid4())})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert _business_snapshot(db) == expected
    assert all(not line.is_active for line in lines) and not exact.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert restore_audits(db) == [] and restored_events(db) == []
    assert client.get(list_url(project)).json()["items"][0]["state"] == "unavailable"


def test_snapshot_site_line_substitution_with_same_counts_is_rejected(db, tmp_path):
    """条数不变但把快照成员换成无关领用行：不得按「成功」恢复。"""
    from app.services.maintenance_wbdd_reconciliation import _entity

    client, project, _rows, parent, _lines, exact, unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    db.expire_all()
    unknown_line = db.get(SiteIssueLine, unknown.issue_line_id)
    unknown_entity = _entity(unknown_line)
    assert unknown_entity["issue_id"] != exact.issue_id and unknown_line.is_active

    def change(entry):
        restoration = entry["restoration"]
        # 顶层 issue_id 仍指向快照内的领用单头，但成员本体换成另一张活动领用行
        restoration["site_lines"][0] = {
            "issue_line_id": unknown.issue_line_id,
            "issue_id": exact.issue_id,
            "before": {**unknown_entity, "is_active": True},
            "after": unknown_entity,
        }

    edit_receipt(db, batch, change)
    expected = _business_snapshot(db)
    stale = client.get(restore_url(project, parent.raw_order_id))
    assert stale.status_code == 409, stale.text
    response = client.post(restore_url(project, parent.raw_order_id) + "/restore",
                           json={"expected_digest": "f" * 64, "reason": "想恢复",
                                 "idempotency_key": str(uuid4())})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert _business_snapshot(db) == expected
    # 真正被确认作废的领用行仍是作废态，无关活动行未被冒名恢复
    assert not db.get(SiteIssueLine, exact.issue_line_id).is_active
    assert db.get(SiteIssueLine, unknown.issue_line_id).is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert restore_audits(db) == [] and restored_events(db) == []


def test_snapshot_demand_line_substitution_with_same_counts_is_rejected(db, tmp_path):
    """换成同单另一条历史作废需求行（条数不变）：before 取值必须对得上确认代次。"""
    from app.services.maintenance_wbdd_reconciliation import _entity

    client, project, _rows, parent, lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    extra = FMaintenanceLine(
        raw_line_id="RESTORE-R1-EXTRA-VOIDED", order_id=parent.id,
        part_id=lines[0].part_id, qty=1, is_active=False,
        import_batch_id=batch.id,
        voided_at=datetime(2026, 1, 1, tzinfo=UTC), voided_by="old-admin",
        void_reason="历史作废")
    db.add(extra)
    db.commit()
    db.expire_all()
    extra_entity = _entity(db.get(FMaintenanceLine, extra.id))
    assert extra_entity["is_active"] is False

    def change(entry):
        restoration = entry["restoration"]
        restoration["demand_lines"][0] = {
            "id": extra.id, "raw_line_id": extra.raw_line_id,
            "before": {**extra_entity, "is_active": True, "voided_at": None,
                       "voided_by": None, "void_reason": None},
            "after": extra_entity,
        }

    edit_receipt(db, batch, change)
    expected = _business_snapshot(db)
    stale = client.get(restore_url(project, parent.raw_order_id))
    assert stale.status_code == 409, stale.text
    response = client.post(restore_url(project, parent.raw_order_id) + "/restore",
                           json={"expected_digest": "f" * 64, "reason": "想恢复",
                                 "idempotency_key": str(uuid4())})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert _business_snapshot(db) == expected
    assert all(not line.is_active for line in lines)
    assert not exact.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert restore_audits(db) == [] and restored_events(db) == []


def test_stale_replay_after_revoke_is_not_reported_as_restored(db, tmp_path):
    """恢复→再次作废换代后，同键旧请求重放不能谎报「当前已恢复」。"""
    client, project, rows, parent, lines, _exact, _unknown, _receipt, _batch, url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    original_body = restore_body(preview(client, project, parent), key="r1-stale-replay")
    first = restore(db, client, project, parent, original_body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "restored"

    # 来源单再次出现：重新导入（已取消）+ 重新挂靠 → 允许第二代人工确认作废
    for row in rows:
        row["数据状态"] = "已取消"
    batch = upload(db, tmp_path, rows)
    assign(db, parent, project)
    confirm(db, client, f"{url}/{batch.id}/{parent.raw_order_id}")
    db.expire_all()
    tombstone = db.get(MaintenanceDemandTombstone, parent.raw_order_id)
    assert tombstone.restored_at is None  # 已被再次作废换代
    current_version = tombstone.version
    audits_before = len(restore_audits(db))
    events_before = len(restored_events(db))

    retry = restore(db, client, project, parent, original_body)
    assert retry.status_code == 409, retry.text
    payload = retry.json()["detail"]
    assert payload["code"] == "source_cancellation_restore_conflict"
    assert "恢复" in payload["message"]
    db.expire_all()
    tombstone = db.get(MaintenanceDemandTombstone, parent.raw_order_id)
    assert tombstone.version == current_version and tombstone.restored_at is None
    assert all(not line.is_active for line in lines)
    assert len(restore_audits(db)) == audits_before
    assert len(restored_events(db)) == events_before


def test_zero_member_confirmation_restore_is_reported_as_restored(db, tmp_path):
    """单头单（无需求行/领用行）：恢复成功后必须报 restored，不能落成 conflict。"""
    from tests.wbdd_fixtures import make_rows

    client = client_for(db, username="cancel-admin", role="admin")
    project = make_project(db, "CANCEL-EMPTY-PROJECT")
    rows = make_rows(orders=0, headless=1)
    upload(db, tmp_path, rows)
    parent = db.scalar(select(FMaintenanceOrder))
    assert db.scalar(select(func.count()).select_from(FMaintenanceLine).where(
        FMaintenanceLine.order_id == parent.id)) == 0
    assign(db, parent, project)
    rows[0]["数据状态"] = "已取消"
    batch = upload(db, tmp_path, rows)
    url = f"/api/maintenance/projects/{project.project_id}/source-cancellations"
    confirm(db, client, f"{url}/{batch.id}/{parent.raw_order_id}")

    first_preview = preview(client, project, parent)
    assert first_preview["state"] == "restorable"
    assert first_preview["voided_lines"] == 0
    first = restore(db, client, project, parent, restore_body(first_preview))
    assert first.status_code == 200, first.text
    assert first.json()["restored_lines"] == 0

    after = preview(client, project, parent)
    assert after["state"] == "restored" and after["restored"] is True
    assert after["restore_receipt"]["result"]["status"] == "restored"
    again = restore(db, client, project, parent, restore_body(after))
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "already_restored"
