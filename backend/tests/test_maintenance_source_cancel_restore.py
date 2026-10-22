"""D-0022: 误取消确认的完整恢复——只逆转本次确认作废，漂移零写，回执幂等。"""
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.maintenance import MaintenanceDemandTombstone
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
)
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue as SiteIssue,
)
from app.models.system import SysImportBatch

from tests.boss_board_helpers import assign, client_for, make_project
from tests.test_maintenance_source_cancel_cascade import (
    confirm_body,
    setup,
    upload,
)
from tests.test_maintenance_source_cancel_concurrency import _business_snapshot


def restore_url(project, source_order_id):
    return (f"/api/maintenance/projects/{project.project_id}/source-cancellations"
            f"/restorations/{source_order_id}")


def list_url(project):
    return f"/api/maintenance/projects/{project.project_id}/source-cancellations/restorations"


def confirm(db, client, detail_url):
    response = client.post(detail_url + "/confirm", json=confirm_body(client, detail_url))
    assert response.status_code == 200, response.text
    db.expire_all()
    return response.json()


def preview(client, project, parent):
    response = client.get(restore_url(project, parent.raw_order_id))
    assert response.status_code == 200, response.text
    return response.json()


def restore_body(preview_json, *, key=None, reason="误确认取消，实名恢复核对"):
    return {"expected_digest": preview_json["expected_digest"], "reason": reason,
            "idempotency_key": key or str(uuid4())}


def restore(db, client, project, parent, body):
    response = client.post(restore_url(project, parent.raw_order_id) + "/restore", json=body)
    db.expire_all()
    return response


def revision(db, project):
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    return state.revision if state else None


def restore_audits(db):
    return list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.action == "RESTORE")))


def corrected_audits(db):
    return list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.action == "return_obligation_corrected")))


def test_full_restore_reverses_cascade_and_preserves_protection_ledger(db, tmp_path):
    from app.services.maintenance_field_protection import protected_fields

    client, project, _rows, parent, lines, exact, unknown, _receipt, _batch, _url, detail_url = setup(
        db, tmp_path)
    lines[0].manual_override = {"qty": {"value": "5.000", "source_value": "3.000",
                                        "updated_by": "cancel-admin",
                                        "updated_at": "2026-10-20T10:00:00", "source": "page"}}
    db.commit()
    protected_before = protected_fields(raw_line_id=lines[0].raw_line_id,
                                        edited_source=lines[0].edited_source,
                                        manual_override=lines[0].manual_override)
    assert "qty" in protected_before
    confirm(db, client, detail_url)
    assert all(not line.is_active for line in lines) and not exact.is_active
    voided_issue = db.get(SiteIssue, exact.issue_id)
    assert voided_issue.normalized_status == "void" and voided_issue.voided_at is not None
    revision_after_confirm = revision(db, project)
    corrections_after_confirm = len(corrected_audits(db))
    exact_version_after_confirm = exact.version

    body = preview(client, project, parent)
    assert body["state"] == "restorable"
    assert body["restored"] is False
    assert {row["raw_line_id"] for row in body["restorable_demands"]} == {line.raw_line_id for line in lines}
    assert [row["issue_line_id"] for row in body["restorable_site_lines"]] == [exact.issue_line_id]
    assert [row["issue_id"] for row in body["restorable_site_issues"]] == [exact.issue_id]
    assert body["restorable_site_issues"][0]["original_status"] == "confirmed"
    assert body["conflicts"] == []

    response = restore(db, client, project, parent, restore_body(body))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "restored" and result["replayed"] is False
    assert result["restored_lines"] == 2 and result["restored_site_lines"] == 1
    assert result["restored_site_issues"] == 1

    db.expire_all()
    for line in lines:
        assert line.is_active and line.voided_at is None
        assert line.voided_by is None and line.void_reason is None
    assert exact.is_active and unknown.is_active
    issue = db.get(SiteIssue, exact.issue_id)
    assert issue.normalized_status == "confirmed" and issue.raw_status == "confirmed"
    assert issue.status_mapping_state == "mapped" and issue.voided_at is None
    tombstone = db.get(MaintenanceDemandTombstone, parent.raw_order_id)
    assert tombstone.restored_at is not None and tombstone.restored_by == "cancel-admin"
    assert "误确认取消" in tombstone.restore_reason
    # 字段保护账本（D-34）原样保留：恢复后保护状态与作废前一致
    assert lines[0].manual_override["qty"]["value"] == "5.000"
    assert protected_fields(raw_line_id=lines[0].raw_line_id, edited_source=lines[0].edited_source,
                            manual_override=lines[0].manual_override) == protected_before
    # 反向 requirements 更正（新增记录，不删原记录）+ revision 升版
    assert len(corrected_audits(db)) == corrections_after_confirm + 1
    assert exact.version == exact_version_after_confirm + 1
    assert revision(db, project) == revision_after_confirm + 1
    # 恢复后预览显示已恢复
    assert preview(client, project, parent)["state"] == "restored"


