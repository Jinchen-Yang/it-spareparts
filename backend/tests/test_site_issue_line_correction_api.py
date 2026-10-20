"""D-31: stable, partial row corrections through the public HTTP command."""

from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import io
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.db import SessionLocal, engine
from app.models.master_data import ProductCategory
from app.models.system import SysUser

from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueCommand,
    MaintenanceSiteIssueDeliverySource,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.services import maintenance_project_operations as operations
from tests.test_maintenance_site_issue_facts import _facts
from tests.test_manual_site_issue_api import _client


def _url(issue, line):
    return f"/api/maintenance/site-issues/{issue.issue_id}/lines/{line.issue_line_id}"


def _body(*, issue_version=1, line_version=1, changes=None, key=None):
    return {
        "expected_issue_version": issue_version,
        "expected_line_version": line_version,
        "changes": changes if changes is not None else {"no_return": True},
        "reason": "依据合同逐行核对返还要求",
        "idempotency_key": key or f"row-correction-{uuid4()}",
    }


@pytest.mark.parametrize("source,mapping", [
    ("page_manual", "contract-v1"), ("workbook", "workbook-v1"),
    ("workbook", "workbook-v2"), ("legacy", "contract-v1"),
])
def test_partial_no_return_three_states_preserves_other_line_and_replays(db, source, mapping):
    project, issue, (target, untouched), _part = _facts(db, source=source)
    issue.status_mapping_version = mapping
    db.commit()
    client = _client(db)
    url = _url(issue, target)
    issue_version = line_version = 1
    for index, value in enumerate([True, False, None], start=1):
        body = _body(issue_version=issue_version, line_version=line_version,
                     changes={"no_return": value})
        result = client.patch(url, json=body)
        assert result.status_code == 200, result.text
        receipt = result.json()
        assert receipt == {
            "issue_id": issue.issue_id, "issue_line_id": target.issue_line_id,
            "issue_version": index + 1, "line_version": index + 1,
            "no_return": value, "changed": True, "idempotent_replay": False,
            "workbook_revision": index,
        }
        replay = client.patch(url, json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json() == {**receipt, "idempotent_replay": True}
        issue_version, line_version = receipt["issue_version"], receipt["line_version"]
    db.expire_all()
    assert db.get(MaintenanceSiteIssueLine, untouched.issue_line_id).is_active
    assert db.get(MaintenanceSiteIssueLine, untouched.issue_line_id).version == 1
    assert db.query(MaintenanceProjectOperationAudit).filter_by(action="site_issue_facts_edited").count() == 3
    assert db.query(MaintenanceProjectOperationAudit).filter_by(action="return_obligation_corrected").count() == 3
    assert db.query(MaintenanceSiteIssueReturnEvent).count() == 3
    assert db.query(MaintenanceSiteIssueCommand).filter_by(action="correct").count() == 3
    assert operations.get_or_create_workbook_state(db, project_id=project.project_id).revision == 3
    for audit in db.scalars(select(MaintenanceProjectOperationAudit)):
        assert audit.operated_by == "manual_site_admin"
        assert audit.reason == "依据合同逐行核对返还要求"


def test_omitted_is_noop_explicit_null_restores_standard_hdd_rule(db):
    project, issue, (line, _), part = _facts(db)
    category = ProductCategory(category_major="硬盘", category_minor="机械硬盘")
    db.add(category)
    db.flush()
    part.category_id = category.id
    line.no_return = False
    db.commit()
    client = _client(db)
    unchanged = client.patch(_url(issue, line), json=_body(changes={}))
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["no_return"] is False
    assert unchanged.json()["changed"] is False
    assert unchanged.json()["issue_version"] == unchanged.json()["line_version"] == 1
    assert unchanged.json()["workbook_revision"] == 0
    assert db.query(MaintenanceProjectOperationAudit).count() == 0
    changed = client.patch(_url(issue, line), json=_body(changes={"no_return": None}))
    assert changed.status_code == 200, changed.text
    rows = client.post("/api/maintenance/site-issues/search", json={"project_id": project.project_id}).json()["rows"]
    read_line = next(row for row in rows[0]["lines"] if row["issue_line_id"] == line.issue_line_id)
    assert read_line["no_return"] is None
    assert read_line["return_requirement"]["requirement_status"] == "exempt"
    assert read_line["return_requirement"]["required_quantity"] == "0.000"


def test_same_issue_sequential_cas_conflicts_and_original_receipt(db):
    project, issue, (first, second), _ = _facts(db)
    client = _client(db)
    first_body = _body()
    first_response = client.patch(_url(issue, first), json=first_body)
    assert first_response.status_code == 200, first_response.text
    stale = client.patch(_url(issue, second), json=_body())
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == {
        "code": "site_issue_edit_version_conflict",
        "message": "领用单或明细版本已变化，请刷新核对后重试",
        "current_issue_version": 2, "current_line_version": 1,
    }
    db.expire_all()
    assert db.get(MaintenanceSiteIssueLine, second.issue_line_id).no_return is None
    corrected = client.patch(_url(issue, second), json=_body(issue_version=first_response.json()["issue_version"]))
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["issue_version"] == 3
    assert corrected.json()["line_version"] == 2
    replay = client.patch(_url(issue, first), json=first_body)
    assert replay.json() == {**first_response.json(), "idempotent_replay": True}
    conflict = client.patch(_url(issue, first), json={**first_body, "changes": {"no_return": False}})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "site_issue_edit_idempotency_conflict"
    db.expire_all()
    assert db.query(MaintenanceSiteIssueCommand).count() == 2
    assert operations.get_or_create_workbook_state(db, project_id=project.project_id).revision == 2


def test_line_version_is_independently_compared_before_writing(db):
    _, issue, (line, _), _ = _facts(db)
    client = _client(db)
    response = client.patch(_url(issue, line), json=_body(line_version=2))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["current_issue_version"] == 1
    assert response.json()["detail"]["current_line_version"] == 1
    assert db.query(MaintenanceSiteIssueCommand).count() == 0
    assert db.query(MaintenanceProjectOperationAudit).count() == 0


def test_same_target_is_audited_noop_receipt_without_revision_change(db):
    _, issue, (line, _), _ = _facts(db)
    line.no_return = True
    db.commit()
    client = _client(db)
    response = client.patch(_url(issue, line), json=_body())
    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert response.json()["workbook_revision"] == 0
    assert response.json()["issue_version"] == response.json()["line_version"] == 1
    assert db.query(MaintenanceSiteIssueCommand).count() == 1
    assert db.query(MaintenanceProjectOperationAudit).count() == 0


@pytest.mark.parametrize("changes", [{"no_return": value} for value in [1, 0, "true", "false", [], {}]] + [{"quantity": "3"}, {"source": "workbook"}])
def test_invalid_or_out_of_scope_fields_fail_without_writes(db, changes):
    project, issue, (line, _), _ = _facts(db)
    client = _client(db)
    response = client.patch(_url(issue, line), json=_body(changes=changes))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "site_issue_edit_invalid_fields"
    db.expire_all()
    assert db.get(MaintenanceSiteIssueLine, line.issue_line_id).no_return is None
    assert db.query(MaintenanceProjectOperationAudit).count() == 0
    assert db.query(MaintenanceSiteIssueCommand).count() == 0
    assert operations.get_or_create_workbook_state(db, project_id=project.project_id).revision == 0


@pytest.mark.parametrize("case,code", [
    ("void_line", "site_issue_edit_inactive"),
    ("void_issue", "site_issue_edit_inactive"),
    ("inactive_project", "site_issue_edit_inactive"),
    ("native", "site_issue_edit_source_blocked"),
    ("foreign_line", "site_issue_edit_identity_conflict"),
    ("missing_line", "site_issue_edit_identity_conflict"),
    ("missing_issue", "site_issue_edit_identity_conflict"),
])
def test_inactive_native_and_foreign_identities_fail_closed(db, case, code):
    project, issue, (line, _), _ = _facts(db)
    if case == "void_line":
        line.is_active = False
    elif case == "void_issue":
        issue.normalized_status = "void"
    elif case == "inactive_project":
        project.is_active = False
    elif case == "native":
        issue.source = "site_issue_v2"
    db.commit()
    client = _client(db)
    url = _url(issue, line)
    if case == "foreign_line":
        _, other_issue, _, _ = _facts(db)
        url = _url(other_issue, line)
    elif case == "missing_line":
        url = f"/api/maintenance/site-issues/{issue.issue_id}/lines/{uuid4()}"
    elif case == "missing_issue":
        url = f"/api/maintenance/site-issues/{uuid4()}/lines/{line.issue_line_id}"
    response = client.patch(url, json=_body())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code
    assert db.query(MaintenanceSiteIssueCommand).count() == 0
    assert db.query(MaintenanceProjectOperationAudit).count() == 0


@pytest.mark.parametrize("case", ["scope", "action", "data", "shared", "replay_revoked"])
def test_current_real_name_permissions_and_scope_are_required(db, case):
    _, issue, (line, _), _ = _facts(db)
    client = _client(db)
    body = _body()
    if case == "replay_revoked":
        assert client.patch(_url(issue, line), json=body).status_code == 200
    user = db.scalar(select(SysUser).where(SysUser.username == "manual_site_admin"))
    user.role = "sales" if case == "scope" else "boss"
    user.permissions = {
        "page_maintenance": True, "action_maintenance_site_issue_manage": case not in {"action", "replay_revoked"},
        "data_purchase_cost": case != "data",
    }
    db.commit()
    if case == "shared":
        from app.auth import _make_token
        token, _ = _make_token(role="admin", sub=user.username, name="共享账号", fallback=True)
        client.headers["Authorization"] = f"Bearer {token}"
    else:
        login = client.post("/api/auth/login", json={"username": user.username, "password": "manual-password-123"})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    denied = client.patch(_url(issue, line), json=body)
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["code"] == "site_issue_edit_permission_denied"
    assert db.query(MaintenanceSiteIssueCommand).count() == int(case == "replay_revoked")


@pytest.mark.parametrize("canonical_matches", [True, False])
def test_project_scope_uses_canonical_salesperson_not_display_name(db, canonical_matches):
    project, issue, (line, _), _ = _facts(db)
    client = _client(db)
    user = db.scalar(select(SysUser).where(SysUser.username == "manual_site_admin"))
    user.role = "sales"
    user.display_name = "页面显示姓名"
    user.salesperson_name = "业务销售姓名"
    project.salesperson = user.salesperson_name if canonical_matches else user.display_name
    db.commit()
    login = client.post("/api/auth/login", json={"username": user.username, "password": "manual-password-123"})
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    response = client.patch(_url(issue, line), json=_body())
    assert response.status_code == (200 if canonical_matches else 403), response.text


@pytest.mark.parametrize("override", [
    {"changes": None}, {"changes": []}, {"reason": ""}, {"reason": " "},
    {"reason": "原" * 1001}, {"expected_issue_version": 0},
    {"expected_line_version": True}, {"expected_line_version": None},
    {"operator": "其他人"}, {"idempotency_key": "short"},
])
def test_invalid_envelope_has_chinese_stable_error(db, override):
    _, issue, (line, _), _ = _facts(db)
    client = _client(db)
    response = client.patch(_url(issue, line), json={**_body(), **override})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] in {"site_issue_edit_invalid_fields", "site_issue_edit_actor_required"}
    assert any("\u4e00" <= char <= "\u9fff" for char in detail["message"])


