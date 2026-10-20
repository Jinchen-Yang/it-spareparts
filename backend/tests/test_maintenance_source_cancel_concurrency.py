"""D-34: public confirmation waits for real PostgreSQL writers and rechecks."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import monotonic, sleep
from uuid import uuid4

import pytest
from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.db import SessionLocal
from app.main import app
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandDeleteEvent,
    MaintenanceDemandDeleteIntent,
    MaintenanceDemandDeleteIntentItem,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysAuditLog, SysImportBatch
from app.services import maintenance_project_assignments as project_assignments
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from tests.boss_board_helpers import client_for, make_project
from tests.test_maintenance_source_cancel_cascade import confirm_body, setup


def _business_snapshot(session):
    """Compare committed business effects, excluding normal HTTP access logs."""
    models = (
        FMaintenanceOrder, FMaintenanceLine, MaintenanceSourceOrderAssignment,
        MaintenanceDemandTombstone, MaintenanceDemandDeleteIntent,
        MaintenanceDemandDeleteIntentItem, MaintenanceDemandDeleteEvent,
        MaintenanceSiteIssue, MaintenanceSiteIssueLine, MaintenanceRkdReturnLine,
        MaintenanceSiteIssueReturnEvent, MaintenanceProjectOperationAudit,
        MaintenanceProjectAuditLog, MaintenanceProjectWorkbookState,
        MaintenanceProjectUserAssignment, MaintenanceProject, SysAuditLog, SysImportBatch,
    )
    return {
        model.__tablename__: list(session.execute(
            select(model.__table__).order_by(*model.__table__.primary_key.columns),
        ))
        for model in models
    }


def _wait_for_advisory_waiters(monitor, owner_pid, futures):
    """Observe actual lock contention; no sleep-based scheduling assumptions."""
    deadline = monotonic() + 10
    while monotonic() < deadline:
        waiting = monitor.scalars(text(
            "SELECT DISTINCT pid FROM pg_locks "
            "WHERE locktype = 'advisory' AND NOT granted "
            "AND :owner_pid = ANY(pg_blocking_pids(pid))"
        ), {"owner_pid": owner_pid}).all()
        if len(waiting) >= len(futures):
            return
        for future in futures:
            if future.done():
                response = future.result()
                pytest.fail(f"确认没有等待共同数据锁：{response.status_code} {response.text}")
        sleep(0.01)
    pytest.fail("未观察到确认请求的 PostgreSQL advisory 锁等待")


@pytest.mark.parametrize("drift,expected_status", [
    ("demand", 409), ("assignment", 409), ("project_scope", 403),
])
def test_waiting_confirmation_rechecks_demand_assignment_and_scope_without_writes(
    db, tmp_path, drift, expected_status,
):
    client, project, _, parent, lines, exact, unknown, receipt, batch, _, detail_url = setup(db, tmp_path)
    project_id, source_id, line_id = project.project_id, parent.raw_order_id, lines[0].id
    target_id = make_project(db, "CANCEL-CONCURRENT-TARGET").project_id if drift == "assignment" else None
    if drift == "project_scope":
        client = client_for(db, username="cancel-scoped-concurrent", overrides={
            "page_maintenance": True, "action_maintenance_demand_delete": True,
            "own_maintenance_projects_only": False,
        })
        project_assignments.sync_project_viewers(
            db, project_id=project_id, usernames=["cancel-scoped-concurrent"],
            operated_by="test-admin", reason="授予合成项目可见范围",
        )
        db.commit()
    body = confirm_body(client, detail_url)
    db.commit()

    with SessionLocal() as changing:
        changing.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
        owner_pid = changing.scalar(text("SELECT pg_backend_pid()"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, detail_url + "/confirm", json=body)
            try:
                _wait_for_advisory_waiters(db, owner_pid, [future])
                if drift == "demand":
                    changing.get(FMaintenanceLine, line_id).line_note = "持锁期间完成的人工更正"
                elif drift == "assignment":
                    # The public assignment writer only accepts effective
                    # headers. Preserve cancellation here to isolate scope
                    # drift, while obeying the immutable-generation trigger.
                    previous = changing.scalar(select(MaintenanceSourceOrderAssignment).where(
                        MaintenanceSourceOrderAssignment.source_order_id == source_id,
                        MaintenanceSourceOrderAssignment.is_active.is_(True),
                    ))
                    previous.is_active = False
                    previous.archived_at = datetime.now(UTC)
                    previous.archived_by = "concurrent-admin"
                    previous.version += 1
                    changing.flush()
                    changing.add(MaintenanceSourceOrderAssignment(
                        assignment_id=str(uuid4()), project_id=target_id,
                        source_order_id=source_id, is_active=True, created_by="concurrent-admin",
                    ))
                else:
                    project_assignments.sync_project_viewers(
                        changing, project_id=project_id, usernames=[],
                        operated_by="concurrent-admin", reason="确认等待期间撤销项目可见范围",
                    )
                changing.flush()
                expected = _business_snapshot(changing)
                changing.commit()
                response = future.result(timeout=15)
            finally:
                changing.rollback()
        assert response.status_code == expected_status, response.text

    db.expire_all()
    assert _business_snapshot(db) == expected
    assert all(line.is_active for line in lines)
    assert exact.is_active and unknown.is_active and receipt.line_status == "active"
    assert "_source_cancel_confirmations" not in db.get(SysImportBatch, batch.id).report_json


def test_simultaneous_same_request_executes_once_then_returns_original_result(db, tmp_path):
    client, _, _, parent, lines, exact, unknown, receipt, batch, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    issue_version = db.get(MaintenanceSiteIssue, exact.issue_id).version
    line_version = exact.version
    source_id, batch_id = parent.raw_order_id, batch.id
    db.commit()
    other_client = TestClient(app)
    other_client.headers["Authorization"] = client.headers["Authorization"]
    try:
        with SessionLocal() as blocking:
            blocking.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
            owner_pid = blocking.scalar(text("SELECT pg_backend_pid()"))
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(requester.post, detail_url + "/confirm", json=body)
                           for requester in (client, other_client)]
                try:
                    _wait_for_advisory_waiters(db, owner_pid, futures)
                    blocking.commit()
                    responses = [future.result(timeout=15) for future in futures]
                finally:
                    blocking.rollback()
    finally:
        other_client.close()
    assert [response.status_code for response in responses] == [200, 200], [r.text for r in responses]
    results = [response.json() for response in responses]
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert {key: value for key, value in results[0].items() if key != "replayed"} == {
        key: value for key, value in results[1].items() if key != "replayed"
    }
    db.expire_all()
    assert all(not line.is_active for line in lines)
    assert not exact.is_active and unknown.is_active and receipt.line_status == "active"
    assert exact.version == line_version + 1
    assert db.get(MaintenanceSiteIssue, exact.issue_id).version == issue_version + 1
    assert db.scalar(select(func.count()).select_from(MaintenanceDemandTombstone).where(
        MaintenanceDemandTombstone.source_order_id == source_id)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "maintenance_line",
        MaintenanceProjectOperationAudit.action == "VOID")) == 2
    assert len(db.get(SysImportBatch, batch_id).report_json["_source_cancel_confirmations"]) == 1
