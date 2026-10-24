"""Actual acceptance bytes, stable users, SQL scopes and independent actions."""
from datetime import datetime, timezone
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import maintenance_acceptance as api
from app.auth import _make_token
from app.authz import runtime
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.maintenance_manager import BusinessFile, BusinessFileDownloadAudit, MaintenanceAcceptanceOperation
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.system import SysUser
from app.services import maintenance_acceptance as business
from tests.test_authz_s3_checklist import setup, cerbos_url  # noqa: F401
from tests.boss_board_helpers import make_project


@pytest.fixture
def acceptance(db, setup, monkeypatch, tmp_path):
    uid, identity, pid, _, headers = setup
    settings = get_settings()
    monkeypatch.setattr(settings, 'raw_file_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'authz_enabled_resources', ['maintenance_acceptance', 'maintenance_acceptance_directory'])
    runtime.activate_configured_resources(db.get_bind())
    row = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == uid))
    row.actions = ['attachment_upload']
    row.version += 1
    db.commit()
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.headers.update(headers)
    return uid, pid, client, row


def upload(client, pid, *, key='upload-key', content=b'canonical acceptance binary'):
    return client.post(f'/api/maintenance/projects/stable/{pid}/acceptance/attachments',
        headers={'Idempotency-Key': key}, files={'file': ('report.bin', content, 'application/octet-stream')})


def change(db, row, actions):
    row.actions = actions
    row.version += 1
    db.commit()


def test_upload_is_independent_and_creates_stable_file_and_operation(db, acceptance):
    uid, pid, client, grant = acceptance
    response = upload(client, pid)
    assert response.status_code == 200, response.text
    fid = response.json()['file_id']
    assert db.get(BusinessFile, fid).owner_user_id == uid
    binding = db.get(SysAuthorizationArtifact, ('business_file', fid))
    assert binding.owner_user_id == uid and binding.artifact_manifests['attachment_download']['complete'] is True
    assert db.scalar(select(MaintenanceAcceptanceOperation)).owner_user_id == uid
    assert client.get(f'/api/maintenance/acceptance-files/{fid}').status_code == 403
    assert client.get(f'/api/maintenance/projects/stable/{pid}/acceptance').status_code == 403
    assert upload(client, pid).json()['replayed'] is True
    change(db, grant, [])
    assert upload(client, pid).status_code == 403
    assert db.scalar(select(func.count()).select_from(BusinessFile)) == 1


def test_download_only_reader_of_same_project_gets_complete_verified_bytes(db, acceptance):
    _, pid, client, grant = acceptance
    response = upload(client, pid)
    assert response.status_code == 200, response.text
    fid = response.json()['file_id']
    change(db, grant, ['attachment_download'])
    url = f'/api/maintenance/acceptance-files/{fid}'
    view = client.get(url, params={'view': 'authorization'})
    assert view.status_code == 200, view.text
    assert view.headers['cache-control'] == 'no-store'
    ctx = view.json()['authorization_context']
    assert view.json()['locator'] == {'object_type': 'business_file', 'object_id': fid}
    pre = {key: ctx[key] for key in ('catalog_version', 'authorization_epoch', 'context')}
    response = client.get(url, params={'authorization_precondition': json.dumps(pre)})
    assert response.status_code == 200, response.text
    assert response.content == b'canonical acceptance binary'
    assert response.headers['content-security-policy'] == 'sandbox'
    assert db.scalar(select(func.count()).select_from(BusinessFileDownloadAudit)) == 1
    pre['context']['authorization_revision'] = 'stale'
    assert client.get(url, params={'authorization_precondition': json.dumps(pre)}).status_code == 409
    assert db.scalar(select(func.count()).select_from(BusinessFileDownloadAudit)) == 1
    file = db.get(BusinessFile, fid)
    business._resolved_object_path(file.object_key).write_bytes(b'forged same-looking contents')
    assert client.get(url).status_code == 409
    assert db.scalar(select(func.count()).select_from(BusinessFileDownloadAudit)) == 1