def test_correct_key_cannot_collide_with_old_manual_entry(db):
    from app.services import maintenance_manual_site_issue as manual

    project, issue, (line, _), _ = _facts(db)
    key = f"old-manual-key-{uuid4()}"
    manual.patch_manual_site_issue(
        db, issue_id=issue.issue_id, project_id=project.project_id, version=1,
        idempotency_key=key, reason="原整单编辑", operated_by="manual_site_admin",
        site_location="另一现场",
    )
    db.commit()
    client = _client(db)
    response = client.patch(_url(issue, line), json=_body(key=key))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "site_issue_edit_idempotency_conflict"
    assert db.query(MaintenanceSiteIssueCommand).count() == 1


def test_original_receipt_survives_later_same_project_issue_merge(db):
    project, issue, (line, _), _ = _facts(db)
    _, survivor, _, _ = _facts(db)
    survivor.project_id = project.project_id
    db.commit()
    client = _client(db)
    url, body = _url(issue, line), _body()
    response = client.patch(url, json=body)
    assert response.status_code == 200, response.text
    db.expire_all()
    # Fixture for D-0042's later merge: identity is stable, parent has changed.
    line.issue_id, line.line_no = survivor.issue_id, 3
    issue.normalized_status = "void"
    line.version += 1
    db.commit()
    replay = client.patch(url, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**response.json(), "idempotent_replay": True}
    assert db.query(MaintenanceSiteIssueCommand).count() == 1


