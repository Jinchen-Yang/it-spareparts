"""Monthly workbooks keep complete live primary-manager scope and stable identity."""
from datetime import date
from types import MappingProxyType

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.authz import runtime
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant, SysAuthorizationArtifact
from app.models.maintenance_manager import MaintenanceManagerUploadBatch
from app.models.system import SysUser
from app.services.maintenance_manager_workbook_adapter import (
    MaintenanceManagerWorkbookAdapter, read_monthly_workbook, validate_monthly_workbook)
from tests.test_maintenance_manager_workbook_v3_adapter import _seed, _ctx, _set_plan, HMAC_KEY
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401

MONTH = date(2026, 8, 1)
AS_OF = date(2026, 8, 9)


@pytest.fixture
def setup(db, monkeypatch, cerbos_url):
    settings = get_settings()
    for key, value in {'authorization_mode': 'unified_modules',
        'authz_enabled_resources': ['maintenance_manager_workbook'], 'authz_suspended_resources': [],
        'authz_sales_analysis_enabled': False, 'authz_sales_analysis_suspended': False,
        'cerbos_url': cerbos_url, 'cerbos_timeout_seconds': 2.0}.items():
        monkeypatch.setattr(settings, key, value)
    runtime.activate_configured_resources(db.get_bind())
    user, pid, cid = _seed(db)
    user.role = 'readonly'
    db.add(SysAuthorizationGrant(user_id=user.id, resource='maintenance_manager_workbook',
        actions=['download', 'job_read', 'validate', 'apply'], scope='primary_managed_active_projects',
        field_permissions=['data_profit'], version=1))
    db.commit()
    identity = VerifiedIdentity(user.username, user.token_version, False, 'sys_user', MappingProxyType({}), user.id)
    return user.id, identity, pid, cid, _ctx(user), sessionmaker(bind=db.get_bind())


