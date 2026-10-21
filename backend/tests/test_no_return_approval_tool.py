"""D-32 §3: approved manifests through the CLI and real PostgreSQL."""

import hashlib
import json
import csv
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal, engine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueCommand,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.system import SysUser
from tests.test_maintenance_site_issue_facts import _facts
from tests.test_no_return_coverage_tool import _database_snapshot


def _entry(project, issue, line, **changes):
    return {
        "entry_id": "approved-1",
        "project_id": project.project_id,
        "issue_id": issue.issue_id,
        "issue_line_id": line.issue_line_id,
        "source": issue.source,
        "expected_issue_version": issue.version,
        "expected_line_version": line.version,
        "expected_current_no_return": line.no_return,
        "target_no_return": True,
        "reason": "依据合同核对硬盘免返条款",
        "approver": "合成批准人",
        "approval_date": "2026-10-20",
        "evidence_reference": "合成合同第3页",
        **changes,
    }


def _manifest(tmp_path, entries, name="approved.json"):
    path = tmp_path / name
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(db):
    db.expire_all()
    models = (
        MaintenanceProject,
        MaintenanceSiteIssue, MaintenanceSiteIssueLine,
        MaintenanceProjectWorkbookState, MaintenanceSiteIssueCommand,
        MaintenanceProjectOperationAudit, MaintenanceSiteIssueReturnEvent,
    )
    return {
        model.__tablename__: sorted(
            (dict(row) for row in db.execute(select(model.__table__)).mappings()),
            key=str,
        )
        for model in models
    }


def _cli(capsys, path, *args):
    from scripts.correct_no_return_from_approved_manifest import main

    code = main(["--manifest", str(path), *args])
    return code, json.loads(capsys.readouterr().out)


def test_default_dry_run_shows_approval_and_stable_identity_without_writes(db, tmp_path, capsys):
    project, issue, (line, _), _ = _facts(db)
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    before = _snapshot(db)
    whole_database = _database_snapshot()
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        code, result = _cli(capsys, path)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert code == 0
    assert result["mode"] == "dry-run"
    assert result["manifest_sha256"] == sha
    row = result["entries"][0]
    assert row["status"] == "ready"
    assert row["project_id"] == project.project_id
    assert row["issue_id"] == issue.issue_id
    assert row["issue_line_id"] == line.issue_line_id
    assert row["old_no_return"] is None
    assert row["target_no_return"] is True
    assert row["evidence_reference"] == "合成合同第3页"
    assert row["current_issue_version"] == row["current_line_version"] == 1
    assert _snapshot(db) == before
    assert _database_snapshot() == whole_database
    assert all(sql.lstrip().split()[0] in {"SELECT", "SET"} for sql in statements)
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in statements


def _operator(db, **changes):
    user = SysUser(username="approved_operator", display_name="合成实名执行人",
                   password_hash="not-a-login-secret", role="admin", **changes)
    db.add(user)
    db.commit()
    return user.username


def _apply(capsys, path, sha, entry_id="approved-1", operator="approved_operator"):
    return _cli(capsys, path, "--apply-one", entry_id,
                "--manifest-sha256", sha, "--operator", operator)


