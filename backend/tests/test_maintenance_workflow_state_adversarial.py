"""Independent transaction/identity-map counterexamples for the R3 extraction.

These are compatibility boundaries, not new business policy. Successful nested
transactions share a root; savepoint rollback deduplication is deliberately not
promised. All failures below are real PostgreSQL constraint/lock failures.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError, PendingRollbackError

from app.db import SessionLocal
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.services import maintenance_project_operations as legacy
from app.services import maintenance_site_issue_command_identity as identity
from app.services import maintenance_workbook_state as states


def _seed(db, count=1):
    ids = [str(uuid4()) for _ in range(count)]
    db.add_all([
        MaintenanceProject(
            project_id=pid, project_code=f"ADVERSARIAL-{pid}",
            display_name="committed name", lifecycle_status="ongoing",
        )
        for pid in ids
    ])
    db.flush()
    for pid in ids:
        states.get_or_create_workbook_state(db, project_id=pid)
    db.commit()
    return ids


@pytest.mark.parametrize("entry", [states, legacy], ids=["new", "legacy"])
def test_lock_and_bump_preserve_unflushed_state_and_project_writes(db, entry):
    pid, = _seed(db)
    row = db.get(MaintenanceProjectWorkbookState, pid)
    project = db.get(MaintenanceProject, pid)
    row.last_export_id = "caller-pending-export"
    project.display_name = "caller-pending-name"
    # SessionLocal disables autoflush. A refresh/populate_existing here would
    # silently discard these actual caller-owned changes.
    assert not db.autoflush
    assert entry.get_or_create_workbook_state(db, project_id=pid, lock=True) is row
    lock_project = (states.lock_project_for_fact_write if entry is states
                    else legacy._lock_project_for_fact_write)
    assert lock_project(db, pid) is project
    assert row.last_export_id == "caller-pending-export"
    assert project.display_name == "caller-pending-name"
    # Neither locking helper may commit or flush these pending ORM writes.
    assert db.scalar(select(MaintenanceProject.display_name).where(
        MaintenanceProject.project_id == pid)) == "committed name"
    assert db.scalar(select(MaintenanceProjectWorkbookState.last_export_id).where(
        MaintenanceProjectWorkbookState.project_id == pid)) is None
    entry.bump_locked_workbook_revision(db, state=row)
    with SessionLocal() as observer:
        assert observer.get(MaintenanceProject, pid).display_name == "committed name"
        assert observer.get(MaintenanceProjectWorkbookState, pid).revision == 0
    db.commit()
    with SessionLocal() as observer:
        saved = observer.get(MaintenanceProjectWorkbookState, pid)
        assert (saved.revision, saved.last_export_id) == (1, "caller-pending-export")
        assert observer.get(MaintenanceProject, pid).display_name == "caller-pending-name"


def test_lock_keeps_preloaded_identity_until_caller_explicitly_refreshes(db):
    pid, = _seed(db)
    cached = db.get(MaintenanceProjectWorkbookState, pid)
    project = db.get(MaintenanceProject, pid)
    with SessionLocal() as other:
        changed = other.get(MaintenanceProject, pid)
        changed.display_name = "external commit"
        states.bump_workbook_revision(other, project_id=pid)
        other.commit()
    assert states.get_or_create_workbook_state(db, project_id=pid, lock=True) is cached
    assert states.lock_project_for_fact_write(db, pid) is project
    # Row locking does not bypass SQLAlchemy's identity map. This documents
    # the caller's freshness obligation without blessing a stale revision bump.
    assert (cached.revision, project.display_name) == (0, "committed name")
    assert db.scalar(select(MaintenanceProjectWorkbookState.revision).where(
        MaintenanceProjectWorkbookState.project_id == pid)) == 1
    assert db.scalar(select(MaintenanceProject.display_name).where(
        MaintenanceProject.project_id == pid)) == "external commit"
    db.refresh(cached)
    db.refresh(project)
    assert project.display_name == "external commit"
    assert states.bump_locked_workbook_revision(db, state=cached).revision == 2
    db.commit()
    with SessionLocal() as observer:
        assert observer.get(MaintenanceProjectWorkbookState, pid).revision == 2


def test_committed_savepoints_do_not_split_root_revision_deduplication(db):
    first, second = _seed(db, 2)
    rows = states.lock_workbook_states(db, project_ids=[second, first])
    root = db.get_transaction()
    with db.begin_nested():
        assert db.get_transaction() is root
        legacy.bump_locked_workbook_revision(db, state=rows[first])
    with db.begin_nested():
        assert db.get_transaction() is root
        states.bump_workbook_revision(db, project_id=first)
        legacy.bump_workbook_revision(db, project_id=second)
    states.bump_locked_workbook_revision(db, state=rows[second])
    assert [rows[pid].revision for pid in [first, second]] == [1, 1]
    with SessionLocal() as observer:
        assert [observer.get(MaintenanceProjectWorkbookState, pid).revision
                for pid in [first, second]] == [0, 0]
    db.commit()
    with SessionLocal() as observer:
        assert [observer.get(MaintenanceProjectWorkbookState, pid).revision
                for pid in [first, second]] == [1, 1]


@pytest.mark.parametrize("entry", [states, legacy], ids=["new", "legacy"])
def test_real_flush_failure_rolls_back_prior_project_and_does_not_register_failed_bump(db, entry):
    first, second = _seed(db, 2)
    rows = states.lock_workbook_states(db, project_ids=[first, second])
    root = db.get_transaction()
    project = db.get(MaintenanceProject, first)
    project.display_name = "must roll back with both revisions"
    entry.bump_locked_workbook_revision(db, state=rows[first])
    # A second project's invalid state fails the real CHECK constraint during
    # the helper's flush, after the first project's revision already flushed.
    rows[second].revision = -2
    with pytest.raises(IntegrityError) as failed:
        entry.bump_locked_workbook_revision(db, state=rows[second])
    assert failed.value.orig.sqlstate == "23514"
    registry = db.info["_maintenance_workbook_revision_bumps"]
    assert registry[0] is root
    assert first in registry[1]
    assert second not in registry[1]
    with pytest.raises(PendingRollbackError):
        states.bump_workbook_revision(db, project_id=second)
    db.rollback()
    with SessionLocal() as observer:
        assert observer.get(MaintenanceProject, first).display_name == "committed name"
        assert [observer.get(MaintenanceProjectWorkbookState, pid).revision
                for pid in [first, second]] == [0, 0]
    # Reuse the same Session.info: both the failed and previously successful
    # project must bump again after the root transaction has been discarded.
    for pid in [first, second]:
        assert entry.bump_workbook_revision(db, project_id=pid).revision == 1
    assert db.get_transaction() is not root
    db.commit()
    with SessionLocal() as observer:
        assert [observer.get(MaintenanceProjectWorkbookState, pid).revision
                for pid in [first, second]] == [1, 1]


def test_session_close_reuse_cannot_inherit_uncommitted_bump_registry(db):
    pid, = _seed(db)
    assert states.bump_workbook_revision(db, project_id=pid).revision == 1
    root = db.get_transaction()
    db.close()
    # SQLAlchemy close() returns the connection and rolls back but Session.info
    # survives. The root transaction identity must still separate the retry.
    assert db.info["_maintenance_workbook_revision_bumps"][0] is root
    assert legacy.bump_workbook_revision(db, project_id=pid).revision == 1
    assert db.get_transaction() is not root
    db.commit()
    with SessionLocal() as observer:
        assert observer.get(MaintenanceProjectWorkbookState, pid).revision == 1


@pytest.mark.parametrize("holder_is_legacy", [True, False], ids=["legacy-holder", "new-holder"])
def test_project_and_state_lock_entries_share_exclusion_and_recover_after_timeout(db, holder_is_legacy):
    pid, = _seed(db)
    with SessionLocal() as holder, SessionLocal() as contender:
        if holder_is_legacy:
            assert legacy._lock_project_for_fact_write(holder, pid) is not None
        else:
            states.get_or_create_workbook_state(holder, project_id=pid, lock=True)
        contender.execute(text("SET LOCAL lock_timeout = '150ms'"))
        with pytest.raises(OperationalError) as failed:
            if holder_is_legacy:
                states.get_or_create_workbook_state(contender, project_id=pid, lock=True)
            else:
                legacy._lock_project_for_fact_write(contender, pid)
        assert failed.value.orig.sqlstate == "55P03"
        contender.rollback()
        holder.rollback()
        # Same contender Session after an aborted PostgreSQL transaction.
        assert legacy._lock_project_for_fact_write(contender, pid) is not None
        assert states.bump_workbook_revision(contender, project_id=pid).revision == 1
        contender.commit()
    assert db.get(MaintenanceProjectWorkbookState, pid).revision == 1


@pytest.mark.parametrize("lock_entry", [identity.lock_site_issue_idempotency_key,
                                       legacy._lock_idempotency_key],
                         ids=["new", "legacy"])
def test_database_failure_releases_command_identity_and_retry_reacquires(db, lock_entry):
    pid, = _seed(db)
    key = f"flush-failure-{uuid4()}"
    lock_entry(db, key)
    probe = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))")
    with SessionLocal() as observer:
        assert observer.scalar(probe, {"key": key}) is False
        observer.rollback()
        project = db.get(MaintenanceProject, pid)
        project.display_name = None
        with pytest.raises(IntegrityError) as failed:
            states.bump_workbook_revision(db, project_id=pid)
        assert failed.value.orig.sqlstate == "23502"
        db.rollback()
        assert observer.scalar(probe, {"key": key}) is True
        observer.rollback()
        lock_entry(db, key)
        assert observer.scalar(probe, {"key": key}) is False
        observer.rollback()
        assert states.bump_workbook_revision(db, project_id=pid).revision == 1
        db.commit()
        assert observer.scalar(probe, {"key": key}) is True
        observer.rollback()
    assert db.get(MaintenanceProject, pid).display_name == "committed name"