@pytest.mark.parametrize("source", ["workbook", "legacy"])
def test_delivery_backed_line_remains_blocked_under_legacy_or_workbook_source(db, source):
    project, issue, (line, _), part = _facts(db, source=source)
    delivery = MaintenanceSiteIssueDeliverySource(
        delivery_line_id=str(uuid4()), adapter_key="synthetic_delivery_v1",
        project_id=project.project_id, source_order_id=f"SOURCE-{uuid4()}",
        source_line_id=f"LINE-{uuid4()}", delivery_no="CKD-SYNTHETIC",
        delivery_date=date(2026, 9, 20), part_id=part.id, pn=part.pn_std,
        delivered_quantity=Decimal("2"), mapping_state="ready", mapping_version="contract-v1",
    )
    db.add(delivery)
    db.flush()
    line.delivery_line_id = delivery.delivery_line_id
    db.commit()
    response = _client(db).patch(_url(issue, line), json=_body())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "site_issue_edit_source_blocked"
    assert db.query(MaintenanceSiteIssueCommand).count() == 0


def test_permission_revoked_while_waiting_on_state_lock_is_rechecked(db):
    project, issue, (line, _), _ = _facts(db)
    client = _client(db)
    url, body = _url(issue, line), _body()
    db.rollback()
    reached_state = threading.Event()

    def waiting(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO maintenance_project_workbook_state "):
            reached_state.set()

    with SessionLocal() as blocker:
        operations.get_or_create_workbook_state(blocker, project_id=project.project_id, lock=True)
        event.listen(engine, "before_cursor_execute", waiting)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.patch, url, json=body)
                try:
                    assert reached_state.wait(timeout=10)
                    with SessionLocal() as revoker:
                        user = revoker.scalar(select(SysUser).where(SysUser.username == "manual_site_admin"))
                        user.role = "boss"
                        user.permissions = {"page_maintenance": True, "data_purchase_cost": True,
                                            "action_maintenance_site_issue_manage": False}
                        revoker.commit()
                finally:
                    blocker.rollback()
                response = future.result(timeout=20)
        finally:
            blocker.rollback()
            event.remove(engine, "before_cursor_execute", waiting)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "site_issue_edit_permission_denied"
    assert db.query(MaintenanceSiteIssueCommand).count() == 0
    assert db.query(MaintenanceProjectOperationAudit).count() == 0


