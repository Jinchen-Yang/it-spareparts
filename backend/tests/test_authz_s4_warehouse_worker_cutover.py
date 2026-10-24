"""Actual legacy warehouse worker must serialize with the first persistent cutover."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.api import maintenance_warehouse as api
from app.authz import runtime
from app.config import get_settings
from app.models.authorization import SysAuthorizationCutover
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseImportBatch as Batch,
    MaintenanceWarehouseDocument as Document,
    MaintenanceWarehouseAuditEvent as Audit,
)
from app.services import maintenance_warehouse as business
from tests.test_maintenance_warehouse_api import _approve_synthetic_contract, _seed, _content

RESOURCE = 'maintenance_warehouse_import'


@pytest.fixture
def legacy_work(db, monkeypatch):
    _approve_synthetic_contract(monkeypatch)
    _seed(db)
    settings = get_settings()
    for name, value in {'authorization_mode': 'unified_modules', 'authz_enabled_resources': [],
                        'authz_suspended_resources': [], 'authz_sales_analysis_enabled': False,
                        'authz_sales_analysis_suspended': False}.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(api, 'SessionLocal', lambda: Session(db.get_bind()))
    content = _content()
    preview = business.preview_import(content, filename='queued.xlsx', hmac_key=settings.secret_key.encode())
    assert preview['can_apply']
    return (content, 'queued.xlsx', preview['import_id'], preview['preview_token'],
            'Synthetic queued legacy warehouse import', 'legacy-warehouse-operator')


@pytest.mark.parametrize('withdraw', [False, True])
def test_queued_legacy_worker_after_cutover_does_not_read_or_write_domain_facts(db, legacy_work, monkeypatch, withdraw):
    settings = get_settings()
    monkeypatch.setattr(settings, 'authz_enabled_resources', [RESOURCE])
    runtime.activate_configured_resources(db.get_bind())
    if withdraw:
        monkeypatch.setattr(settings, 'authz_enabled_resources', [])
        monkeypatch.setattr(settings, 'authorization_mode', 'legacy_compat')
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        with pytest.raises(HTTPException) as rejected:
            api._apply_worker(*legacy_work)
        assert rejected.value.status_code == 503
        assert rejected.value.detail['code'] == 'AUTHZ_LEGACY_EXECUTION_RETIRED'
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert not any(name in statement for statement in statements for name in (
        'maintenance_warehouse_document', 'maintenance_warehouse_import_batch',
        'maintenance_warehouse_audit_event', 'f_maintenance_order', 'dim_part'))
    for model in (Batch, Document, Audit):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_never_cutover_legacy_worker_preserves_real_apply_and_idempotent_replay(db, legacy_work):
    first = api._apply_worker(*legacy_work)
    assert first['writes']['documents'] == 1
    again = api._apply_worker(*legacy_work)
    assert again['idempotent_replay'] is True
    assert all(value == 0 for value in again['writes'].values())
    assert db.scalar(select(func.count()).select_from(Batch)) == 1
    assert db.scalar(select(func.count()).select_from(Document)) == 1
    assert db.scalar(select(SysAuthorizationCutover.resource).where(SysAuthorizationCutover.resource == RESOURCE)) is None


def test_first_cutover_waits_for_real_legacy_worker_transaction_commit(db, legacy_work, monkeypatch):
    """The lock barrier spans actual facts and commit, not only an initial check."""
    staged, release, activation_waiting = Event(), Event(), Event()
    original = business.apply_import
    observed = {}
    def paused(writer, *args, **kwargs):
        observed['worker_pid'] = writer.connection().connection.driver_connection.info.backend_pid
        result = original(writer, *args, **kwargs)
        writer.flush()
        staged.set()
        assert release.wait(10), 'test failed to release warehouse transaction'
        return result
    monkeypatch.setattr(business, 'apply_import', paused)
    def lock_attempt(conn, cursor, statement, parameters, context, executemany):
        if 'pg_advisory_xact_lock(' in statement and '_shared' not in statement:
            observed['activation_pid'] = conn.connection.driver_connection.info.backend_pid
            activation_waiting.set()
    with ThreadPoolExecutor(max_workers=2) as workers:
        work = workers.submit(api._apply_worker, *legacy_work)
        assert staged.wait(10)
        event.listen(db.get_bind(), 'before_cursor_execute', lock_attempt)
        try:
            monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [RESOURCE])
            activation = workers.submit(runtime.activate_configured_resources, db.get_bind())
            assert activation_waiting.wait(5)
            deadline = time.monotonic() + 5
            blockers = []
            while time.monotonic() < deadline:
                with Session(db.get_bind()) as observer:
                    blockers = observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['activation_pid']})
                if observed['worker_pid'] in blockers or activation.done():
                    break
                Event().wait(0.01)
            assert observed['worker_pid'] in blockers, 'first cutover did not wait for the actual legacy transaction'
            assert not activation.done()
            with Session(db.get_bind()) as observer:
                assert observer.scalar(select(func.count()).select_from(Batch)) == 0
                assert observer.scalar(select(SysAuthorizationCutover.resource).where(SysAuthorizationCutover.resource == RESOURCE)) is None
        finally:
            release.set()
            event.remove(db.get_bind(), 'before_cursor_execute', lock_attempt)
        assert work.result(timeout=10)['writes']['documents'] == 1
        activation.result(timeout=10)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Batch)) == 1
    assert db.scalar(select(SysAuthorizationCutover.resource).where(SysAuthorizationCutover.resource == RESOURCE)) == RESOURCE
    # Even an idempotent old receipt is not a license to enter the old service again.
    with pytest.raises(HTTPException) as rejected:
        api._apply_worker(*legacy_work)
    assert rejected.value.detail['code'] == 'AUTHZ_LEGACY_EXECUTION_RETIRED'