def test_reader_is_not_limited_to_uploader_but_needs_current_project_scope(db, acceptance):
    _, pid, client, _ = acceptance
    result = upload(client, pid)
    assert result.status_code == 200, result.text
    fid = result.json()['file_id']
    reader = SysUser(username='canonical-reader', role='admin', password_hash='synthetic')
    db.add(reader)
    db.flush()
    db.add(SysAuthorizationGrant(user_id=reader.id, resource='maintenance_acceptance',
        actions=['attachment_download'], scope='assigned_or_sales_projects', field_permissions=[], version=1))
    db.commit()
    token, _ = _make_token(reader.role, reader.username, None, token_version=reader.token_version, authn='sys_user')
    client.headers['Authorization'] = 'Bearer ' + token
    url = f'/api/maintenance/acceptance-files/{fid}'
    assert client.get(url).status_code == 403
    assignment = MaintenanceProjectUserAssignment(assignment_id='file-reader-project', project_id=pid, user_id=reader.id,
        responsibility_type='primary_manager', assigned_by='synthetic', assignment_reason='scoped reader')
    db.add(assignment)
    db.commit()
    assert client.get(url).status_code == 200
    assignment.archived_at = datetime.now(timezone.utc)
    assignment.archived_by = 'synthetic'
    assignment.archive_reason = 'revoke'
    assignment.version += 1
    db.commit()
    assert client.get(url).status_code == 403


def test_stable_owner_replay_survives_rename_and_name_reuse_cannot_claim(db, acceptance):
    uid, pid, client, grant = acceptance
    first = upload(client, pid)
    assert first.status_code == 200, first.text
    owner = db.get(SysUser, uid)
    old_name = owner.username
    owner.username = 'acceptance-renamed'
    db.commit()
    token, _ = _make_token(owner.role, owner.username, None, token_version=owner.token_version, authn='sys_user')
    client.headers['Authorization'] = 'Bearer ' + token
    assert upload(client, pid).json()['file_id'] == first.json()['file_id']
    assert db.scalar(select(func.count()).select_from(MaintenanceAcceptanceOperation)) == 1
    replacement = SysUser(username=old_name, role='admin', password_hash='synthetic')
    db.add(replacement)
    db.commit()
    token, _ = _make_token(replacement.role, replacement.username, None, token_version=replacement.token_version, authn='sys_user')
    client.headers['Authorization'] = 'Bearer ' + token
    assert upload(client, pid).status_code == 403
    assert db.get(BusinessFile, first.json()['file_id']).owner_user_id == uid


def test_submit_and_delete_require_their_own_action_and_actual_parent(db, acceptance):
    uid, pid, client, grant = acceptance
    first = upload(client, pid)
    assert first.status_code == 200, first.text
    fid = first.json()['file_id']
    base = f'/api/maintenance/projects/stable/{pid}/acceptance'
    submit = lambda: client.post(base + '/submit', headers={'Idempotency-Key': 'submit-key'}, json={'expected_version': 2})
    assert submit().status_code == 403
    change(db, grant, ['submit'])
    assert submit().status_code == 200
    assert submit().json()['replayed'] is True
    assert client.delete(base + '/attachments/' + fid).status_code == 403
    change(db, grant, ['attachment_delete'])
    other = make_project(db, 'wrong-file-parent')
    assert client.delete(f'/api/maintenance/projects/stable/{other.project_id}/acceptance/attachments/{fid}').status_code == 403
    assert client.delete(base + '/attachments/' + fid).status_code == 409  # Last submitted evidence remains.


def test_directory_scope_precedes_total_and_does_not_grant_report(db, acceptance):
    uid, pid, client, grant = acceptance
    change(db, grant, [])
    db.add(SysAuthorizationGrant(user_id=uid, resource='maintenance_acceptance_directory', actions=['search'],
        scope='assigned_or_sales_projects', field_permissions=[], version=1))
    db.add(MaintenanceProjectUserAssignment(assignment_id='directory-project', project_id=pid, user_id=uid,
        responsibility_type='primary_manager', assigned_by='synthetic', assignment_reason='directory scope'))
    db.commit()
    make_project(db, 'unrelated-report-project')
    url = '/api/maintenance/acceptance-deliverables/search'
    view = client.post(url + '?view=authorization', json={})
    assert view.status_code == 200, view.text
    result = client.post(url, json={'page': 1, 'page_size': 1})
    assert result.status_code == 200, result.text
    assert result.json()['total'] == 1 and result.json()['rows'][0]['project_id'] == pid
    assert client.get(f'/api/maintenance/projects/stable/{pid}/acceptance').status_code == 403


def test_legacy_file_is_not_claimed_and_fresh_upload_publishes_new_evidence(db, acceptance):
    uid, pid, client, grant = acceptance
    old, _ = business.upload_attachment(db, project_id=pid, operator='checklist-owner', client_key='old-key',
        filename='report.bin', mime_type='application/octet-stream', content=b'canonical acceptance binary')
    db.commit()
    change(db, grant, ['attachment_download', 'attachment_upload'])
    assert client.get('/api/maintenance/acceptance-files/' + old['file_id']).status_code == 403
    new = upload(client, pid)
    assert new.status_code == 200, new.text
    assert new.json()['file_id'] != old['file_id']
    assert db.get(BusinessFile, old['file_id']).owner_user_id is None
    assert db.get(BusinessFile, new.json()['file_id']).owner_user_id == uid
    assert client.get('/api/maintenance/acceptance-files/' + new.json()['file_id']).status_code == 200


