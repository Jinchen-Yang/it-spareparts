"""Legacy workbook workers serialize their entire transaction with first cutover."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.api import maintenance_project_workbooks as project_api
from app.api import maintenance_manager_workbooks as monthly_api
from app.authz import runtime
from app.config import get_settings
from app.models.authorization import SysAuthorizationCutover
from app.models.maintenance_manager import MaintenanceManagerUploadBatch
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation
from app.security import UserContext
from app.services.maintenance_manager_workbook_adapter import MaintenanceManagerWorkbookAdapter
from app.services.maintenance_project_workbook_adapter import MaintenanceProjectWorkbookAdapter
from tests.test_maintenance_manager_workbook_v3_adapter import _seed


@pytest.fixture(params=['project', 'monthly'])
def work(db, monkeypatch, tmp_path, request):
    settings = get_settings()
    for key, value in dict(authorization_mode='unified_modules', authz_enabled_resources=[],
                           authz_suspended_resources=[], authz_sales_analysis_enabled=False,
                           authz_sales_analysis_suspended=False).items():
        monkeypatch.setattr(settings, key, value)
    user, project_id, _ = _seed(db)
    user.role = 'admin'
    db.commit()
    ctx = UserContext(role='admin', user_id=user.username, is_authenticated=True,
                      permissions={'page_maintenance': True, 'data_purchase_cost': True,
                                   'data_profit': True, 'data_customer': True})
    api = project_api if request.param == 'project' else monthly_api
    cls = MaintenanceProjectWorkbookAdapter if request.param == 'project' else MaintenanceManagerWorkbookAdapter
    model = MaintenanceProjectWorkbookValidation if request.param == 'project' else MaintenanceManagerUploadBatch
    monkeypatch.setattr(api, 'business_today', lambda: date(2026, 8, 9))
    monkeypatch.setattr(api, 'SessionLocal', lambda: Session(db.get_bind()))
    key = b'synthetic-workbook-worker-cutover-key'
    adapter = cls(db, user_ctx=ctx, operator=user.username, as_of=date(2026, 8, 9))
    if request.param == 'project':
        artifact = adapter.export(project_id, hmac_key=key)
        invoke = api._validate_project_workbook_in_worker
        args = dict(project_id=project_id, original_name='queued.xlsx')
    else:
        artifact, _ = adapter.export(date(2026, 8, 1), hmac_key=key)
        invoke = api._validate_in_worker
        args = dict(report_month=date(2026, 8, 1))
    db.commit()
    path = tmp_path / 'queued.xlsx'
    path.write_bytes(artifact.content)
    args.update(upload_path=str(path), ident={'sub': user.username, 'role': 'admin', 'authn': 'sys_user', 'fb': False}, ctx=ctx, hmac_key=key)
    return api, cls, model, invoke, args


@pytest.mark.parametrize('withdraw', [False, True])
def test_queued_workbook_rejects_cutover_before_cleanup_identity_or_file_reads(db, work, monkeypatch, withdraw):
    api, _, model, invoke, args = work
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [api._RESOURCE])
    runtime.activate_configured_resources(db.get_bind())
    if withdraw:
        monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [])
        monkeypatch.setattr(get_settings(), 'authorization_mode', 'legacy_compat')
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    # A vanished upload must not be opened before the persistent cutover guard.
    from pathlib import Path
    Path(args['upload_path']).unlink()
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        with pytest.raises(HTTPException) as rejected:
            invoke(**args)
        assert rejected.value.status_code == 503
        assert rejected.value.detail['code'] == 'AUTHZ_LEGACY_EXECUTION_RETIRED'
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert not any(table in statement for statement in statements for table in (
        'maintenance_project_workbook_validation', 'maintenance_manager_upload_batch',
        'maintenance_project_contract', 'maintenance_project_user_assignment', 'sys_user'))
    assert db.scalar(select(func.count()).select_from(model)) == 0


def test_never_cutover_worker_validates_real_signed_workbook(db, work):
    _, _, model, invoke, args = work
    result = invoke(**args)
    assert result['can_apply'] is True
    assert result['errors'] == []
    assert db.scalar(select(func.count()).select_from(model)) == 1


def test_first_activation_waits_until_real_validation_commit(db, work, monkeypatch):
    api, cls, model, invoke, args = work
    staged, release, attempting = Event(), Event(), Event()
    observed = {}
    original = cls.validate
    def pause(adapter, *values, **options):
        result = original(adapter, *values, **options)
        adapter.db.flush()
        observed['worker_pid'] = adapter.db.connection().connection.driver_connection.info.backend_pid
        staged.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(cls, 'validate', pause)
    def on_lock(conn, cursor, statement, parameters, context, executemany):
        if 'pg_advisory_xact_lock(' in statement:
            observed['activation_pid'] = conn.connection.driver_connection.info.backend_pid
            attempting.set()
    with ThreadPoolExecutor(max_workers=2) as workers:
        worker = workers.submit(invoke, **args)
        try:
            assert staged.wait(10), worker.exception(timeout=1) if worker.done() else 'worker never staged'
            event.listen(db.get_bind(), 'before_cursor_execute', on_lock)
            monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [api._RESOURCE])
            activation = workers.submit(runtime.activate_configured_resources, db.get_bind())
            assert attempting.wait(5)
            deadline = time.monotonic() + 5
            blockers = []
            while time.monotonic() < deadline:
                with Session(db.get_bind()) as observer:
                    blockers = observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['activation_pid']})
                if observed['worker_pid'] in blockers or activation.done():
                    break
                Event().wait(0.01)
            assert observed['worker_pid'] in blockers, 'activation overtook legacy workbook transaction'
            with Session(db.get_bind()) as observer:
                assert observer.scalar(select(func.count()).select_from(model)) == 0
                assert observer.get(SysAuthorizationCutover, api._RESOURCE) is None
        finally:
            release.set()
            if event.contains(db.get_bind(), 'before_cursor_execute', on_lock):
                event.remove(db.get_bind(), 'before_cursor_execute', on_lock)
        assert worker.result(timeout=10)['can_apply'] is True
        activation.result(timeout=10)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(model)) == 1
    assert db.get(SysAuthorizationCutover, api._RESOURCE) is not None


@pytest.mark.parametrize('work', ['project'], indirect=True)
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_cutover_after_cleanup_transaction_is_rechecked_before_workbook_access(db, work, monkeypatch, cleanup_fails):
    api, _, model, invoke, args = work
    original = api._run_validation_retention_cleanup
    if cleanup_fails:
        def broken_cleanup(writer):
            writer.execute(text('SELECT 1 / 0'))
        monkeypatch.setattr(api, 'cleanup_project_workbook_validations', broken_cleanup)
    def cleanup_then_cutover(writer):
        original(writer)
        assert not writer.in_transaction(), 'cleanup must release old validation-row locks'
        monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [api._RESOURCE])
        runtime.activate_configured_resources(db.get_bind())
    monkeypatch.setattr(api, '_run_validation_retention_cleanup', cleanup_then_cutover)
    # Failure to reacquire the guard would open this file (and first load SysUser).
    from pathlib import Path
    Path(args['upload_path']).unlink()
    with pytest.raises(HTTPException) as rejected:
        invoke(**args)
    assert rejected.value.status_code == 503
    assert rejected.value.detail['code'] == 'AUTHZ_LEGACY_EXECUTION_RETIRED'
    assert db.scalar(select(func.count()).select_from(model)) == 0
