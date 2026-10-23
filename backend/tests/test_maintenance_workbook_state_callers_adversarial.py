"""Independent R3b PG counterexamples; optional environment-driven fault checks."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectAuditLog
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.services import maintenance_demand_manual as demand
from app.services import maintenance_expense_collection_workbook as expense
from app.services import maintenance_project_catalog as catalog
from app.services import maintenance_project_operations as legacy
from app.services import maintenance_workbook_state as states


@pytest.fixture(autouse=True)
def _fault_experiment(monkeypatch):
    """Opt-in runtime mutants never modify production files."""
    mutant = os.environ.get("R3B_TRANSACTION_MUTANT")
    if mutant == "skip_lock":
        original = states.get_or_create_workbook_state

        def unlocked(db, *, project_id, lock=False):
            return original(db, project_id=project_id, lock=False)

        monkeypatch.setattr(states, "get_or_create_workbook_state", unlocked)
    elif mutant == "drop_refresh":
        monkeypatch.setattr(demand, "_lock_workbook_states", states.lock_workbook_states)
    elif mutant == "early_commit":
        original = states.bump_locked_workbook_revision

        def commit_after_bump(db, *, state):
            result = original(db, state=state)
            db.commit()
            return result

        monkeypatch.setattr(states, "bump_locked_workbook_revision", commit_after_bump)
    elif mutant == "reverse_order":
        def reverse(db, *, project_ids):
            return {pid: states.get_or_create_workbook_state(db, project_id=pid, lock=True)
                    for pid in sorted(set(project_ids), reverse=True)}

        monkeypatch.setattr(states, "lock_workbook_states", reverse)
    else:
        assert mutant is None, f"unknown fault experiment: {mutant}"


def _seed(db, count=1):
    ids = [f"r3b-adversarial-{i}" for i in range(count)]
    db.add_all(MaintenanceProject(project_id=pid, project_code=pid,
        display_name=f"独立事务测试 {i}", lifecycle_status="ongoing")
        for i, pid in enumerate(ids))
    db.commit()
    for pid in ids:
        legacy.get_or_create_workbook_state(db, project_id=pid)
    db.commit()
    return ids


def _blocked(observer, worker_pid, holder_pid, future):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if holder_pid in observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": worker_pid}):
            return
        assert not future.done(), "caller skipped the existing workbook-state lock"
        Event().wait(0.01)
    pytest.fail("no PostgreSQL blocking relation observed")


def _edit(db, pid, note):
    return catalog.update_project(db, project_id=pid, version=1,
        updates={"project_note": note}, reason="独立事务反例", operated_by="r3b-adversarial")


@pytest.mark.parametrize("caller", ["catalog", "expense"])
def test_migrated_caller_waits_on_legacy_state_before_project_lock(db, caller):
    pid, = _seed(db)
    ready, worker = Event(), {}

    def run():
        with SessionLocal() as session:
            session.execute(text("SET LOCAL lock_timeout = '8s'"))
            worker["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            if caller == "catalog":
                _edit(session, pid, "只在外层事务内可见")
            else:
                assert expense._lock_project_apply_context(session, project_id=pid).project_id == pid
            session.rollback()

    with SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        legacy.get_or_create_workbook_state(holder, project_id=pid, lock=True)
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        future = pool.submit(run)
        try:
            assert ready.wait(5)
            _blocked(db, worker["pid"], holder_pid, future)
            # NOWAIT proves the blocked caller has not taken the later project lock.
            with SessionLocal() as observer:
                project = observer.scalar(select(MaintenanceProject).where(
                    MaintenanceProject.project_id == pid).with_for_update(nowait=True))
                assert project.project_note is None
                assert observer.get(MaintenanceProjectWorkbookState, pid).revision == 0
                observer.rollback()
        finally:
            holder.rollback()
        future.result(timeout=10)
    db.expire_all()
    assert db.get(MaintenanceProject, pid).project_note is None
    assert db.get(MaintenanceProjectWorkbookState, pid).revision == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectAuditLog)) == 0


def test_demand_caller_refreshes_existing_identity_after_another_transaction(db):
    pid, = _seed(db)
    stale = db.get(MaintenanceProjectWorkbookState, pid)
    assert stale.revision == 0
    with SessionLocal() as writer:
        legacy.bump_workbook_revision(writer, project_id=pid)
        writer.commit()
    assert stale.revision == 0
    locked = demand._lock_workbook_states(db, project_ids=[pid])[pid]
    assert locked is stale
    assert locked.revision == 1, "caller must refresh even when the helper preserves ORM identity"
    assert locked.data_version == states.workbook_data_version(pid, 1)
    states.bump_locked_workbook_revision(db, state=locked)
    db.commit()
    with SessionLocal() as observer:
        persisted = observer.get(MaintenanceProjectWorkbookState, pid)
        assert persisted.revision == 2
        assert persisted.data_version == states.workbook_data_version(pid, 2)


def test_demand_multi_project_lock_order_is_visible_in_postgres(db):
    first, middle, last = _seed(db, 3)
    ready, worker = Event(), {}

    def run():
        with SessionLocal() as session:
            session.execute(text("SET LOCAL lock_timeout = '8s'"))
            worker["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            rows = demand._lock_workbook_states(session, project_ids=[last, middle, first])
            result = list(rows)
            session.rollback()
            return result

    with SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        legacy.get_or_create_workbook_state(holder, project_id=middle, lock=True)
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        future = pool.submit(run)
        try:
            assert ready.wait(5)
            _blocked(db, worker["pid"], holder_pid, future)
            with SessionLocal() as observer:
                with pytest.raises(OperationalError) as caught:
                    observer.scalar(select(MaintenanceProjectWorkbookState).where(
                        MaintenanceProjectWorkbookState.project_id == first).with_for_update(nowait=True))
                assert caught.value.orig.sqlstate == "55P03"
                observer.rollback()
                assert observer.scalar(select(MaintenanceProjectWorkbookState).where(
                    MaintenanceProjectWorkbookState.project_id == last).with_for_update(nowait=True)) is not None
                observer.rollback()
        finally:
            holder.rollback()
        assert future.result(timeout=10) == [first, middle, last]


def test_failure_after_real_bump_rolls_back_and_same_session_retry_bumps(db, monkeypatch):
    pid, = _seed(db)
    original = states.bump_locked_workbook_revision

    def fail_after_flush(session, *, state):
        original(session, state=state)
        raise RuntimeError("injected after revision and fact flush")

    with monkeypatch.context() as patch:
        patch.setattr(states, "bump_locked_workbook_revision", fail_after_flush)
        with pytest.raises(RuntimeError, match="injected after revision"):
            _edit(db, pid, "必须随异常回滚")
        db.rollback()
    with SessionLocal() as observer:
        project = observer.get(MaintenanceProject, pid)
        assert (project.project_note, project.version) == (None, 1)
        assert observer.get(MaintenanceProjectWorkbookState, pid).revision == 0
        assert observer.scalar(select(func.count()).select_from(MaintenanceProjectAuditLog)) == 0
    _edit(db, pid, "重试后才提交")
    assert legacy.bump_workbook_revision(db, project_id=pid).revision == 1
    with SessionLocal() as observer:
        assert observer.get(MaintenanceProject, pid).project_note is None
    db.commit()
    with SessionLocal() as observer:
        project = observer.get(MaintenanceProject, pid)
        assert (project.project_note, project.version) == ("重试后才提交", 2)
        assert observer.get(MaintenanceProjectWorkbookState, pid).revision == 1
        assert observer.scalar(select(func.count()).select_from(MaintenanceProjectAuditLog)) == 1


def test_nested_expense_context_preserves_prelocked_state_identity_and_rejects_other_project(db):
    first, second = _seed(db, 2)
    locked = states.lock_workbook_states(db, project_ids=[second, first])
    state = locked[first]
    state.revision = 9
    state.data_version = states.workbook_data_version(first, 9)
    with db.no_autoflush:
        assert expense._lock_project_apply_context(db, project_id=first, workbook_state=state) is state
        assert (state.revision, state.data_version) == (9, states.workbook_data_version(first, 9))
        with pytest.raises(expense.WorkbookError, match="并发状态与项目不匹配"):
            expense._lock_project_apply_context(db, project_id=first, workbook_state=locked[second])
    db.rollback()
    with SessionLocal() as observer:
        assert observer.get(MaintenanceProjectWorkbookState, first).revision == 0
        assert observer.get(MaintenanceProjectWorkbookState, second).revision == 0