def test_no_return_preserves_quantity_part_prices_receipts_and_notes(db):
    from app.services.maintenance_return_receipts import receipt_summary, register_receipt

    project, issue, (line, _), _ = _facts(db)
    project.no_return_note = "提醒文字不能生成行更正"
    line.serial_number = "SN-preserved"
    line.remark = "现场备注"
    db.commit()
    operations.fill_manual_cost(
        db, project_id=project.project_id, issue_line_id=line.issue_line_id, version=1,
        manual_unit_cost=Decimal("40"), evidence="合同价凭证", reason="补价", operated_by="tester",
    )
    register_receipt(db, project_id=project.project_id, pn="OTHER-PN", qty=5, operated_by="receiver")
    db.commit()
    db.refresh(line)
    before = operations.site_issue_line_dict(line)
    receipt_before = receipt_summary(db, project_id=project.project_id)
    client = _client(db)
    response = client.patch(_url(issue, line), json=_body(line_version=before["version"]))
    assert response.status_code == 200, response.text
    db.expire_all()
    after = operations.site_issue_line_dict(db.get(MaintenanceSiteIssueLine, line.issue_line_id))
    assert {k: v for k, v in after.items() if k not in {"version", "no_return"}} == {
        k: v for k, v in before.items() if k not in {"version", "no_return"}}
    assert receipt_summary(db, project_id=project.project_id) == receipt_before
    assert project.no_return_note == "提醒文字不能生成行更正"


