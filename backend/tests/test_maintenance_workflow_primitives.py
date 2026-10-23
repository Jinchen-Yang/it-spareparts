"""Database contracts for the extracted state and command identity primitives."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text

from app.db import SessionLocal, engine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.services import maintenance_project_operations as legacy
from app.services import maintenance_site_issue_command_identity as identity
from app.services import maintenance_workbook_state as states


def _projects(db, count=1):
    ids = sorted(str(uuid4()) for _ in range(count))
    db.add_all(MaintenanceProject(
        project_id=value, project_code=f"PRIMITIVE-{value}",
        display_name="合成锁测试项目", lifecycle_status="ongoing",
    ) for value in ids)
    db.commit()
    return ids


def _wait_for_blocker(observer, worker, blocker_pid):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if blocker_pid in observer.scalar(
            text("SELECT pg_blocking_pids(:pid)"), {"pid": worker["pid"]},
        ):
            return
        Event().wait(0.01)
    pytest.fail("the competing connection never waited for the held transaction")


def test_state_creation_is_transactional_and_missing_project_is_not_created(db):
    project_id, = _projects(db)
    state = states.get_or_create_workbook_state(db, project_id=project_id)
    assert state.revision == 0 and len(state.data_version) == 64
    with SessionLocal() as observer:
        assert observer.get(MaintenanceProjectWorkbookState, project_id) is None
    db.rollback()
    assert db.get(MaintenanceProjectWorkbookState, project_id) is None
    assert states.lock_project_for_fact_write(db, str(uuid4())) is None
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectWorkbookState)) == 0


def test_revision_deduplication_resets_after_commit_and_rollback(db):
    project_id, = _projects(db)
    row = states.get_or_create_workbook_state(db, project_id=project_id, lock=True)
    first_version = row.data_version
    assert states.bump_locked_workbook_revision(db, state=row).revision == 1
    assert legacy.bump_workbook_revision(db, project_id=project_id).revision == 1
    assert row.data_version != first_version
    db.commit()
    assert states.bump_workbook_revision(db, project_id=project_id).revision == 2
    db.rollback()
    assert states.bump_workbook_revision(db, project_id=project_id).revision == 2
    db.commit()
    with SessionLocal() as observer:
        assert observer.get(MaintenanceProjectWorkbookState, project_id).revision == 2


def test_states_lock_sorted_unique_ids_one_at_a_time(db):
    ids = _projects(db, 3)
    observed = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if (statement.startswith("SELECT maintenance_project_workbook_state.")
                and "FOR UPDATE" in statement):
            observed.append(parameters["project_id_1"])

    event.listen(engine, "before_cursor_execute", capture)
    try:
        rows = states.lock_workbook_states(
            db, project_ids=[ids[2], None, "", " ", ids[1], f" {ids[0]} ", ids[2]],
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert list(rows) == ids
    assert observed == ids
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectWorkbookState)) == 3


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_two_connections_create_one_state_after_transaction_finishes(db, finish):
    project_id, = _projects(db)
    ready, worker = Event(), {}

    def create():
        with SessionLocal() as session:
            session.execute(text("SET LOCAL lock_timeout = '8s'"))
            worker["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            row = legacy.get_or_create_workbook_state(session, project_id=project_id, lock=True)
            result = (row.project_id, row.revision, row.data_version)
            session.commit()
            return result

    with SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        row = states.get_or_create_workbook_state(holder, project_id=project_id, lock=True)
        expected = (row.project_id, row.revision, row.data_version)
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        future = pool.submit(create)
        try:
            assert ready.wait(timeout=5)
            _wait_for_blocker(db, worker, holder_pid)
        finally:
            getattr(holder, finish)()
        assert future.result(timeout=10) == expected
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectWorkbookState)) == 1


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_old_and_new_command_locks_share_identity_until_transaction_ends(db, finish):
    key = f"shared-key-{uuid4()}"
    ready, worker = Event(), {}

    def acquire():
        with SessionLocal() as session:
            session.execute(text("SET LOCAL lock_timeout = '8s'"))
            worker["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            legacy._lock_idempotency_key(session, key)
            session.rollback()

    with SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        identity.lock_site_issue_idempotency_key(holder, key)
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        # An unrelated command can progress while this identity is held.
        with SessionLocal() as other:
            other.execute(text("SET LOCAL lock_timeout = '1s'"))
            identity.lock_site_issue_idempotency_key(other, key + "-other")
            other.rollback()
        future = pool.submit(acquire)
        try:
            assert ready.wait(timeout=5)
            _wait_for_blocker(db, worker, holder_pid)
        finally:
            getattr(holder, finish)()
        future.result(timeout=10)


def test_command_fingerprint_keeps_unicode_order_and_absent_vs_null():
    expected = '74a97fa2843f79e26abf9268c6feae42bc192ed9525eb7bbecbe4815465bd814'
    assert identity.site_issue_request_fingerprint({"名字": "领用", "n": 0}) == expected
    assert legacy._site_issue_request_fingerprint({"n": 0, "名字": "领用"}) == expected
    assert identity.site_issue_request_fingerprint({}) != identity.site_issue_request_fingerprint({"n": None})
    with pytest.raises(TypeError):
        identity.site_issue_request_fingerprint({"unsupported": object()})
