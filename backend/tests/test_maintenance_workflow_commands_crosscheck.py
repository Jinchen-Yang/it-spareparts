"""Independent R3a command composition checks, using PostgreSQL and real HTTP.

The expected legacy version counts are compatibility observations, not a new
business rule. No extracted helper is mocked or used as the command under test.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text

from app.db import SessionLocal, engine
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueCommand,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.system import SysUser
from app.services import maintenance_manual_site_issue as manual
from app.services import maintenance_project_operations as operations
from app.services import maintenance_site_issue_line_correction as correction
from tests.test_maintenance_site_issue_facts import _facts
from tests.test_manual_site_issue_api import _client, _part, _project


def _key(label):
    return f"r3-command-{label}-{uuid4()}"


def _line_body(*, key, issue_version=1, line_version=1, changes=None):
    return {
        "idempotency_key": key,
        "expected_issue_version": issue_version,
        "expected_line_version": line_version,
        "changes": {"no_return": True} if changes is None else changes,
        "reason": "独立跨入口命令核验",
    }


def _counts(db):
    return {
        "commands": db.query(MaintenanceSiteIssueCommand).count(),
        "audits": Counter(db.scalars(select(MaintenanceProjectOperationAudit.action))),
        "events": db.query(MaintenanceSiteIssueReturnEvent).count(),
    }


def test_http_mixed_command_history_preserves_original_receipts_and_versions(db):
    """Every route owns a commit; old receipts survive later edits and voiding."""
    project = _project(db, str(uuid4()))
    part = _part(db, f"R3-PN-{uuid4().hex[:8]}")
    client = _client(db)
    create_url = f"/api/maintenance/site-issues/projects/{project.project_id}/manual"
    create_body = {
        "idempotency_key": _key("create"), "issue_date": "2026-09-20",
        "receiver": "核验接收人", "issued_by": "核验发出人",
        "site_location": "初始现场", "reason": "合成命令链",
        "lines": [{"part_id": part.id, "quantity": "2"}],
    }
    created = client.post(create_url, json=create_body)
    assert created.status_code == 201, created.text
    first = created.json()
    issue_id, line_id = first["issue_id"], first["lines"][0]["issue_line_id"]
    line_url = f"/api/maintenance/site-issues/{issue_id}/lines/{line_id}"
    line_body = _line_body(key=_key("partial"))
    changed = client.patch(line_url, json=line_body)
    assert changed.status_code == 200, changed.text
    partial = changed.json()
    assert (partial["issue_version"], partial["line_version"], partial["workbook_revision"]) == (2, 2, 2)

    manual_url = f"/api/maintenance/site-issues/manual/{issue_id}"
    manual_body = {
        "project_id": project.project_id, "version": 2,
        "idempotency_key": _key("full"), "reason": "整单接续局部更正",
        "lines": [{"issue_line_id": line_id, "part_id": part.id,
                   "quantity": "3", "no_return": False}],
    }
    stale_manual = client.patch(manual_url, json={
        **manual_body, "version": 1, "idempotency_key": _key("stale-full"),
    })
    assert stale_manual.status_code == 409, stale_manual.text
    assert stale_manual.json()["detail"] == "领用单版本已变化，请刷新后重试"
    updated = client.patch(manual_url, json=manual_body)
    assert updated.status_code == 200, updated.text
    full = updated.json()
    # Existing manual commands increment the line and issue twice here.
    assert (full["version"], full["lines"][0]["version"], full["workflow_status"]) == (4, 4, "corrected")
    stale_partial = client.patch(line_url, json=_line_body(
        key=_key("stale-partial"), issue_version=2, line_version=2,
    ))
    assert stale_partial.status_code == 409, stale_partial.text
    assert stale_partial.json()["detail"] == {
        "code": "site_issue_edit_version_conflict",
        "message": "领用单或明细版本已变化，请刷新核对后重试",
        "current_issue_version": 4, "current_line_version": 4,
    }
    void_url = f"/api/maintenance/site-issues/{issue_id}/void"
    void_body = {
        "project_id": project.project_id, "version": 4,
        "idempotency_key": _key("void"), "reason": "通过旧 operations 作废",
    }
    voided = client.post(void_url, json=void_body)
    assert voided.status_code == 200, voided.text
    last = voided.json()
    assert (last["version"], last["workflow_status"], last["inventory_effect"]) == (6, "void", "none")
    new_after_void = client.patch(line_url, json=_line_body(
        key=_key("after-void"), issue_version=6, line_version=5,
    ))
    assert new_after_void.status_code == 409, new_after_void.text
    assert new_after_void.json()["detail"]["code"] == "site_issue_edit_inactive"

    before_replays = _counts(db)
    for method, url, body, receipt, status in (
        (client.post, create_url, create_body, first, 201),
        (client.patch, line_url, line_body, partial, 200),
        (client.patch, manual_url, manual_body, full, 200),
        (client.post, void_url, void_body, last, 200),
    ):
        replay = method(url, json=body)
        assert replay.status_code == status, replay.text
        assert replay.json() == {**receipt, "idempotent_replay": True}
    db.expire_all()
    assert _counts(db) == before_replays == {
        "commands": 4, "events": 3,
        "audits": Counter({"create": 1, "site_issue_facts_edited": 1,
                           "return_obligation_corrected": 3, "correct": 1, "void": 1}),
    }
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == 4
    assert db.get(MaintenanceSiteIssueLine, line_id).version == 5
    assert db.get(MaintenanceSiteIssueLine, line_id).is_active is False
    assert all(a.operated_by == "manual_site_admin" for a in db.scalars(select(MaintenanceProjectOperationAudit)))


@pytest.mark.parametrize("consumer", ["manual", "operations", "header"])
def test_partial_receipt_key_is_rejected_by_other_real_http_commands(db, consumer):
    """Reverse collision direction, including same action=correct consumers."""
    project, issue, (line, _), _ = _facts(db)
    client = _client(db)
    key = _key("reverse-collision")
    first = client.patch(
        f"/api/maintenance/site-issues/{issue.issue_id}/lines/{line.issue_line_id}",
        json=_line_body(key=key),
    )
    assert first.status_code == 200, first.text
    before = _counts(db)
    if consumer == "manual":
        result = client.patch(f"/api/maintenance/site-issues/manual/{issue.issue_id}", json={
            "project_id": project.project_id, "version": 2, "idempotency_key": key,
            "receiver": "应拒绝的接收人", "reason": "另一人工入口",
        })
        expected = "幂等键已用于不同的人工领用操作"
    elif consumer == "operations":
        result = client.post(f"/api/maintenance/site-issues/{issue.issue_id}/void", json={
            "project_id": project.project_id, "version": 2, "idempotency_key": key,
            "reason": "另一作废入口",
        })
        expected = "幂等键已用于不同的现场领用操作"
    else:
        result = client.patch(f"/api/maintenance/site-issues/{issue.issue_id}/header", json={
            "expected_issue_version": 2, "idempotency_key": key,
            "changes": {"receiver": "应拒绝的接收人"}, "reason": "另一单头入口",
        })
        expected = {"code": "site_issue_edit_idempotency_conflict",
                    "message": "幂等键已用于不同的操作，请核对原请求"}
    assert result.status_code == 409, result.text
    assert result.json()["detail"] == expected
    db.expire_all()
    assert _counts(db) == before
    assert db.get(MaintenanceSiteIssue, issue.issue_id).receiver == "接收人"
    assert db.get(MaintenanceSiteIssue, issue.issue_id).version == 2
    assert db.get(MaintenanceSiteIssueLine, line.issue_line_id).is_active is True
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == 1


def test_real_services_share_one_root_transaction_and_rollback_conflicting_tail(db):
    """A legacy failure after two different writers must leave no partial commit."""
    project, issue, (line, _), part = _facts(db)
    project_id, issue_id, line_id, part_id = project.project_id, issue.issue_id, line.issue_line_id, part.id
    db.rollback()
    full_key, partial_key, void_key = _key("tx-full"), _key("tx-partial"), _key("tx-void")

    def two_edits():
        full = manual.patch_manual_site_issue(
            db, issue_id=issue_id, project_id=project_id, version=1,
            idempotency_key=full_key, site_location="同事务现场",
            reason="根事务人工命令", operated_by="synthetic_operator",
        )
        partial = correction.correct_site_issue_line(
            db, issue_id=issue_id, issue_line_id=line_id,
            expected_issue_version=full["version"], expected_line_version=1,
            changes={"quantity": "3"}, reason="根事务局部命令",
            idempotency_key=partial_key, authorize=lambda _pid: "synthetic_operator",
        )
        assert full["version"] == 2
        assert (partial["issue_version"], partial["line_version"], partial["workbook_revision"]) == (3, 2, 1)
        return partial

    result = two_edits()
    with SessionLocal() as observer:
        assert observer.get(MaintenanceSiteIssue, issue_id).version == 1
        assert observer.get(MaintenanceSiteIssueLine, line_id).quantity == Decimal("2")
        assert observer.get(MaintenanceProjectWorkbookState, project_id).revision == 0
        assert _counts(observer) == {"commands": 0, "audits": Counter(), "events": 0}
    with pytest.raises(operations.MaintenanceOperationConflict, match="幂等键已用于不同的现场领用操作"):
        operations.void_site_issue(
            db, issue_id=issue_id, project_id=project_id, version=result["issue_version"],
            idempotency_key=partial_key, reason="故意复用另一入口的键", operated_by="synthetic_operator",
        )
    db.rollback()
    assert db.get(MaintenanceSiteIssue, issue_id).version == 1
    assert db.get(MaintenanceSiteIssueLine, line_id).quantity == Decimal("2")
    assert db.get(MaintenanceProjectWorkbookState, project_id).revision == 0
    assert _counts(db) == {"commands": 0, "audits": Counter(), "events": 0}

    # Same Session and the same rolled-back keys must work in a new root tx.
    result = two_edits()
    voided = operations.void_site_issue(
        db, issue_id=issue_id, project_id=project_id, version=result["issue_version"],
        idempotency_key=void_key, reason="同事务旧出口作废", operated_by="synthetic_operator",
    )
    assert voided["version"] == 5
    assert db.get(MaintenanceProjectWorkbookState, project_id).revision == 1
    db.commit()
    with SessionLocal() as observer:
        assert observer.get(MaintenanceSiteIssue, issue_id).normalized_status == "void"
        assert observer.get(MaintenanceProjectWorkbookState, project_id).revision == 1
        assert _counts(observer) == {
            "commands": 3, "events": 2,
            "audits": Counter({"correct": 1, "site_issue_facts_edited": 1,
                               "return_obligation_corrected": 3, "void": 1}),
        }
    # Committing must also reset deduplication for a later command in this Session.
    manual.create_manual_site_issue(
        db, project_id=project_id, idempotency_key=_key("tx-next"),
        issue_date=date(2026, 9, 20), receiver="接收人", issued_by="发出人",
        site_location="下一事务", lines=[{"part_id": part_id, "quantity": "1"}],
        reason="提交后继续使用 Session", operated_by="synthetic_operator",
    )
    db.commit()
    assert db.get(MaintenanceProjectWorkbookState, project_id).revision == 2
    assert db.query(MaintenanceSiteIssueCommand).count() == 4


@pytest.mark.parametrize("after_wait", ["collision", "token_revoked", "action_revoked"])
def test_manual_command_advisory_wait_blocks_other_project_partial_and_rechecks_auth(db, after_wait):
    """Hold a real old command, prove PG lock wait, then commit or revoke auth."""
    project_a, issue_a, (line_a, _), _ = _facts(db)
    project_b, issue_b, _, _ = _facts(db)
    client = _client(db)
    project_a_id, issue_a_id, line_a_id = project_a.project_id, issue_a.issue_id, line_a.issue_line_id
    project_b_id, issue_b_id = project_b.project_id, issue_b.issue_id
    key = _key("cross-project-lock")
    db.rollback()
    reached = threading.Event()
    waiter = {}

    def capture(connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            waiter["pid"] = connection.connection.driver_connection.info.backend_pid
            reached.set()

    with SessionLocal() as blocker:
        blocker_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
        old_receipt = manual.patch_manual_site_issue(
            blocker, issue_id=issue_b_id, project_id=project_b_id, version=1,
            idempotency_key=key, site_location="旧命令尚未提交的现场",
            reason="跨项目同键竞争", operated_by="manual_site_admin",
        )
        assert old_receipt["version"] == 2
        event.listen(engine, "before_cursor_execute", capture)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    client.patch,
                    f"/api/maintenance/site-issues/{issue_a_id}/lines/{line_a_id}",
                    json=_line_body(key=key),
                )
                try:
                    assert reached.wait(10), "HTTP command never reached advisory SQL"
                    deadline = time.monotonic() + 10
                    with engine.connect() as observer:
                        while True:
                            lock = observer.execute(text(
                                "SELECT wait_event_type, wait_event, pg_blocking_pids(pid) AS blockers "
                                "FROM pg_stat_activity WHERE pid = :pid"
                            ), {"pid": waiter["pid"]}).mappings().one()
                            observer.commit()
                            if blocker_pid in lock["blockers"]:
                                break
                            assert time.monotonic() < deadline, dict(lock)
                            time.sleep(0.02)
                    assert lock["wait_event_type"] == "Lock"
                    assert lock["wait_event"] == "advisory"
                    assert not future.done()
                    if after_wait != "collision":
                        with SessionLocal() as revoker:
                            user = revoker.scalar(select(SysUser).where(SysUser.username == "manual_site_admin"))
                            if after_wait == "token_revoked":
                                user.token_version = int(user.token_version or 0) + 1
                            else:
                                user.role = "boss"
                                user.permissions = {"page_maintenance": True, "data_purchase_cost": True,
                                                    "action_maintenance_site_issue_manage": False}
                            revoker.commit()
                    blocker.commit()
                finally:
                    blocker.rollback()
                result = future.result(timeout=20)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
            blocker.rollback()
    if after_wait == "collision":
        assert result.status_code == 409, result.text
        assert result.json()["detail"]["code"] == "site_issue_edit_idempotency_conflict"
    else:
        assert result.status_code == 403, result.text
        assert result.json()["detail"]["code"] == "site_issue_edit_permission_denied"
        if after_wait == "token_revoked":
            assert result.json()["detail"]["message"] == "账号权限已变化，请重新登录"
    db.expire_all()
    assert db.get(MaintenanceSiteIssue, issue_a_id).version == 1
    assert db.get(MaintenanceSiteIssueLine, line_a_id).no_return is None
    assert db.get(MaintenanceProjectWorkbookState, project_a_id).revision == 0
    assert db.get(MaintenanceProjectWorkbookState, project_b_id).revision == 1
    assert _counts(db) == {"commands": 1, "events": 0, "audits": Counter({"correct": 1})}
    command = db.scalar(select(MaintenanceSiteIssueCommand))
    assert command.project_id == project_b_id
    assert command.response_json == old_receipt