@pytest.mark.parametrize("source", ["page_manual", "workbook", "legacy"])
def test_apply_one_persists_receipt_audits_and_revision_and_replays(db, tmp_path, capsys, source):
    project, issue, (line, other), _ = _facts(db, source=source)
    _operator(db)
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    other_before = _snapshot(db)["maintenance_site_issue_line"]
    all_before = _database_snapshot()
    code, result = _apply(capsys, path, sha)
    assert code == 0
    assert result["status"] == "success"
    receipt = result["receipt"]
    assert receipt["issue_version"] == receipt["line_version"] == 2
    assert receipt["workbook_revision"] == 1
    assert receipt["no_return"] is True
    assert receipt["approved_manifest"]["manifest_sha256"] == sha
    db.expire_all()
    assert db.query(MaintenanceSiteIssueCommand).count() == 1
    assert db.query(MaintenanceSiteIssueReturnEvent).count() == 1
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit)))
    assert {a.action for a in audits} == {
        "site_issue_facts_edited", "return_obligation_corrected", "approved_manifest_applied",
    }
    for audit in audits:
        assert audit.operated_by == "approved_operator"
        assert sha in audit.reason and "approved-1" in audit.reason
        assert "合成合同第3页" in audit.reason and "合成批准人" in audit.reason
    assert next(r for r in _snapshot(db)["maintenance_site_issue_line"]
                if r["issue_line_id"] == other.issue_line_id) == next(
        r for r in other_before if r["issue_line_id"] == other.issue_line_id)
    target_before = next(r for r in other_before if r["issue_line_id"] == line.issue_line_id)
    target_after = next(r for r in _snapshot(db)["maintenance_site_issue_line"]
                        if r["issue_line_id"] == line.issue_line_id)
    # The existing ORM advances updated_at with any row correction.
    assert target_after["updated_at"] > target_before["updated_at"]
    assert {k: v for k, v in target_after.items() if k not in {"no_return", "version", "updated_at"}} == {
        k: v for k, v in target_before.items() if k not in {"no_return", "version", "updated_at"}
    }
    allowed_tables = {
        "maintenance_site_issue", "maintenance_site_issue_line",
        "maintenance_site_issue_command", "maintenance_site_issue_return_event",
        "maintenance_project_workbook_state", "maintenance_project_operation_audit",
    }
    for name, rows in _database_snapshot().items():
        if name not in allowed_tables:
            assert rows == all_before[name], name
    before_replay = _snapshot(db)
    code, replay = _apply(capsys, path, sha)
    assert code == 0 and replay["status"] == "replay"
    assert replay["receipt"] == {**receipt, "idempotent_replay": True}
    assert _snapshot(db) == before_replay


def test_same_issue_requires_stored_causal_receipt_and_keeps_original_replay(db, tmp_path, capsys):
    project, issue, (first, second), _ = _facts(db)
    _operator(db)
    path, sha = _manifest(tmp_path, [
        _entry(project, issue, first),
        _entry(project, issue, second, entry_id="approved-2", predecessor_entry_id="approved-1"),
    ])
    before = _snapshot(db)
    code, missing = _apply(capsys, path, sha, "approved-2")
    assert code == 2 and missing["status"] == "conflict"
    assert missing["code"] == "causal_receipt_required"
    assert _snapshot(db) == before
    _, first_result = _apply(capsys, path, sha)
    code, preview = _cli(capsys, path)
    assert code == 0
    assert [row["status"] for row in preview["entries"]] == ["replay", "ready"]
    assert preview["entries"][1]["effective_expected_issue_version"] == 2
    code, second_result = _apply(capsys, path, sha, "approved-2")
    assert code == 0 and second_result["status"] == "success"
    assert second_result["receipt"]["issue_version"] == 3
    assert second_result["receipt"]["line_version"] == 2
    _, replay_first = _apply(capsys, path, sha)
    _, replay_second = _apply(capsys, path, sha, "approved-2")
    assert replay_first["receipt"] == {**first_result["receipt"], "idempotent_replay": True}
    assert replay_second["receipt"] == {**second_result["receipt"], "idempotent_replay": True}


@pytest.mark.parametrize("later_change", [False, True])
def test_undo_is_a_new_approved_entry_and_conflicts_after_later_changes(
    db, tmp_path, capsys, later_change,
):
    project, issue, (line, _), _ = _facts(db)
    _operator(db)
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    _, forward = _apply(capsys, path, sha)
    receipt = forward["receipt"]
    undo = _entry(
        project, issue, line, entry_id="undo-1",
        expected_issue_version=receipt["issue_version"],
        expected_line_version=receipt["line_version"],
        expected_current_no_return=True, target_no_return=None,
        reason=f"撤销 {sha}/approved-1，经合同重新核对",
    )
    reverse_path, reverse_sha = _manifest(tmp_path, [undo], name="undo.json")
    if later_change:
        db.expire_all()
        db.get(MaintenanceSiteIssue, issue.issue_id).version += 1
        db.get(MaintenanceSiteIssueLine, line.issue_line_id).version += 1
        db.commit()
    before = _snapshot(db)
    code, result = _apply(capsys, reverse_path, reverse_sha, "undo-1")
    if later_change:
        assert code == 2 and result["status"] == "conflict"
        assert _snapshot(db) == before
    else:
        assert code == 0 and result["status"] == "success"
        assert result["receipt"]["no_return"] is None
        assert result["receipt"]["line_version"] == 3
        db.expire_all()
        assert db.query(MaintenanceSiteIssueCommand).count() == 2
        assert db.query(MaintenanceProjectOperationAudit).count() == 6