def test_bytes_materialize_in_read_only_snapshot_then_separate_append_audit(db, acceptance, monkeypatch):
    _, pid, client, grant = acceptance
    result = upload(client, pid)
    assert result.status_code == 200, result.text
    change(db, grant, ['attachment_download'])
    observed = []
    original = business.controlled_download
    from sqlalchemy import text
    def read(snapshot, **kwargs):
        observed.append((snapshot.scalar(text('SHOW transaction_isolation')), snapshot.scalar(text('SHOW transaction_read_only'))))
        assert kwargs['audit'] is False
        return original(snapshot, **kwargs)
    monkeypatch.setattr(business, 'controlled_download', read)
    response = client.get('/api/maintenance/acceptance-files/' + result.json()['file_id'])
    assert response.status_code == 200, response.text
    assert observed == [('repeatable read', 'on')]
    assert db.scalar(select(func.count()).select_from(BusinessFileDownloadAudit)) == 1


def test_revoked_while_waiting_for_project_lock_writes_no_file_or_receipt(db, acceptance):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from types import MappingProxyType
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    from fastapi import HTTPException
    from app.authz.principal import VerifiedIdentity
    from app.models.maintenance_project import MaintenanceProject
    from app.services import maintenance_acceptance_authorization as protected
    uid, pid, _, _ = acceptance
    owner = db.get(SysUser, uid)
    identity = VerifiedIdentity(owner.username, owner.token_version, False, 'sys_user', MappingProxyType({}), uid)
    db.rollback()
    def create():
        with Session(db.get_bind()) as worker:
            try:
                protected.command(worker, identity, entry_id=protected.UPLOAD, params={'project_id': pid},
                    client_key='waiting-key', filename='report.bin', mime_type='application/octet-stream', content=b'wait')
                worker.commit()
                return 200
            except HTTPException as exc:
                worker.rollback()
                return exc.status_code
    with Session(db.get_bind()) as blocker:
        blocker.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id == pid).with_for_update())
        blocker_pid = blocker.scalar(text('SELECT pg_backend_pid()'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(create)
            blocked = False
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline and not future.done():
                    with db.get_bind().connect() as observer:
                        blocked = observer.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))'), {'pid': blocker_pid})
                    if blocked: break
                    time.sleep(.02)
                assert blocked, 'Did not reach actual project lock'
                with Session(db.get_bind()) as administrator:
                    user = administrator.get(SysUser, uid)
                    user.token_version += 1
                    administrator.commit()
            finally:
                blocker.rollback()
            assert future.result(timeout=10) == 401
    assert db.scalar(select(func.count()).select_from(BusinessFile)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceAcceptanceOperation)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact)) == 0
    assert not [p for p in __import__('pathlib').Path(get_settings().raw_file_dir).rglob('*') if p.is_file()]


def test_main_mount_keeps_feature_gate_and_allows_canonical_without_old_beta_key(db, acceptance, monkeypatch):
    from app.main import app
    _, pid, local, _ = acceptance
    client = TestClient(app)
    client.headers.update(local.headers)
    monkeypatch.setattr(get_settings(), 'maintenance_beta_enabled', True)
    response = upload(client, pid)
    assert response.status_code == 200, response.text
    monkeypatch.setattr(get_settings(), 'maintenance_beta_enabled', False)
    assert upload(client, pid).status_code == 404


def test_denied_attachment_does_not_load_deliverable_business_details(db,acceptance):
    from sqlalchemy import event
    _,pid,client,_=acceptance
    response=upload(client,pid);assert response.status_code==200,response.text
    columns=[]
    def capture(_conn,cursor,statement,_params,_ctx,_many):
        if cursor.description:columns.extend(str(column[0]) for column in cursor.description)
    event.listen(db.get_bind(),'after_cursor_execute',capture)
    try:
        denied=client.get('/api/maintenance/acceptance-files/'+response.json()['file_id'])
        assert denied.status_code==403,denied.text
    finally:event.remove(db.get_bind(),'after_cursor_execute',capture)
    assert not any(name.endswith(('rejection_reason','submitted_by','approved_by','due_date')) for name in columns),columns