def test_restore_reactivates_only_lines_voided_by_this_confirmation(db, tmp_path):
    client, project, _rows, parent, lines, exact, unknown, _receipt, _batch, _url, detail_url = setup(
        db, tmp_path)
    # 取消确认前已因其他原因作废的需求行：恢复不得复活
    lines[1].is_active = False
    lines[1].voided_at = datetime(2026, 10, 1, tzinfo=UTC)
    lines[1].voided_by, lines[1].void_reason = "别人", "其他原因作废"
    db.commit()
    confirm(db, client, detail_url)
    db.expire_all()
    assert not lines[0].is_active and not lines[1].is_active

    body = preview(client, project, parent)
    assert [row["raw_line_id"] for row in body["restorable_demands"]] == [lines[0].raw_line_id]
    response = restore(db, client, project, parent, restore_body(body))
    assert response.status_code == 200, response.text
    assert response.json()["restored_lines"] == 1
    db.expire_all()
    assert lines[0].is_active and lines[0].void_reason is None
    assert not lines[1].is_active and lines[1].void_reason == "其他原因作废"
    assert lines[1].voided_by == "别人"
    assert exact.is_active and unknown.is_active


@pytest.mark.parametrize("drift", ["manual_edit", "reimport", "tombstone_restored", "reassigned"])
def test_drift_conflicts_zero_write(db, tmp_path, drift):
    from app.services import maintenance_demands

    client, project, rows, parent, lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    body = preview(client, project, parent)
    assert body["state"] == "restorable"
    if drift == "manual_edit":
        lines[0].line_note = "确认后他人改动"
    elif drift == "reimport":
        for row in rows:
            row["数据状态"] = "已生效"
        upload(db, tmp_path, rows)
    elif drift == "tombstone_restored":
        maintenance_demands.restore_demand(db, source_order_id=parent.raw_order_id,
                                          operated_by="别人", reason="来源再现，先实名恢复墓碑")
    else:
        assign(db, parent, project)
    db.commit()
    db.expire_all()
    expected = _business_snapshot(db)
    stale = preview(client, project, parent)
    assert stale["state"] == "conflict" and stale["expected_digest"] is None
    assert stale["conflicts"] and all(item["reason"] for item in stale["conflicts"])

    response = restore(db, client, project, parent, restore_body(body))
    assert response.status_code == 409, response.text
    payload = response.json()
    assert payload["detail"]["code"] == "source_cancellation_restore_conflict"
    assert payload["detail"]["conflicts"]
    db.expire_all()
    assert _business_snapshot(db) == expected
    assert "_source_cancel_restorations" not in (
        db.get(SysImportBatch, batch.id).report_json or {})
    assert restore_audits(db) == []
    if drift != "tombstone_restored":
        assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert revision(db, project) == stale["context"]["revision"]


def test_stale_expected_digest_rejected_zero_write(db, tmp_path):
    client, project, _rows, parent, lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    fresh = preview(client, project, parent)
    stale_digest = {"expected_digest": "f" * 64, "reason": "误确认，恢复",
                    "idempotency_key": str(uuid4())}
    response = restore(db, client, project, parent, stale_digest)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_cancellation_restore_conflict"
    db.expire_all()
    assert all(not line.is_active for line in lines)
    assert "_source_cancel_restorations" not in (
        db.get(SysImportBatch, batch.id).report_json or {})
    assert restore_audits(db) == []
    assert revision(db, project) == fresh["context"]["revision"]


def test_same_key_replay_returns_same_receipt_without_extra_audits(db, tmp_path):
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, _batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    body = restore_body(preview(client, project, parent))
    first = restore(db, client, project, parent, body)
    assert first.status_code == 200, first.text
    db.expire_all()
    audits = restore_audits(db)
    revision_after = revision(db, project)
    version_after = db.get(MaintenanceDemandTombstone, parent.raw_order_id).version

    second = restore(db, client, project, parent, body)
    assert second.status_code == 200, second.text
    assert {**second.json(), "replayed": False} == first.json()
    db.expire_all()
    assert restore_audits(db) == audits
    assert revision(db, project) == revision_after
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).version == version_after
    # 同键异请求 → 409，不写
    changed = {**body, "reason": "同键改变原因"}
    assert restore(db, client, project, parent, changed).status_code == 409


def test_different_key_after_restore_reports_status_without_second_write(db, tmp_path):
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, _batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    restore(db, client, project, parent, restore_body(preview(client, project, parent)))
    db.expire_all()
    audits = restore_audits(db)
    revision_after = revision(db, project)

    again = restore(db, client, project, parent, restore_body(preview(client, project, parent)))
    assert again.status_code == 200, again.text
    payload = again.json()
    assert payload["status"] == "already_restored" and payload["replayed"] is False
    assert payload["restored_via"] == "cancel_restore"
    assert payload["restored_by"] == "cancel-admin"
    db.expire_all()
    assert restore_audits(db) == audits
    assert revision(db, project) == revision_after


