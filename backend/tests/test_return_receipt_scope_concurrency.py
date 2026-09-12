"""Actual project-writer locks must precede the final receipt authorization."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.api import maintenance_return_receipts as api
from app.db import SessionLocal
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_catalog as catalog
from app.services import maintenance_return_receipts as receipts
from tests.test_return_receipt_hardening import _create, _failed, _grant
from tests.test_maintenance_return_receipts_api import _client
from tests.test_site_issue_v2_api import _project


@pytest.mark.parametrize("action", ["create", "update", "void", "transfer"])
def test_receipt_waits_for_scope_revocation_then_denies_all_write_paths(db, monkeypatch, action):
    source = _project(db, project_id=str(uuid4()))
    target = _project(db, project_id=str(uuid4()))
    client = _client(db, username="racing-viewer", role="readonly", permissions={
        "page_maintenance": True, "action_maintenance_bad_return_manage": True,
    })
    _grant(db, source.project_id, "racing-viewer")
    _grant(db, target.project_id, "racing-viewer")
    existing = _create(db, source) if action != "create" else None
    revoked_project = target.project_id if action == "transfer" else source.project_id
    source_id = source.project_id
    db.commit()
    worker = {}
    started = Event()
    original_lock = receipts.lock_receipt_context

    def capture_context(session):
        original_lock(session)
        worker["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
        started.set()

    monkeypatch.setattr(receipts, "lock_receipt_context", capture_context)
    with SessionLocal() as changing:
        # Use the actual catalog/viewer writer protocol, not an invented lock.
        catalog._lock_project_for_master_write(changing, project_id=revoked_project)
        assignments.sync_project_viewers(changing, project_id=revoked_project,
                                         usernames=[], operated_by="admin", reason="revoke scope")
        writer_pid = changing.scalar(text("SELECT pg_backend_pid()"))

        def request():
            if action == "create":
                return client.post(f"/api/maintenance/projects/stable/{source_id}/return-receipts",
                                   json={"pn": "TEST", "qty": 1})
            url = f"/api/maintenance/return-receipts/{existing['receipt_id']}"
            if action == "void":
                return client.post(f"{url}/void", json={"version": 1, "reason": "void"})
            changes = {"project_id": revoked_project} if action == "transfer" else {"qty": 8}
            return client.patch(url, json={"version": 1, "reason": "update", **changes})

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(request)
            assert started.wait(timeout=5)
            blocked = False
            deadline = monotonic() + 5
            try:
                while monotonic() < deadline:
                    blockers = db.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": worker["pid"]})
                    if writer_pid in blockers:
                        blocked = True
                        break
                    sleep(0.01)
            finally:
                changing.commit()
            response = future.result(timeout=10)
            assert blocked, "scope must be checked after acquiring the project writer's lock"
            assert response.status_code == 403, response.text
    db.expire_all()
    rows = db.scalars(select(MaintenanceRkdReturnLine)).all()
    if existing is None:
        assert not rows
    else:
        assert len(rows) == 1
        assert rows[0].project_id == source_id and rows[0].qty == 3
        assert rows[0].version == 1 and rows[0].line_status == "active"
    assert _failed(db)[-1].after_json["status_code"] == 403


@pytest.mark.parametrize("revoke_target", [False, True])
def test_authorized_transfer_holds_both_project_scopes_through_commit(db, monkeypatch, revoke_target):
    source = _project(db, project_id=str(uuid4()))
    target = _project(db, project_id=str(uuid4()))
    client = _client(db, username="transferring-viewer", role="readonly", permissions={
        "page_maintenance": True, "action_maintenance_bad_return_manage": True,
    })
    _grant(db, source.project_id, "transferring-viewer")
    _grant(db, target.project_id, "transferring-viewer")
    existing = _create(db, source)
    source_id, target_id = source.project_id, target.project_id
    db.commit()
    authorized, release, writer_started = Event(), Event(), Event()
    ids = {}
    original_scope = api.enforce_maintenance_project_access

    def pause_after_both_scopes(session, *, project_id, ctx):
        original_scope(session, project_id=project_id, ctx=ctx)
        if project_id == target_id:
            ids["request"] = session.scalar(text("SELECT pg_backend_pid()"))
            authorized.set()
            assert release.wait(timeout=10)

    monkeypatch.setattr(api, "enforce_maintenance_project_access", pause_after_both_scopes)

    def request():
        return client.patch(f"/api/maintenance/return-receipts/{existing['receipt_id']}", json={
            "version": 1, "reason": "authorized transfer", "project_id": target_id,
        })

    def revoke():
        with SessionLocal() as changing:
            ids["writer"] = changing.scalar(text("SELECT pg_backend_pid()"))
            writer_started.set()
            project_id = target_id if revoke_target else source_id
            catalog._lock_project_for_master_write(changing, project_id=project_id)
            assignments.sync_project_viewers(changing, project_id=project_id, usernames=[],
                                             operated_by="admin", reason="scope changed afterwards")
            changing.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        response_future = pool.submit(request)
        assert authorized.wait(timeout=5)
        revoke_future = pool.submit(revoke)
        assert writer_started.wait(timeout=5)
        deadline, blocked = monotonic() + 5, False
        try:
            while monotonic() < deadline:
                blockers = db.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": ids["writer"]})
                if ids["request"] in blockers:
                    blocked = True
                    break
                sleep(0.01)
        finally:
            release.set()
        response = response_future.result(timeout=10)
        revoke_future.result(timeout=10)
        assert blocked, "either project's scope writer must wait until authorized transfer commits"
        assert response.status_code == 200, response.text
    db.expire_all()
    row = db.get(MaintenanceRkdReturnLine, existing["receipt_id"])
    assert row.project_id == target_id and row.version == 2