def test_unrelated_issue_change_breaks_causal_chain_without_refresh(db, tmp_path, capsys):
    project, issue, (first, second), _ = _facts(db)
    _operator(db)
    path, sha = _manifest(tmp_path, [
        _entry(project, issue, first),
        _entry(project, issue, second, entry_id="approved-2", predecessor_entry_id="approved-1"),
    ])
    _apply(capsys, path, sha)
    db.expire_all()
    db.get(MaintenanceSiteIssue, issue.issue_id).version += 1
    db.commit()
    before = _snapshot(db)
    code, result = _apply(capsys, path, sha, "approved-2")
    assert code == 2 and result["status"] == "conflict"
    assert _snapshot(db) == before


@pytest.mark.parametrize("change", ["permission", "preimage"])
def test_waiting_for_workbook_lock_rechecks_current_permission_and_preimage(db, tmp_path, change):
    from app.services.maintenance_no_return_approval import ApprovalError, apply_one, load_manifest

    project, issue, (line, _), _ = _facts(db)
    _operator(db)
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    manifest = load_manifest(path)
    project_id, line_id = project.project_id, line.issue_line_id
    reached_lock = threading.Event()

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if threading.current_thread().name.startswith("approval-writer"):
            if "maintenance_project_workbook_state" in statement and "FOR UPDATE" in statement:
                reached_lock.set()

    def apply_in_thread():
        with SessionLocal() as writer:
            try:
                return apply_one(writer, manifest, entry_id="approved-1",
                                 manifest_sha256=sha, operator="approved_operator")
            except ApprovalError as exc:
                return exc.detail

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with SessionLocal() as blocker, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="approval-writer",
        ) as pool:
            blocker.execute(select(MaintenanceProjectWorkbookState).where(
                MaintenanceProjectWorkbookState.project_id == project_id,
            ).with_for_update())
            future = pool.submit(apply_in_thread)
            try:
                assert reached_lock.wait(timeout=10), "writer did not reach actual PostgreSQL lock"
                assert not future.done()
                if change == "permission":
                    blocker.scalar(select(SysUser)).is_active = False
                else:
                    blocker.get(MaintenanceSiteIssueLine, line_id).no_return = True
                blocker.commit()
            finally:
                blocker.rollback()
            result = future.result(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result["status"] == ("rejected" if change == "permission" else "conflict")
    db.expire_all()
    assert db.query(MaintenanceSiteIssueCommand).count() == 0
    assert db.query(MaintenanceProjectOperationAudit).count() == 0
    assert db.get(MaintenanceProjectWorkbookState, project_id).revision == 0


def test_approval_audit_failure_rolls_back_fact_command_event_and_revision(db, tmp_path):
    from app.services.maintenance_no_return_approval import apply_one, load_manifest

    project, issue, (line, _), _ = _facts(db)
    _operator(db)
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    manifest = load_manifest(path)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE FUNCTION itsp4b_test_reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic approval audit unavailable'; END; $$;
        """))
        conn.execute(text("""
            CREATE TRIGGER itsp4b_test_reject_audit BEFORE INSERT
            ON maintenance_project_operation_audit FOR EACH ROW
            WHEN (NEW.action = 'approved_manifest_applied')
            EXECUTE FUNCTION itsp4b_test_reject_audit();
        """))
    before = _database_snapshot()
    try:
        with SessionLocal() as writer, pytest.raises(DBAPIError):
            apply_one(writer, manifest, entry_id="approved-1",
                      manifest_sha256=sha, operator="approved_operator")
        assert _database_snapshot() == before
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER itsp4b_test_reject_audit ON maintenance_project_operation_audit"))
            conn.execute(text("DROP FUNCTION itsp4b_test_reject_audit()"))


@pytest.mark.parametrize("change", [
    {"target_no_return": "true"}, {"target_no_return": 1}, {"expected_line_version": True},
    {"expected_current_no_return": ""}, {"approver": ""}, {"evidence_reference": ""},
    {"approval_date": "2026-02-30"}, {"quantity": "5"}, {"project_no_return_default": True},
    {"predecessor_entry_id": "not-approved"}, {"reason": "x" * 1000},
])
def test_invalid_manifest_is_rejected_before_any_write(db, tmp_path, capsys, change):
    project, issue, (line, _), _ = _facts(db)
    path, sha = _manifest(tmp_path, [_entry(project, issue, line, **change)])
    before = _snapshot(db)
    code, result = _apply(capsys, path, sha)
    assert code == 2 and result["status"] == "rejected"
    assert _snapshot(db) == before


def test_csv_manifest_keeps_null_explicit_and_can_apply_one(db, tmp_path, capsys):
    project, issue, (line, _), _ = _facts(db)
    _operator(db)
    entry = _entry(project, issue, line)
    path = tmp_path / "approved.csv"
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(entry))
        writer.writeheader()
        writer.writerow({**entry, "expected_current_no_return": "null", "target_no_return": "true"})
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    code, preview = _cli(capsys, path)
    assert code == 0 and preview["entries"][0]["old_no_return"] is None
    code, result = _apply(capsys, path, sha)
    assert code == 0 and result["status"] == "success"
    assert result["receipt"]["no_return"] is True


@pytest.mark.parametrize("suffix,content", [
    (".csv", "entry_id\n" + "x" * 140_000),
    (".json", "[" * 1500 + "null" + "]" * 1500),
], ids=["csv-field-limit", "deep-json"])
def test_malformed_files_return_structured_rejection(tmp_path, capsys, suffix, content):
    path = tmp_path / f"malformed{suffix}"
    path.write_text(content, encoding="utf-8")
    code, result = _cli(capsys, path)
    assert code == 2 and result["status"] == "rejected"
    assert result["code"] == "invalid_manifest"


@pytest.mark.parametrize("case", ["sha", "line_version", "issue_version", "old_value", "source"])
def test_approval_drift_never_refreshes_or_writes(db, tmp_path, capsys, case):
    project, issue, (line, _), _ = _facts(db)
    _operator(db)
    entry = _entry(project, issue, line)
    path, sha = _manifest(tmp_path, [entry])
    if case == "sha":
        path.write_text(path.read_text() + "\n", encoding="utf-8")
    elif case == "line_version":
        line.version += 1
    elif case == "issue_version":
        issue.version += 1
    elif case == "old_value":
        # Equal to the target is still drift, not replay/no-op.
        line.no_return = True
    else:
        issue.source = "legacy"
    db.commit()
    before = _snapshot(db)
    code, result = _apply(capsys, path, sha)
    assert code == 2
    assert result["status"] == ("rejected" if case == "sha" else "conflict")
    assert _snapshot(db) == before


@pytest.mark.parametrize("case", ["inactive_line", "void_issue", "inactive_project", "native", "delivery"])
def test_noneditable_facts_are_rejected_without_state_creation(db, tmp_path, capsys, case):
    from app.models.maintenance_project_operations import MaintenanceSiteIssueDeliverySource

    project, issue, (line, _), part = _facts(db)
    _operator(db)
    # Remove only this test's synthetic state: failure must undo the command
    # lock helper's initial INSERT too, not merely leave an existing revision.
    db.query(MaintenanceProjectWorkbookState).filter_by(project_id=project.project_id).delete()
    if case == "inactive_line":
        line.is_active = False
    elif case == "void_issue":
        issue.normalized_status = "void"
    elif case == "inactive_project":
        project.is_active = False
    elif case == "native":
        issue.source = "site_issue_v2"
    else:
        source = MaintenanceSiteIssueDeliverySource(
            delivery_line_id="synthetic-delivery", project_id=project.project_id,
            adapter_key="synthetic_delivery_v1", source_order_id="synthetic-order",
            source_line_id="synthetic-line", delivery_no="SYN-001",
            delivery_date=issue.issue_date, part_id=part.id, pn=part.pn_std,
            delivered_quantity="2", mapping_state="ready", mapping_version="synthetic-v1",
        )
        db.add(source)
        db.flush()
        line.delivery_line_id = source.delivery_line_id
    db.commit()
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    before = _snapshot(db)
    code, result = _apply(capsys, path, sha)
    assert code == 2 and result["status"] == "rejected"
    assert _snapshot(db) == before


@pytest.mark.parametrize("case", ["foreign_project", "foreign_line", "missing_line"])
def test_stable_identity_drift_is_conflict_without_writes(db, tmp_path, capsys, case):
    project, issue, (line, _), _ = _facts(db)
    other_project, other_issue, (other_line, _), _ = _facts(db)
    _operator(db)
    entry = _entry(project, issue, line)
    if case == "foreign_project":
        entry["project_id"] = other_project.project_id
    elif case == "foreign_line":
        entry["issue_line_id"] = other_line.issue_line_id
    else:
        entry["issue_line_id"] = "missing-stable-line"
    path, sha = _manifest(tmp_path, [entry])
    before = _snapshot(db)
    code, result = _apply(capsys, path, sha)
    assert code == 2 and result["status"] == "conflict"
    assert _snapshot(db) == before


@pytest.mark.parametrize("case", ["missing", "inactive", "action", "page", "cost", "scope"])
def test_operator_current_permissions_and_scope_are_required(db, tmp_path, capsys, case):
    project, issue, (line, _), _ = _facts(db)
    user = SysUser(username="approved_operator", display_name="合成执行人",
                   password_hash="test-only", role="sales", permissions={
                       "page_maintenance": True, "action_maintenance_site_issue_manage": True,
                       "data_purchase_cost": True,
                   })
    if case == "inactive":
        user.is_active = False
    if case in {"action", "page", "cost"}:
        key = {"action": "action_maintenance_site_issue_manage",
               "page": "page_maintenance", "cost": "data_purchase_cost"}[case]
        user.permissions = {**user.permissions, key: False}
    if case != "missing":
        db.add(user)
    db.commit()
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    before = _snapshot(db)
    code, result = _apply(capsys, path, sha)
    assert code == 2 and result["status"] == "rejected"
    assert _snapshot(db) == before


@pytest.mark.parametrize("permission_key", [
    "page_maintenance", "action_maintenance_site_issue_manage", "data_purchase_cost",
], ids=["page", "action", "data"])
def test_full_scope_operator_missing_single_permission_is_denied(
    db, tmp_path, capsys, permission_key,
):
    project, issue, (line, _), _ = _facts(db)
    operator_permissions = {
        "page_maintenance": True, "action_maintenance_site_issue_manage": True,
        "data_purchase_cost": True,
    }
    # boss has full project scope, so scope denial cannot mask a missing guard.
    db.add(SysUser(
        username="approved_operator", display_name="合成执行人",
        password_hash="test-only", role="boss",
        permissions={**operator_permissions, permission_key: False},
    ))
    db.commit()
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    before = _snapshot(db)
    code, result = _apply(capsys, path, sha)
    assert code == 2
    assert result["status"] == "rejected"
    assert result["code"] == "permission_denied"
    assert _snapshot(db) == before


def test_full_scope_operator_with_all_permissions_can_apply_one(db, tmp_path, capsys):
    project, issue, (line, _), _ = _facts(db)
    db.add(SysUser(
        username="approved_operator", display_name="合成执行人",
        password_hash="test-only", role="boss", permissions={
            "page_maintenance": True, "action_maintenance_site_issue_manage": True,
            "data_purchase_cost": True,
        },
    ))
    db.commit()
    path, sha = _manifest(tmp_path, [_entry(project, issue, line)])
    code, result = _apply(capsys, path, sha)
    assert code == 0
    assert result["status"] == "success"
    assert result["receipt"]["no_return"] is True
    db.expire_all()
    assert db.get(MaintenanceSiteIssueLine, line.issue_line_id).no_return is True


def test_noop_is_receipted_and_replay_rechecks_revoked_permission(db, tmp_path, capsys):
    project, issue, (line, _), _ = _facts(db)
    _operator(db)
    path, sha = _manifest(tmp_path, [_entry(project, issue, line, target_no_return=None)])
    code, result = _apply(capsys, path, sha)
    assert code == 0 and result["status"] == "no-op"
    assert result["receipt"]["issue_version"] == 1
    assert result["receipt"]["line_version"] == 1
    assert result["receipt"]["workbook_revision"] == 0
    db.expire_all()
    assert db.query(MaintenanceSiteIssueReturnEvent).count() == 0
    assert [a.action for a in db.scalars(select(MaintenanceProjectOperationAudit))] == ["approved_manifest_applied"]
    db.scalar(select(SysUser)).is_active = False
    db.commit()
    before = _snapshot(db)
    code, result = _apply(capsys, path, sha)
    assert code == 2 and result["status"] == "rejected"
    assert _snapshot(db) == before