def test_restore_requires_action_and_project_access(db, tmp_path):
    client, project, _rows, parent, lines, _exact, _unknown, _receipt, _batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    body = restore_body(preview(client, project, parent))
    no_action = client_for(db, username="restore-no-action", role="boss", overrides={
        "page_maintenance": True, "action_maintenance_demand_delete": False})
    assert no_action.get(restore_url(project, parent.raw_order_id)).status_code == 403
    assert no_action.post(restore_url(project, parent.raw_order_id) + "/restore",
                          json=body).status_code == 403
    outsider = client_for(db, username="restore-outsider", overrides={
        "page_maintenance": True, "action_maintenance_demand_delete": True})
    assert outsider.get(restore_url(project, parent.raw_order_id)).status_code == 403
    assert outsider.post(restore_url(project, parent.raw_order_id) + "/restore",
                         json=body).status_code == 403
    assert outsider.get(list_url(project)).status_code == 403
    db.expire_all()
    assert all(not line.is_active for line in lines)
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    assert restore_audits(db) == []


def test_restore_audits_reference_original_receipt(db, tmp_path):
    client, project, _rows, parent, lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    restore(db, client, project, parent, restore_body(preview(client, project, parent)))
    db.expire_all()
    audits = restore_audits(db)
    assert {(row.entity_type, row.entity_id) for row in audits} == {
        ("maintenance_line", str(line.id)) for line in lines} | {
        ("site_issue_line", exact.issue_line_id), ("site_issue", exact.issue_id)}
    assert all(row.operated_by == "cancel-admin" for row in audits)
    assert all(f"batch={batch.id}" in row.reason and "取消确认回执" in row.reason
               for row in audits)
    assert all(row.before_json["is_active"] is False and row.after_json["is_active"] is True
               for row in audits if row.entity_type != "site_issue")
    void_audit = next(row for row in audits if row.entity_type == "site_issue")
    assert void_audit.before_json["normalized_status"] == "void"
    assert void_audit.after_json["normalized_status"] == "confirmed"


def test_old_receipt_without_snapshot_is_not_auto_restorable(db, tmp_path):
    client, project, _rows, parent, lines, _exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    # 模拟本功能上线前的历史回执：剥掉恢复快照
    stored = db.get(SysImportBatch, batch.id)
    # deepcopy：JSONB 原地改内层再回裹同一对象时新旧相等，属性不脏、UPDATE 不触发
    receipts = deepcopy(stored.report_json["_source_cancel_confirmations"])
    for entry in receipts.values():
        entry.pop("restoration", None)
    stored.report_json = {**stored.report_json,
                          "_source_cancel_confirmations": receipts}
    db.commit()
    db.expire_all()
    expected = _business_snapshot(db)

    body = preview(client, project, parent)
    assert body["state"] == "unavailable"
    assert body["expected_digest"] is None
    assert any("快照" in item["reason"] for item in body["conflicts"])

    response = client.post(restore_url(project, parent.raw_order_id) + "/restore",
                           json={"expected_digest": "f" * 64, "reason": "想恢复",
                                 "idempotency_key": str(uuid4())})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_cancellation_restore_unavailable"
    db.expire_all()
    assert _business_snapshot(db) == expected
    assert all(not line.is_active for line in lines)


def test_list_scopes_to_project_and_unknown_order_is_unavailable(db, tmp_path):
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, _batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    items = client.get(list_url(project)).json()["items"]
    assert [item["source_order_id"] for item in items] == [parent.raw_order_id]
    assert items[0]["state"] == "restorable"
    assert items[0]["batch_id"]
    other_project = make_project(db, "RESTORE-OTHER")
    # admin 角色天然有全局项目访问权（预期 409）；403 断言须用非 admin 的外部用户
    outsider = client_for(db, username="restore-list-outsider", overrides={
        "page_maintenance": True, "action_maintenance_demand_delete": True})
    assert outsider.get(restore_url(other_project, parent.raw_order_id)).status_code == 403
    response = client.get(restore_url(project, "no-such-order"))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_cancellation_restore_unavailable"


def test_restore_requires_reason_and_validates_inputs(db, tmp_path):
    client, project, _rows, parent, _lines, _exact, _unknown, _receipt, _batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    body = restore_body(preview(client, project, parent))
    blank = {**body, "reason": "   "}
    assert restore(db, client, project, parent, blank).status_code == 422
    bad_key = {**body, "idempotency_key": "x"}
    assert restore(db, client, project, parent, bad_key).status_code == 422
