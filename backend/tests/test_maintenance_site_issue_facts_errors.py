"""Default fact-edit protocol: ownership, stable errors and exact audit counts."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssueReturnEvent,
)
from app.services.maintenance_site_issue_facts import (
    SiteIssueEditError,
    apply_locked_site_issue_edits,
)
from app.services import maintenance_project_operations as operations
from app.services.maintenance_site_issue_facts import LegacySiteIssueEditContext
from tests.test_maintenance_site_issue_facts import _demand_prices, _facts


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_cross_project_demand_rejected_before_any_dirty_state(db):
    project, issue, lines, _part = _facts(db)
    foreign_project, _foreign_issue, _foreign_lines, foreign_part = _facts(db)
    demand_no = "WBDD-20260920-0999"
    _demand_prices(db, project=foreign_project, part=foreign_part,
                   prices=((demand_no, Decimal("10")),))
    first, second = sorted(lines, key=lambda line: line.issue_line_id)
    with pytest.raises(SiteIssueEditError) as caught:
        apply_locked_site_issue_edits(
            db, project=project, issue=issue,
            line_changes={first.issue_line_id: {"remark": "不得提前赋值"},
                          second.issue_line_id: {"demand_order_no": demand_no}},
            header_changes={"receiver": "不得提前赋值"},
            operated_by="tester", reason="跨项目需求单反例",
        )
    assert caught.value.code == "site_issue_edit_demand_project_mismatch"
    assert caught.value.status_code == 409
    assert caught.value.message == "关联需求单不属于当前项目，请核对单号或留空"
    assert not db.dirty and not db.new
    # A caller catching the error must not accidentally persist partial facts.
    db.commit()
    db.expire_all()
    assert issue.receiver == "接收人" and issue.version == 1
    assert all((line.remark, line.demand_order_no, line.version) == (None, None, 1) for line in lines)
    assert _count(db, MaintenanceProjectOperationAudit) == 0
    assert _count(db, MaintenanceSiteIssueReturnEvent) == 0
    assert operations.get_or_create_workbook_state(db, project_id=project.project_id).revision == 0


def test_multi_issue_multi_line_edit_has_one_fact_audit_per_issue(db):
    project, issue, lines, _part = _facts(db)
    _other_project, other_issue, other_lines, _other_part = _facts(db)
    other_issue.project_id = project.project_id
    db.commit()
    expected_ids = {issue.issue_id, other_issue.issue_id}
    for target, targets in ((issue, lines), (other_issue, other_lines)):
        patch = {line.issue_line_id: {"quantity": "3", "remark": "一并核对"} for line in targets}
        result = apply_locked_site_issue_edits(
            db, project=project, issue=target, line_changes=patch,
            header_changes={"site_location": "新地址"}, operated_by="tester", reason="多单多行",
        )
        assert result.issue_version == 2
        assert result.changed_line_ids == tuple(sorted(line.issue_line_id for line in targets))
        # Reapplying unchanged intent is a no-op; real receipt replay belongs to adapters.
        unchanged = apply_locked_site_issue_edits(
            db, project=project, issue=target, line_changes=patch,
            header_changes={"site_location": "新地址"}, operated_by="tester", reason="原样重试",
        )
        assert not unchanged.changed and unchanged.issue_version == 2
    db.commit()
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.action == "site_issue_facts_edited")))
    assert len(audits) == 2
    assert {audit.entity_id for audit in audits} == expected_ids
    assert all(len(audit.before_json["lines"]) == len(audit.after_json["lines"]) == 2 for audit in audits)
    events = list(db.scalars(select(MaintenanceSiteIssueReturnEvent)))
    assert len(events) == 2 and {item.issue_id for item in events} == expected_ids
    assert all(len(item.payload["changes"]) == 2 for item in events)
    assert _count(db, MaintenanceProjectOperationAudit) == 6  # four line audits + two fact audits


def test_invalid_field_intents_have_stable_code_without_partial_assignment(db):
    project, issue, lines, part = _facts(db)
    invalid_patches = [
        {"is_active": False}, {"quantity": "0"}, {"quantity": "NaN"},
        {"quantity": "1.0001"}, {"no_return": "是"}, {"pn": ""},
        {"part_id": True}, {"part_id": 999999},
        {"pn": "UNMATCHED-PN", "part_id": part.id},
        {"serial_number": 12}, {"demand_order_no": 12}, {"demand_order_no": "x" * 65},
    ]
    invalid_headers = [
        {"project_id": "foreign"}, {"issue_date": "2026-09-21"},
        {"receiver": 12}, {"issue_no": None}, {"site_location": "x" * 257},
    ]
    first, second = sorted(lines, key=lambda line: line.issue_line_id)
    for patch, headers in ([(item, {"receiver": "不得先写"}) for item in invalid_patches]
                           + [({}, item) for item in invalid_headers]):
        with pytest.raises(SiteIssueEditError) as caught:
            apply_locked_site_issue_edits(
                db, project=project, issue=issue,
                line_changes={first.issue_line_id: {"remark": "不得先写"}, second.issue_line_id: patch},
                header_changes=headers, operated_by="tester", reason="非法字段",
            )
        assert caught.value.code == "site_issue_edit_invalid_fields"
        assert caught.value.status_code == 422
        assert caught.value.detail == {"code": caught.value.code, "message": str(caught.value)}
        assert not db.dirty and not db.new
    db.commit()
    assert issue.receiver == "接收人" and all(line.remark is None for line in lines)
    assert _count(db, MaintenanceProjectOperationAudit) == _count(db, MaintenanceSiteIssueReturnEvent) == 0


@pytest.mark.parametrize(("scenario", "code", "http"), [
    ("project_identity", "site_issue_edit_identity_conflict", 409),
    ("line_identity", "site_issue_edit_identity_conflict", 409),
    ("missing_line", "site_issue_edit_identity_conflict", 409),
    ("project_inactive", "site_issue_edit_inactive", 409),
    ("issue_void", "site_issue_edit_inactive", 409),
    ("line_inactive", "site_issue_edit_inactive", 409),
    ("source", "site_issue_edit_source_blocked", 409),
    ("status", "site_issue_edit_source_blocked", 409),
    ("operator", "site_issue_edit_permission_denied", 403),
    ("reason", "site_issue_edit_permission_denied", 403),
])
def test_domain_failure_codes_leave_facts_versions_and_audits_untouched(db, scenario, code, http):
    project, issue, lines, _part = _facts(db)
    line_id, operator, reason = lines[0].issue_line_id, "tester", "错误契约"
    if scenario in {"project_identity", "line_identity"}:
        other_project, _other_issue, other_lines, _other_part = _facts(db)
        if scenario == "project_identity":
            project = other_project
        else:
            line_id = other_lines[0].issue_line_id
    elif scenario == "missing_line":
        line_id = "missing-line"
    elif scenario == "project_inactive":
        project.is_active = False
    elif scenario == "issue_void":
        issue.normalized_status = "void"
    elif scenario == "line_inactive":
        lines[0].is_active = False
    elif scenario == "source":
        issue.source = "direct_api"
    elif scenario == "status":
        issue.normalized_status = "draft"
    elif scenario == "operator":
        operator = " "
    elif scenario == "reason":
        reason = " "
    db.commit()
    with pytest.raises(SiteIssueEditError) as caught:
        apply_locked_site_issue_edits(
            db, project=project, issue=issue,
            line_changes={line_id: {"remark": "不得写入"}},
            header_changes={"receiver": "不得写入"}, operated_by=operator, reason=reason,
        )
    assert (caught.value.code, caught.value.status_code) == (code, http)
    assert not db.dirty and not db.new
    db.commit()
    assert issue.receiver == "接收人" and issue.version == 1
    assert all(line.remark is None and line.version == 1 for line in lines)
    assert _count(db, MaintenanceProjectOperationAudit) == _count(db, MaintenanceSiteIssueReturnEvent) == 0


def test_demand_reference_normalization_and_assignment_validation(db):
    project, issue, lines, part = _facts(db)
    demand_no = "WBDD-20260920-0101"
    _demand_prices(db, project=project, part=part, prices=((demand_no, Decimal("10")),))
    line = lines[0]
    for value, expected in ((f"  {demand_no}  ", demand_no), (None, None),
                            (demand_no, demand_no), ("  ", None), (demand_no, demand_no)):
        apply_locked_site_issue_edits(
            db, project=project, issue=issue,
            line_changes={line.issue_line_id: {"demand_order_no": value}},
            header_changes=None, operated_by="tester", reason="需求单引用",
        )
        db.commit()
        assert line.demand_order_no == expected
    # Omitting the field keeps the existing association.
    apply_locked_site_issue_edits(
        db, project=project, issue=issue, line_changes={line.issue_line_id: {"remark": "保留关联"}},
        header_changes=None, operated_by="tester", reason="局部更正",
    )
    assert line.demand_order_no == demand_no
    db.commit()
    for unknown in ("WBDD-NOT-FOUND",):
        with pytest.raises(SiteIssueEditError) as caught:
            apply_locked_site_issue_edits(
                db, project=project, issue=issue,
                line_changes={line.issue_line_id: {"demand_order_no": unknown}},
                header_changes=None, operated_by="tester", reason="无归属不可关联",
            )
        assert caught.value.code == "site_issue_edit_demand_project_mismatch"
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from app.security import UserContext
    from app.services.maintenance_source_assignments import unassign_source_orders

    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.project_id == project.project_id))
    unassign_source_orders(
        db, items=[{"assignment_id": assignment.assignment_id, "expected_version": assignment.version}],
        reason="合成夹具撤销归属", operated_by="tester",
        user_ctx=UserContext(user_id=None, role="admin"),
    )
    db.commit()
    with pytest.raises(SiteIssueEditError) as caught:
        apply_locked_site_issue_edits(
            db, project=project, issue=issue,
            line_changes={line.issue_line_id: {"demand_order_no": demand_no}},
            header_changes=None, operated_by="tester", reason="已停用归属",
        )
    assert caught.value.code == "site_issue_edit_demand_project_mismatch"
    assert not db.dirty and not db.new


def test_default_editor_does_not_acquire_for_update_locks(db):
    project, issue, lines, part = _facts(db)
    demand_no = "WBDD-20260920-0101"
    _demand_prices(db, project=project, part=part, prices=((demand_no, Decimal("10")),))
    project = operations._lock_project_for_fact_write(db, project.project_id)
    db.refresh(issue, with_for_update=True)
    for line in lines:
        db.refresh(line, with_for_update=True)
    statements = []
    connection = db.connection()

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        apply_locked_site_issue_edits(
            db, project=project, issue=issue,
            line_changes={lines[0].issue_line_id: {"quantity": "3", "demand_order_no": demand_no}},
            header_changes={"issue_date": date(2026, 9, 21)},
            operated_by="tester", reason="锁由调用方负责",
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements and not any("for update" in sql for sql in statements)
    assert _count(db, MaintenanceSiteIssueReturnEvent) == 1
    assert _count(db, MaintenanceProjectOperationAudit) == 2


@pytest.mark.parametrize("entrypoint", ["manual", "v1", "v2"])
def test_http_adapters_return_structured_domain_errors_and_roll_back(db, monkeypatch, entrypoint):
    from app.api import maintenance_project_master_workbook as workbook_api
    from app.models.maintenance_project_operations import MaintenanceSiteIssue
    from app.services import maintenance_manual_site_issue as manual
    from app.services import maintenance_project_master_workbook as master
    from tests.test_manual_site_issue_api import _client as manual_client
    from tests.test_maintenance_expense_collection_workbook import uploader

    project, issue, _lines, _part = _facts(db)
    issue_id = issue.issue_id
    if entrypoint == "manual":
        client = manual_client(db)
    else:
        client = uploader(db)
        monkeypatch.setattr(workbook_api.get_settings(), "maintenance_project_master_v2_enabled", entrypoint == "v2")
        content = (master.build_project_master_v2(db, project_id=project.project_id, sheets=(master.V2_SHEET_SITE,))
                   if entrypoint == "v2" else master.build_project_master(db, project_id=project.project_id, sheets=(master.SHEET_SITE,)))
    for code, http in [
        ("site_issue_edit_invalid_fields", 422),
        ("site_issue_edit_identity_conflict", 409),
        ("site_issue_edit_inactive", 409),
        ("site_issue_edit_source_blocked", 409),
        ("site_issue_edit_permission_denied", 403),
        ("site_issue_edit_demand_project_mismatch", 409),
    ]:
        # Inject a domain failure after a flushed write at the service boundary.
        # The actual HTTP envelope must undo it and preserve the typed error.
        def fail(session, *args, **kwargs):
            session.get(MaintenanceSiteIssue, issue_id).receiver = "应回滚的接收人"
            session.flush()
            raise SiteIssueEditError(code, "合成领域错误")

        if entrypoint == "manual":
            monkeypatch.setattr(manual, "patch_manual_site_issue", fail)
            response = client.patch(f"/api/maintenance/site-issues/manual/{issue_id}", json={
                "project_id": project.project_id, "version": 1,
                "idempotency_key": str(uuid4()), "reason": "错误映射与原子回滚",
                "receiver": "新接收人",
            })
        else:
            monkeypatch.setattr(master, "apply_project_master_v2" if entrypoint == "v2" else "apply", fail)
            response = client.post(f"/api/maintenance/projects/stable/{project.project_id}/master-workbook/apply",
                                   files={"file": ("facts.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert response.status_code == http, response.text
        assert response.json() == {"detail": {
            "code": code, "message": "合成领域错误",
            **({"issues": []} if entrypoint != "manual" else {}),
        }}
        db.expire_all()
        assert issue.receiver == "接收人" and issue.version == 1
        assert _count(db, MaintenanceProjectOperationAudit) == _count(db, MaintenanceSiteIssueReturnEvent) == 0


def test_legacy_obligation_projection_keeps_its_existing_for_update_lock(db):
    from app.models.maintenance_project_operations import MaintenanceSiteIssueDeliverySource
    from app.models.maintenance_bad_return import MaintenanceReturnObligation
    from app.services import maintenance_bad_returns, maintenance_site_return_requirements as requirements

    project, issue, lines, part = _facts(db, source="legacy")
    # Historical native obligations have real delivery identities. Legacy
    # adapters retain this old projection protocol; the default editor blocks it.
    for line in lines:
        delivery = MaintenanceSiteIssueDeliverySource(
            delivery_line_id=str(uuid4()), adapter_key="synthetic_delivery_v1",
            project_id=project.project_id, source_order_id=f"source-{uuid4()}",
            source_line_id=f"line-{uuid4()}", delivery_no="CKD-SYNTHETIC",
            delivery_date=issue.issue_date, part_id=part.id, pn=part.pn_std,
            delivered_quantity=Decimal("2"), mapping_state="ready", mapping_version="contract-v1",
        )
        db.add(delivery)
        db.flush()
        line.delivery_line_id = delivery.delivery_line_id
        line.no_return = False
    initial = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()), project_id=project.project_id, issue_id=issue.issue_id,
        issue_version=1, event_type="return_obligation_created",
        payload=operations._site_issue_return_payload(issue, lines),
    )
    db.add(initial)
    db.flush()
    maintenance_bad_returns.consume_return_event(db, initial)
    db.commit()
    before = {line.issue_line_id: requirements.snapshot(line) for line in lines}
    project = operations._lock_project_for_fact_write(db, project.project_id)
    db.refresh(issue, with_for_update=True)
    for line in lines:
        db.refresh(line, with_for_update=True)
    statements = []
    connection = db.connection()

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        apply_locked_site_issue_edits(
            db, project=project, issue=issue,
            line_changes={line.issue_line_id: {"quantity": Decimal("3")} for line in lines},
            header_changes=None, operated_by="tester", reason="保留历史义务锁",
            legacy_context=LegacySiteIssueEditContext("v2", before),
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    locks = [sql for sql in statements if "for update" in sql]
    assert locks and all("from maintenance_return_obligation " in sql for sql in locks)
    assert [row.source_quantity for row in db.scalars(select(MaintenanceReturnObligation))] == [Decimal("3"), Decimal("3")]
    assert _count(db, MaintenanceSiteIssueReturnEvent) == 2  # initial + one correction for both lines
    db.rollback()
    assert all(line.quantity == Decimal("2") and line.version == 1 for line in lines)
    assert _count(db, MaintenanceSiteIssueReturnEvent) == 1