@pytest.mark.parametrize("same_key", [False, True])
def test_two_http_connections_serialize_conflict_or_replay(db, same_key):
    _, issue, (line, _), _ = _facts(db)
    client = _client(db)
    url = _url(issue, line)
    body = _body()
    db.rollback()
    barrier = threading.Barrier(2)

    def synchronize(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            barrier.wait(timeout=10)

    def request(payload):
        with TestClient(client.app) as concurrent_client:
            concurrent_client.headers.update(client.headers)
            return concurrent_client.patch(url, json=payload)

    event.listen(engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request, payload) for payload in (
                body, body if same_key else {**body, "idempotency_key": f"parallel-{uuid4()}"},
            )]
            responses = [future.result(timeout=20) for future in futures]
    finally:
        event.remove(engine, "before_cursor_execute", synchronize)
    assert sorted(response.status_code for response in responses) == ([200, 200] if same_key else [200, 409])
    if same_key:
        assert sorted(response.json()["idempotent_replay"] for response in responses) == [False, True]
    assert db.query(MaintenanceSiteIssueCommand).count() == 1
    assert db.query(MaintenanceProjectOperationAudit).filter_by(action="site_issue_facts_edited").count() == 1


@pytest.mark.parametrize("failed_table", ["maintenance_project_operation_audit", "maintenance_site_issue_return_event", "maintenance_site_issue_command"])
def test_audit_outbox_and_receipt_failures_rollback_the_entire_command(db, failed_table):
    project, issue, (line, _), _ = _facts(db)
    client = _client(db)
    url = _url(issue, line)

    def reject_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(f"INSERT INTO {failed_table} "):
            raise RuntimeError("synthetic transaction failure")

    event.listen(engine, "before_cursor_execute", reject_insert)
    try:
        with TestClient(client.app, raise_server_exceptions=False) as failing_client:
            failing_client.headers.update(client.headers)
            response = failing_client.patch(url, json=_body())
        assert response.status_code == 500
    finally:
        event.remove(engine, "before_cursor_execute", reject_insert)
    db.expire_all()
    assert db.get(MaintenanceSiteIssueLine, line.issue_line_id).no_return is None
    assert db.get(MaintenanceSiteIssueLine, line.issue_line_id).version == 1
    assert db.get(MaintenanceSiteIssue, issue.issue_id).version == 1
    assert operations.get_or_create_workbook_state(db, project_id=project.project_id).revision == 0
    assert db.query(MaintenanceSiteIssueCommand).count() == 0
    assert db.query(MaintenanceProjectOperationAudit).count() == 0
    assert db.query(MaintenanceSiteIssueReturnEvent).count() == 0


def test_old_workbook_cannot_overwrite_web_return_requirement(db, monkeypatch):
    from tests.test_maintenance_site_issue_facts import _site_workbook
    from app.api import maintenance_project_master_workbook as master_api

    monkeypatch.setattr(master_api.get_settings(), "maintenance_project_master_v2_enabled", True)
    project, issue, (line, _), _ = _facts(db, source="workbook")
    client = _client(db)
    client.app.include_router(master_api.router, prefix="/api")
    book, sheet, columns, rows = _site_workbook(db, project)
    sheet.cell(rows[line.issue_line_id], columns["是否应返还"]).value = "是"
    content = io.BytesIO()
    book.save(content)
    db.rollback()
    response = client.patch(_url(issue, line), json=_body())
    assert response.status_code == 200, response.text
    uploaded = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/master-workbook/validate",
        files={"file": ("stale.xlsx", content.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert any(conflict.get("entity_id") == line.issue_line_id for conflict in uploaded.json()["conflicts"]), uploaded.json()
    assert response.json()["workbook_revision"] == 1
