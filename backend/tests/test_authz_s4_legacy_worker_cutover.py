"""Already queued legacy work cannot retain authority after source cutover."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.orm import Session

from app.api import imports as api
from app.authz import runtime
from app.config import get_settings
from app.models.authorization import SysAuthorizationCutover
from app.models.system import SysImportBatch, SysImportJob
from tests.test_authz_s4_import import actor, cerbos_url, _purchase  # noqa: F401


def test_legacy_queued_worker_does_not_import_after_source_cutover(db, actor, tmp_path, monkeypatch):
    user, _ = actor
    # A real legacy queue row has no stable owner, epoch or canonical manifest.
    job = SysImportJob(created_by=user.username, mode='skip', total_files=1, status='processing')
    db.add(job)
    db.commit()
    file = tmp_path / 'queued-before-cutover.xlsx'
    file.write_bytes(_purchase())
    monkeypatch.setattr(api, 'SessionLocal', lambda: Session(db.get_bind()))
    api._process_import_job(job.id, [(str(file), file.name)], 'skip', user.username)
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0
    current = db.get(SysImportJob, job.id, populate_existing=True)
    assert current.status == 'failed' and current.done_files == 0
    assert current.error_files == 1


def test_first_activation_waits_for_legacy_commit_then_rejects_old_execution(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'authorization_mode', 'unified_modules')
    monkeypatch.setattr(settings, 'authz_enabled_resources', [])
    monkeypatch.setattr(settings, 'authz_suspended_resources', [])
    monkeypatch.setattr(settings, 'authz_sales_analysis_enabled', False)
    monkeypatch.setattr(settings, 'authz_sales_analysis_suspended', False)
    db.execute(delete(SysAuthorizationCutover).where(SysAuthorizationCutover.resource == 'import_purchase'))
    db.commit()
    runtime.guard_legacy_execution(db, ('import_purchase',))
    # A real transaction is allowed to finish before activation is committed.
    row = SysImportJob(created_by='legacy-before-cutover', mode='skip', total_files=0, status='done')
    db.add(row)
    db.flush()
    legacy_pid = db.scalar(text('SELECT pg_backend_pid()'))
    monkeypatch.setattr(settings, 'authz_enabled_resources', ['import_purchase'])
    started = Event()
    activation_pids = []
    def before(_conn, cursor, statement, _params, _ctx, _many):
        if current_thread().name.startswith('cutover-test') and 'pg_advisory_xact_lock(' in statement:
            activation_pids.append(cursor.connection.info.backend_pid)
            started.set()
    engine = db.get_bind()
    event.listen(engine, 'before_cursor_execute', before)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='cutover-test') as pool:
            future = pool.submit(runtime.activate_configured_resources, engine)
            try:
                assert started.wait(5)
                deadline = time.monotonic() + 5
                blocked = False
                with engine.connect() as observer:
                    while time.monotonic() < deadline:
                        blockers = observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': activation_pids[0]})
                        if legacy_pid in blockers:
                            blocked = True
                            break
                        time.sleep(.01)
                assert blocked, 'Actual PostgreSQL activation must wait on the legacy transaction'
                assert not future.done()
                db.commit()
                future.result(timeout=5)
            finally:
                db.rollback()  # unblock the worker even if a preceding assertion fails
    finally:
        event.remove(engine, 'before_cursor_execute', before)
    assert db.scalar(select(SysAuthorizationCutover.resource).where(
        SysAuthorizationCutover.resource == 'import_purchase')) == 'import_purchase'
    with pytest.raises(HTTPException) as denied:
        runtime.guard_legacy_execution(db, ('import_purchase',))
    assert denied.value.status_code == 503
    assert denied.value.detail['code'] == 'AUTHZ_LEGACY_EXECUTION_RETIRED'
    db.rollback()
    # An unrelated never-cutover source is not retired by a different source.
    runtime.guard_legacy_execution(db, ('import_inventory',))
    db.rollback()
    monkeypatch.setattr(settings, 'authz_enabled_resources', [])
    monkeypatch.setattr(settings, 'authorization_mode', 'legacy_compat')
    with pytest.raises(HTTPException) as suspended:
        runtime.guard_legacy_execution(db, ('import_purchase',))
    assert suspended.value.detail['code'] == 'AUTHZ_LEGACY_EXECUTION_RETIRED'
    db.rollback()


def test_legacy_guard_rejects_historical_snapshot_even_before_cutover(db):
    connection = db.get_bind().connect().execution_options(isolation_level='REPEATABLE READ')
    try:
        with Session(bind=connection) as snapshot:
            with pytest.raises(HTTPException) as denied:
                runtime.guard_legacy_execution(snapshot, ('import_purchase',))
            assert denied.value.detail['code'] == 'AUTHZ_LEGACY_EXECUTION_RETIRED'
    finally:
        connection.close()


def test_other_never_cutover_source_keeps_legacy_worker_but_unknown_source_does_not(db, actor, tmp_path, monkeypatch):
    user, _ = actor
    db.execute(delete(SysAuthorizationCutover).where(
        SysAuthorizationCutover.resource.like('import_%'),
        SysAuthorizationCutover.resource != 'import_inventory'))
    db.commit()
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['import_inventory'])
    monkeypatch.setattr(api, 'SessionLocal', lambda: Session(db.get_bind()))
    for recognized in (True, False):
        job = SysImportJob(created_by=user.username, mode='skip', total_files=1, status='processing')
        db.add(job)
        db.commit()
        file = tmp_path / ('purchase.xlsx' if recognized else 'unknown.xlsx')
        file.write_bytes(_purchase() if recognized else b'not-a-workbook')
        api._process_import_job(job.id, [(str(file), file.name)], 'skip', user.username)
        current = db.get(SysImportJob, job.id, populate_existing=True)
        assert current.done_files == (1 if recognized else 0)
        assert current.error_files == (0 if recognized else 1)
        assert current.status == ('done' if recognized else 'failed')
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 1
