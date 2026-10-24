"""Checklist project/owner contracts over real HTTP, PostgreSQL and Cerbos."""
from types import MappingProxyType

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import maintenance_acceptance_checklist as api
from app.auth import _make_token
from app.authz import runtime
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.maintenance_acceptance_checklist import MaintenanceAcceptanceChecklistBatch
from app.models.system import SysUser
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_acceptance_checklist as checklist
from tests.test_maintenance_acceptance_checklist import _project, _xlsx
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def setup(db, monkeypatch, cerbos_url):
    settings = get_settings()
    for name, value in {'authorization_mode': 'unified_modules',
        'authz_enabled_resources': ['maintenance_acceptance'], 'authz_suspended_resources': [],
        'authz_sales_analysis_enabled': False, 'authz_sales_analysis_suspended': False,
        'cerbos_url': cerbos_url, 'cerbos_timeout_seconds': 2.0}.items():
        monkeypatch.setattr(settings, name, value)
    runtime.activate_configured_resources(db.get_bind())
    artifacts.begin_artifact_protocol(db.get_bind())
    owner = SysUser(username='checklist-owner', role='readonly', password_hash='synthetic')
    db.add(owner)
    db.flush()
    db.add(SysAuthorizationGrant(user_id=owner.id, resource='maintenance_acceptance',
        actions=['read_checklist', 'checklist_preview', 'checklist_apply', 'template_download'],
        scope='all_projects', field_permissions=[], version=1))
    db.commit()
    project = _project(db, 'canonical-checklist-project')
    identity = VerifiedIdentity(owner.username, owner.token_version, False, 'sys_user', MappingProxyType({}), owner.id)
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    token, _ = _make_token(owner.role, owner.username, None, token_version=owner.token_version, authn='sys_user')
    return owner.id, identity, project.project_id, TestClient(app), {'Authorization': 'Bearer ' + token}


def _create(db, identity, project_id, key='canonical-request'):
    parsed = checklist.parse_checklist_workbook(_xlsx([('合成清单', '是')]), 'checklist.xlsx')
    bid = checklist.store_preview(db, parsed, project_id=project_id,
        uploaded_by='forged-audit-name', idempotency_key=key, identity=identity)
    db.commit()
    return bid


def test_preview_http_authorizes_custom_grant_without_old_page_bool_and_binds_owner(db, setup):
    uid, _, pid, client, headers = setup
    response = client.post(f'/api/maintenance/projects/stable/{pid}/acceptance/checklist/preview',
        headers={**headers, 'Idempotency-Key': 'http-preview-1'},
        files={'file': ('checklist.xlsx', _xlsx([('合成清单', '是')]))})
    assert response.status_code == 200, response.text
    bid = response.json()['batch_id']
    row = db.get(MaintenanceAcceptanceChecklistBatch, bid)
    assert row.owner_user_id == uid and row.uploaded_by == 'checklist-owner'
    assert db.get(SysAuthorizationArtifact, ('acceptance_checklist_batch', bid)).owner_user_id == uid


def test_same_actor_rename_can_apply_but_admin_other_id_cannot(db, setup):
    uid, identity, pid, _, _ = setup
    bid = _create(db, identity, pid)
    owner = db.get(SysUser, uid)
    owner.username = 'renamed-checklist-owner'
    db.commit()
    renamed = VerifiedIdentity(owner.username, owner.token_version, False, 'sys_user', MappingProxyType({}), uid)
    other = SysUser(username='different-admin', role='admin', password_hash='synthetic')
    db.add(other)
    db.flush()
    db.add(SysAuthorizationGrant(user_id=other.id, resource='maintenance_acceptance', actions=['checklist_apply'],
        scope='all_projects', field_permissions=[], version=1))
    db.commit()
    stranger = VerifiedIdentity(other.username, other.token_version, False, 'sys_user', MappingProxyType({}), other.id)
    with pytest.raises(HTTPException) as denied:
        checklist.apply_batch(db, bid, operated_by=owner.username, identity=stranger)
    assert denied.value.status_code == 403
    db.rollback()
    result = checklist.apply_batch(db, bid, operated_by='forged', identity=renamed)
    assert result['batch_id'] == bid
    assert db.get(MaintenanceAcceptanceChecklistBatch, bid).applied_by == renamed.username


def test_read_only_grant_cannot_get_apply_authority_or_write(db, setup):
    uid, identity, pid, client, headers = setup
    bid = _create(db, identity, pid)
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == uid))
    grant.actions = ['read_checklist']
    grant.version += 1
    db.commit()
    response = client.post(f'/api/maintenance/acceptance-checklist/{bid}/apply?view=authorization', headers=headers)
    assert response.status_code == 200, response.text
    decisions = response.json()['authorization_context']['resources']['maintenance_acceptance']['decisions']
    assert decisions == {'checklist_apply': {'effect': 'deny', 'reason': {'code': 'NOT_GRANTED', 'message': '未获得当前业务对象的操作权限'}}}
    assert client.post(f'/api/maintenance/acceptance-checklist/{bid}/apply', headers=headers).status_code == 403
    assert db.get(MaintenanceAcceptanceChecklistBatch, bid).status == 'pending'


def test_unknown_legacy_owner_is_not_claimed_by_same_username(db, setup):
    _, identity, pid, _, _ = setup
    row = MaintenanceAcceptanceChecklistBatch(batch_id='11111111-1111-1111-1111-111111111111',
        project_id=pid, file_hash='a' * 64, filename='old.xlsx', idempotency_key='legacy-request',
        uploaded_by=identity.username, owner_user_id=None, item_rows=0, issue_rows=0, status='pending')
    db.add(row)
    db.commit()
    with pytest.raises(HTTPException):
        checklist.apply_batch(db, row.batch_id, operated_by=identity.username, identity=identity)
    db.rollback()
    assert db.get(MaintenanceAcceptanceChecklistBatch, row.batch_id).status == 'pending'


def test_withdrawn_resource_config_cannot_fall_back_to_legacy_admin(db, setup, monkeypatch):
    _, identity, pid, client, headers = setup
    bid = _create(db, identity, pid)
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [])
    monkeypatch.setattr(get_settings(), 'authorization_mode', 'legacy_compat')
    response = client.post(f'/api/maintenance/acceptance-checklist/{bid}/apply', headers=headers)
    assert response.status_code == 503
    assert db.get(MaintenanceAcceptanceChecklistBatch, bid).status == 'pending'