def test_current_full_workbook_validate_apply_and_replay_are_bound_to_real_batch(db, setup):
    uid, identity, pid, cid, ctx, factory = setup
    artifact, _ = read_monthly_workbook(factory, identity, MONTH, ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    validation, values = validate_monthly_workbook(factory, identity, MONTH, _set_plan(artifact.content, cid),
        ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    assert validation.can_apply
    bid = values['batch_id']
    assert db.get(SysAuthorizationArtifact, ('manager_upload_batch', bid)).owner_user_id == uid
    adapter = MaintenanceManagerWorkbookAdapter(db, user_ctx=ctx, operator='forged', as_of=AS_OF, identity=identity)
    result = adapter.apply(bid, data_version=values['data_version'])
    db.commit()
    assert result['changed_rows'] == 1
    assert db.get(MaintenanceManagerUploadBatch, bid).applied_by == identity.username
    assert adapter.apply(bid, data_version=values['data_version']) == result
    db.rollback()


def test_same_stable_id_rename_does_not_invalidate_signed_owner(db, setup):
    uid, identity, _, cid, ctx, factory = setup
    artifact, _ = read_monthly_workbook(factory, identity, MONTH, ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    user = db.get(SysUser, uid)
    user.username = 'monthly-renamed'
    db.commit()
    renamed = VerifiedIdentity(user.username, user.token_version, False, 'sys_user', MappingProxyType({}), uid)
    validation, values = validate_monthly_workbook(factory, renamed, MONTH, _set_plan(artifact.content, cid),
        ctx=_ctx(user), as_of=AS_OF, hmac_key=HMAC_KEY)
    assert validation.can_apply and values['owner_user_id'] == uid


def test_download_action_never_authorizes_validation_or_applied_history(db, setup):
    uid, identity, _, cid, ctx, factory = setup
    artifact, _ = read_monthly_workbook(factory, identity, MONTH, ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == uid))
    grant.actions = ['download']
    grant.version += 1
    db.commit()
    with pytest.raises(HTTPException) as denied:
        validate_monthly_workbook(factory, identity, MONTH, _set_plan(artifact.content, cid),
            ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    assert denied.value.status_code == 403
    assert not db.scalars(select(MaintenanceManagerUploadBatch)).all()


def test_foreign_admin_even_explicit_full_actions_has_no_managed_scope(db, setup):
    _, _, _, _, ctx, factory = setup
    user = SysUser(username='monthly-admin-no-project', role='admin', password_hash='synthetic')
    db.add(user)
    db.flush()
    db.add(SysAuthorizationGrant(user_id=user.id, resource='maintenance_manager_workbook',
        actions=['download', 'job_read'], scope='primary_managed_active_projects',
        field_permissions=['data_profit'], version=1))
    db.commit()
    identity = VerifiedIdentity(user.username, 0, False, 'sys_user', MappingProxyType({}), user.id)
    with pytest.raises(HTTPException) as denied:
        read_monthly_workbook(factory, identity, MONTH, ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    assert denied.value.status_code == 403


def test_actual_http_routes_use_typed_worker_identity_and_whole_file_gate(db, setup, monkeypatch):
    uid, identity, _, _, _, _ = setup
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import maintenance_manager_workbooks as api
    from app.auth import _make_token
    from app.db import get_db
    monkeypatch.setattr(api, 'business_today', lambda: AS_OF)
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    token, _ = _make_token('readonly', identity.username, None, token_version=0, authn='sys_user')
    headers = {'Authorization': 'Bearer ' + token}
    client = TestClient(app)
    resolution = client.get('/api/maintenance/project-manager/workbooks/v3?report_month=2026-08&view=authorization', headers=headers)
    assert resolution.status_code == 200, resolution.text
    assert set(resolution.json()) == {'locator', 'authorization_context'}
    assert set(resolution.json()['locator']) == {'collection_ref'}
    response = client.get('/api/maintenance/project-manager/workbooks/v3?report_month=2026-08', headers=headers)
    assert response.status_code == 200, response.text
    assert response.content[:2] == b'PK'
    response = client.post('/api/maintenance/project-manager/workbooks/v3/validate?report_month=2026-08',
        headers=headers, files={'file': ('monthly.xlsx', response.content)})
    assert response.status_code == 200, response.text
    bid = response.json()['batch_id']
    assert db.get(SysAuthorizationArtifact, ('manager_upload_batch', bid)).owner_user_id == uid
    response = client.get('/api/maintenance/project-manager/workbooks/v3/status?report_month=2026-08', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['latest_batch']['batch_id'] == bid


def test_corrupted_persisted_plan_cannot_borrow_original_producer_binding(db, setup):
    _, identity, _, cid, ctx, factory = setup
    artifact, _ = read_monthly_workbook(factory, identity, MONTH, ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    _, values = validate_monthly_workbook(factory, identity, MONTH, _set_plan(artifact.content, cid),
        ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    row = db.get(MaintenanceManagerUploadBatch, values['batch_id'])
    from copy import deepcopy
    plan = deepcopy(row.plan_json)
    plan['milestone_changes'][0]['planned_amount'] = '999999'
    row.plan_json = plan
    db.commit()
    adapter = MaintenanceManagerWorkbookAdapter(db, user_ctx=ctx, operator=identity.username, as_of=AS_OF, identity=identity)
    with pytest.raises(HTTPException) as denied:
        adapter.apply(row.batch_id, data_version=row.data_version)
    assert denied.value.status_code == 403
    db.rollback()
    assert db.get(MaintenanceManagerUploadBatch, row.batch_id).status == 'valid'


def test_scope_expansion_after_validation_cannot_replay_old_project_plan(db, setup):
    uid, identity, _, cid, ctx, factory = setup
    artifact, _ = read_monthly_workbook(factory, identity, MONTH, ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    _, values = validate_monthly_workbook(factory, identity, MONTH, _set_plan(artifact.content, cid),
        ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectUserAssignment
    from datetime import datetime, timezone
    project = MaintenanceProject(project_id='new-managed-project', project_code='NEW-MONTHLY',
        display_name='new current scope', lifecycle_status='ongoing')
    db.add(project)
    db.flush()
    db.add(MaintenanceProjectUserAssignment(assignment_id='new-monthly-assignment',
        project_id=project.project_id, user_id=uid, responsibility_type='primary_manager',
        version=1, assigned_at=datetime.now(timezone.utc), assigned_by='synthetic', assignment_reason='scope change'))
    db.commit()
    adapter = MaintenanceManagerWorkbookAdapter(db, user_ctx=ctx, operator=identity.username, as_of=AS_OF, identity=identity)
    with pytest.raises(HTTPException) as denied:
        adapter.apply(values['batch_id'], data_version=values['data_version'])
    assert denied.value.status_code == 403
    db.rollback()
    assert db.get(MaintenanceManagerUploadBatch, values['batch_id']).status == 'valid'


def test_apply_waiting_for_project_lock_reloads_current_version(db, setup):
    """A genuine row-lock wait must not retain the pre-lock ORM scope snapshot."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from sqlalchemy import event, text
    from app.models.maintenance_project import MaintenanceProject
    from app.services.maintenance_manager_workbook_adapter import ManagerWorkbookConflict

    _, identity, pid, cid, ctx, factory = setup
    artifact, _ = read_monthly_workbook(factory, identity, MONTH, ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    _, values = validate_monthly_workbook(factory, identity, MONTH, _set_plan(artifact.content, cid),
        ctx=ctx, as_of=AS_OF, hmac_key=HMAC_KEY)
    db.rollback()
    waiting = threading.Event()
    worker_pid = []
    engine = db.get_bind()

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if ('FROM maintenance_project ' in statement and 'FOR UPDATE' in statement
                and 'JOIN' not in statement and threading.current_thread().name.startswith('monthly-wait')):
            worker_pid[:] = [conn.connection.driver_connection.info.backend_pid]
            waiting.set()

    def apply():
        with factory() as worker:
            adapter = MaintenanceManagerWorkbookAdapter(worker, user_ctx=ctx, operator=identity.username,
                as_of=AS_OF, identity=identity)
            try:
                result = adapter.apply(values['batch_id'], data_version=values['data_version'])
                worker.commit()
                return result
            except Exception:
                worker.rollback()
                raise

    event.listen(engine, 'before_cursor_execute', before_execute)
    try:
        with factory() as writer, ThreadPoolExecutor(max_workers=1, thread_name_prefix='monthly-wait') as pool:
            project = writer.scalar(select(MaintenanceProject).where(
                MaintenanceProject.project_id == pid).with_for_update())
            project.version += 1
            project.display_name = 'concurrently updated project'
            writer.flush()
            future = pool.submit(apply)
            try:
                assert waiting.wait(5)
                deadline = time.monotonic() + 5
                blocked = False
                with engine.connect() as observer:
                    while time.monotonic() < deadline:
                        blocked = observer.scalar(text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                            {'pid': worker_pid[0]})
                        if blocked:
                            break
                        observer.commit()
                        time.sleep(0.01)
                assert blocked, 'test must observe an actual PostgreSQL lock wait'
            finally:
                writer.commit()
            with pytest.raises(ManagerWorkbookConflict):
                future.result(timeout=10)
    finally:
        event.remove(engine, 'before_cursor_execute', before_execute)
    db.expire_all()
    assert db.get(MaintenanceManagerUploadBatch, values['batch_id']).status == 'valid'
