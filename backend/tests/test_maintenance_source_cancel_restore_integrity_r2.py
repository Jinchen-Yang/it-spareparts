"""PR #440 第 2 轮返工：畸形快照/非法状态值/历史作废行替换 全部整次拒绝。

对应验收必改项（sol M1/M2 + astra B1/B2）：
- 畸形快照（缺字段/类型错误/多余字段，含缺 after.voided_at）必须返回可读 4xx，
  不能在预览/清单/执行任何入口抛 500；清单里该单标「快照损坏，不可恢复」，
  其余单照常；
- before 的 normalized_status/raw_status/status_mapping_state/status_mapping_version
  必须满足数据库 CHECK 约束，非法值在预览阶段就 4xx，不能等 flush 报 500；
- 快照成员必须能被「这一次确认作废」的审计证明：历史作废行替换、删成员并
  同步改数量、伪造 before 内容，一律整次拒绝、零写入。
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder, MaintenanceDemandTombstone
from app.services.maintenance_wbdd_reconciliation import _entity
from tests.test_maintenance_return_metrics import _issue
from tests.test_maintenance_source_cancel_cascade import setup
from tests.test_maintenance_source_cancel_concurrency import _business_snapshot
from tests.test_maintenance_source_cancel_restore import (
    confirm,
    list_url,
    preview,
    restore,
    restore_body,
    restore_url,
)
from tests.test_maintenance_source_cancel_restore_integrity import (
    confirm_second_order,
    edit_receipt,
)


def quiet(client):
    """500 不冒泡成测试异常：直接读响应状态码与报文。"""
    cloned = TestClient(app, raise_server_exceptions=False)
    cloned.headers["Authorization"] = client.headers["Authorization"]
    return cloned


def assert_corrupt_snapshot_refused(db, client, project, parent, second=None, *,
                                    execute_body=None):
    """畸形快照三入口口径：预览/执行可读 409，清单 200 且坏单被隔离，零写入。"""
    expected = _business_snapshot(db)
    with quiet(client) as probe:
        p = probe.get(restore_url(project, parent.raw_order_id))
        listing = probe.get(list_url(project))
        execution = probe.post(
            restore_url(project, parent.raw_order_id) + "/restore",
            json=execute_body or {"expected_digest": "f" * 64, "reason": "第 2 轮返工核对",
                                  "idempotency_key": str(uuid4())})
        other = (probe.get(restore_url(project, second.raw_order_id))
                 if second is not None else None)
    db.expire_all()
    assert p.status_code == 409, f"{p.status_code} {p.text[:300]}"
    assert p.json()["detail"]["code"] == "source_cancellation_restore_unavailable"
    assert "快照" in p.json()["detail"]["message"]
    assert execution.status_code == 409, f"{execution.status_code} {execution.text[:300]}"
    assert listing.status_code == 200, listing.text[:300]
    items = {row["source_order_id"]: row for row in listing.json()["items"]}
    broken = items[parent.raw_order_id]
    assert broken["state"] == "unavailable"
    assert any("快照" in row["reason"] for row in broken["conflicts"])
    if second is not None:
        assert items[second.raw_order_id]["state"] == "restorable"
        assert other.status_code == 200, other.text[:300]
        assert other.json()["state"] == "restorable"
    assert _business_snapshot(db) == expected, "畸形快照必须零写入"


# ---- 必改 1：畸形快照 → 可读 4xx、清单隔离、零写入 ----------------------------


@pytest.mark.parametrize("member", ["demand_lines", "site_issues"])
def test_missing_after_voided_at_is_readable_4xx_and_isolated(db, tmp_path, member):
    """缺 after.voided_at（sol M1 / astra B2 实测 500 的字段）必须变可读 409。"""
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    second, _batch2 = confirm_second_order(db, tmp_path, client, project, _url)
    edit_receipt(db, batch, lambda e: e["restoration"][member][0]["after"].pop("voided_at"))
    assert_corrupt_snapshot_refused(db, client, project, parent, second)


def test_missing_before_voided_at_demand_line_refused(db, tmp_path):
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    edit_receipt(db, batch, lambda e: e["restoration"]["demand_lines"][0]["before"].pop("voided_at"))
    assert_corrupt_snapshot_refused(db, client, project, parent)


@pytest.mark.parametrize("section,inner,field", [
    ("site_lines", "after", "quantity"),
    ("site_issues", "before", "status_mapping_version"),
    ("order", None, "order_no"),
    ("tombstone", None, "version"),
])
def test_snapshot_missing_whole_columns_refused(db, tmp_path, section, inner, field):
    """快照必须是与库表列一一对应的完整 dump：缺任何列都按损坏处理。"""
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)

    def change(entry):
        target = entry["restoration"][section]
        (target if inner is None else target[0][inner]).pop(field)

    edit_receipt(db, batch, change)
    assert_corrupt_snapshot_refused(db, client, project, parent)


@pytest.mark.parametrize("mutation", [
    pytest.param(lambda e: e["restoration"]["demand_lines"][0]["before"].__setitem__("qty", 5),
                 id="before_qty_int"),
    pytest.param(lambda e: e["restoration"]["demand_lines"][0]["after"].__setitem__("rogue_key", "x"),
                 id="after_extra_key"),
    pytest.param(lambda e: e["restoration"].__setitem__("rogue_section", 1),
                 id="restoration_extra_key"),
    pytest.param(lambda e: e["restoration"]["demand_lines"][0]["after"].__setitem__("voided_at", 123),
                 id="after_voided_at_int"),
    pytest.param(lambda e: e["restoration"]["site_lines"][0]["before"].__setitem__("is_active", "yes"),
                 id="site_line_before_is_active_text"),
    pytest.param(lambda e: e["restoration"]["site_issues"][0]["before"].__setitem__("normalized_status", 5),
                 id="before_normalized_status_int"),
])
def test_snapshot_type_errors_and_extra_keys_refused(db, tmp_path, mutation):
    """类型错误与多余字段同样按损坏处理：不得被当成合法小恢复集执行。"""
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    edit_receipt(db, batch, mutation)
    assert_corrupt_snapshot_refused(db, client, project, parent)


# ---- 必改 2：before 非法状态值 → 预览即 4xx（不是 200 也不是 500） ------------


@pytest.mark.parametrize("mutation", [
    pytest.param(lambda b: b.__setitem__("normalized_status", None), id="normalized_none"),
    pytest.param(lambda b: b.__setitem__("normalized_status", "bogus"), id="normalized_bogus"),
    pytest.param(lambda b: b.__setitem__("raw_status", None), id="raw_status_none"),
    pytest.param(lambda b: b.__setitem__("raw_status", ""), id="raw_status_empty"),
    pytest.param(lambda b: b.__setitem__("status_mapping_state", None), id="mapping_state_none"),
    pytest.param(lambda b: b.__setitem__("status_mapping_state", "bogus"), id="mapping_state_bogus"),
    pytest.param(lambda b: b.__setitem__("status_mapping_version", None), id="mapping_version_none"),
    pytest.param(lambda b: (b.__setitem__("normalized_status", "unknown"),
                            b.__setitem__("status_mapping_state", "mapped")),
                 id="mapped_but_unknown"),
    pytest.param(lambda b: (b.__setitem__("normalized_status", "confirmed"),
                            b.__setitem__("status_mapping_state", "unmapped")),
                 id="unmapped_but_known"),
])
def test_invalid_before_status_rejected_at_preview_not_flush(db, tmp_path, mutation):
    """非法状态值在预览阶段就 409；修复前预览 200（假可恢复）、执行 500。"""
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    edit_receipt(db, batch, lambda e: mutation(e["restoration"]["site_issues"][0]["before"]))
    assert_corrupt_snapshot_refused(db, client, project, parent)


def test_invalid_after_status_shape_rejected_at_preview(db, tmp_path):
    """after 的非法映射组合同样不能等执行写库才报错。"""
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    edit_receipt(db, batch, lambda e: e["restoration"]["site_issues"][0]["after"].__setitem__(
        "status_mapping_state", "unmapped"))
    assert_corrupt_snapshot_refused(db, client, project, parent)


# ---- 必改 3：成员真实性——历史作废行替换/删成员改数量/伪造 before ---------------


def test_historical_site_line_substitution_is_rejected(db, tmp_path):
    """同领用单历史作废行（is_active=False）冒充本次成员：整次拒绝（astra B1）。"""
    client, project, _rows, parent, lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    extra = _issue(db, project, db.get(DimPart, lines[0].part_id), "99", active=False)
    extra.issue_id = exact.issue_id
    extra.line_no = 2
    extra.source_line_id = "R2-HISTORICAL-OTHER-LINE"
    db.commit()
    confirm(db, client, detail_url)
    extra_entity = _entity(extra)

    def change(entry):
        entry["restoration"]["site_lines"][0] = dict(
            issue_line_id=extra.issue_line_id, issue_id=extra.issue_id,
            before={**extra_entity, "is_active": True}, after=extra_entity)

    edit_receipt(db, batch, change)
    expected = _business_snapshot(db)
    with quiet(client) as probe:
        p = probe.get(restore_url(project, parent.raw_order_id))
        r = probe.post(restore_url(project, parent.raw_order_id) + "/restore", json={
            "expected_digest": "f" * 64, "reason": "第 2 轮返工核对", "idempotency_key": str(uuid4())})
    db.expire_all()
    assert p.status_code == 409, f"{p.status_code} {p.text[:300]}"
    assert p.json()["detail"]["code"] == "source_cancellation_restore_unavailable"
    assert "快照" in p.json()["detail"]["message"]
    assert r.status_code == 409, f"{r.status_code} {r.text[:300]}"
    db.refresh(extra)
    db.refresh(exact)
    assert extra.is_active is False, "历史作废行不得被激活"
    assert exact.is_active is False, "本次成员也不得被半恢复"
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert _business_snapshot(db) == expected


def test_member_dropped_with_counts_adjusted_is_rejected(db, tmp_path):
    """删一个成员并同步改小确认数量：成员集合对不上审计，整次拒绝。"""
    client, project, _rows, parent, _lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)

    def change(entry):
        entry["restoration"]["site_lines"].pop(0)
        entry["result"]["voided_site_lines"] = 0

    edit_receipt(db, batch, change)
    expected = _business_snapshot(db)
    with quiet(client) as probe:
        p = probe.get(restore_url(project, parent.raw_order_id))
        r = probe.post(restore_url(project, parent.raw_order_id) + "/restore", json={
            "expected_digest": "f" * 64, "reason": "第 2 轮返工核对", "idempotency_key": str(uuid4())})
    db.expire_all()
    assert p.status_code == 409, f"{p.status_code} {p.text[:300]}"
    assert r.status_code == 409, f"{r.status_code} {r.text[:300]}"
    db.refresh(exact)
    assert exact.is_active is False, "被删成员不得静默跳过"
    assert _business_snapshot(db) == expected


def test_forged_before_content_is_rejected_by_audit_anchor(db, tmp_path):
    """before 内容伪造（合法类型、非法史实）：与作废审计对不上即拒绝。"""
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    edit_receipt(db, batch, lambda e: e["restoration"]["site_issues"][0]["before"].__setitem__(
        "raw_status", "伪造的作废前状态"))
    assert_corrupt_snapshot_refused(db, client, project, parent)


# ---- 正向对照：合法快照不得被新校验误伤 ---------------------------------------


def test_valid_receipt_still_restorable_and_restores(db, tmp_path):
    client, project, _rows, parent, lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    info = preview(client, project, parent)
    assert info["state"] == "restorable"
    response = restore(db, client, project, parent, restore_body(info))
    assert response.status_code == 200, response.text
    db.refresh(exact)
    assert exact.is_active is True
    assert all(line.is_active for line in lines)


def test_shared_issue_update_member_still_restorable(db, tmp_path):
    """共享单头（未作废、审计 action=UPDATE）同样是合法成员，可正常恢复。"""
    client, project, _rows, parent, lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    sibling = _issue(db, project, db.get(DimPart, lines[0].part_id), "4")
    sibling.issue_id = exact.issue_id
    sibling.line_no = 2
    sibling.source_line_id = "R2-UNRELATED-SOURCE-LINE"
    db.commit()
    result = confirm(db, client, detail_url)
    assert result["voided_site_lines"] == 1 and result["voided_site_issues"] == 0
    info = preview(client, project, parent)
    assert info["state"] == "restorable"
    body = restore_body(info)
    first = restore(db, client, project, parent, body)
    assert first.status_code == 200, first.text
    db.refresh(exact)
    db.refresh(sibling)
    assert exact.is_active is True
    assert sibling.is_active is True
    replay = restore(db, client, project, parent, body)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    other = restore(db, client, project, parent, {**body, "idempotency_key": str(uuid4())})
    assert other.status_code == 200 and other.json()["status"] == "already_restored"


def test_zero_member_receipt_still_restorable(db, tmp_path):
    """单头单（零成员确认）无法做成员对账，不得因此拒绝恢复。"""
    from tests.boss_board_helpers import assign, client_for, make_project
    from tests.test_maintenance_source_cancel_cascade import upload
    from tests.wbdd_fixtures import make_rows

    client = client_for(db, username="cancel-admin", role="admin")
    project = make_project(db, "R2-CANCEL-EMPTY-PROJECT")
    rows = make_rows(orders=0, headless=1)
    upload(db, tmp_path, rows)
    zero = db.scalar(select(FMaintenanceOrder))
    assign(db, zero, project)
    rows[0]["数据状态"] = "已取消"
    batch = upload(db, tmp_path, rows)
    url = f"/api/maintenance/projects/{project.project_id}/source-cancellations"
    confirm(db, client, f"{url}/{batch.id}/{zero.raw_order_id}")
    info = preview(client, project, zero)
    assert info["state"] == "restorable"
    assert info["voided_lines"] == 0
    response = restore(db, client, project, zero, restore_body(info))
    assert response.status_code == 200, response.text
    assert response.json()["restored_lines"] == 0
