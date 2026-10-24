"""Whole v2 workbook and independently authorized produced error artifacts."""
from datetime import date
import json

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.authz import runtime
from app.config import get_settings
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.security import UserContext
from app.services import authorization_artifacts as artifacts
from app.services.maintenance_project_workbook_adapter import MaintenanceProjectWorkbookAdapter
from tests.test_authz_s3_checklist import setup, cerbos_url  # noqa: F401

HMAC_KEY = b'synthetic-project-artifact-contract-key'


@pytest.fixture
def roundtrip(db, setup, monkeypatch):
    uid, identity, pid, client, headers = setup
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources',
        ['maintenance_acceptance', 'maintenance_project_roundtrip'])
    runtime.activate_configured_resources(db.get_bind())
    grant = SysAuthorizationGrant(user_id=uid, resource='maintenance_project_roundtrip',
        actions=['download_v2', 'validate_v2', 'apply_v2'], scope='all_projects',
        field_permissions=['data_purchase_cost', 'data_profit', 'data_customer'], version=1)
    db.add(grant)
    db.commit()
    ctx = UserContext(role='readonly', user_id=identity.username, is_authenticated=True, permissions={})
    adapter = MaintenanceProjectWorkbookAdapter(db, user_ctx=ctx, operator='forged',
        as_of=date(2026, 8, 9), identity=identity)
    return uid, identity, pid, grant.id, adapter


def test_producer_validate_grant_can_publish_without_consumer_error_download(db, roundtrip):
    uid, identity, pid, gid, adapter = roundtrip
    grant = db.get(SysAuthorizationGrant, gid)
    grant.actions = ['validate_v2']
    db.commit()
    validation, issues, bid = adapter.validate(pid, b'not an xlsx', hmac_key=HMAC_KEY)
    assert validation is None
    db.commit()
    row = db.get(SysAuthorizationArtifact, ('project_workbook_validation', bid))
    assert row.owner_user_id == uid and row.artifact_manifests['error_download']['complete'] is True
    with pytest.raises(HTTPException) as denied:
        adapter.load_error_workbook(bid)
    assert denied.value.status_code == 403
    grant = db.get(SysAuthorizationGrant, gid)
    grant.actions = ['error_download_v2']
    grant.version += 1
    db.commit()
    # Current consumer grant works after producer permission is withdrawn.
    content = adapter.load_error_workbook(bid)
    assert content[:2] == b'PK'


def test_download_v2_requires_customer_field_in_same_resource(db, roundtrip):
    _, _, pid, gid, adapter = roundtrip
    grant = db.get(SysAuthorizationGrant, gid)
    grant.field_permissions = ['data_purchase_cost', 'data_profit']
    db.commit()
    with pytest.raises(HTTPException) as denied:
        adapter.export(pid, hmac_key=HMAC_KEY)
    assert denied.value.status_code == 403


def test_complete_roundtrip_export_validate_and_noop_apply(db, roundtrip):
    _, _, pid, _, adapter = roundtrip
    artifact = adapter.export(pid, hmac_key=HMAC_KEY)
    db.commit()
    # New adapter models the next request rather than reusing cached row locks.
    adapter = MaintenanceProjectWorkbookAdapter(db, user_ctx=adapter.user_ctx, operator=adapter.operator,
        as_of=adapter.as_of, identity=adapter.identity)
    validation, issues, bid = adapter.validate(pid, artifact.content, hmac_key=HMAC_KEY)
    assert validation is not None and not issues
    version = adapter._state(pid).data_version
    db.commit()
    adapter = MaintenanceProjectWorkbookAdapter(db, user_ctx=adapter.user_ctx, operator=adapter.operator,
        as_of=adapter.as_of, identity=adapter.identity)
    result, state = adapter.apply_validation(pid, bid, data_version=version)
    db.commit()
    assert result.status == 'applied' and result.created == 0


def test_producer_cannot_borrow_actual_permission_from_different_parent(db, roundtrip, monkeypatch):
    from dataclasses import replace
    from app.models.maintenance_project import MaintenanceProject
    from app.authz import service
    from app.services import maintenance_authorization as foundation
    from app.services import maintenance_workflow_authorization as authorization
    _, identity, pid, _, adapter = roundtrip
    _, _, bid = adapter.validate(pid, b'not xlsx', hmac_key=HMAC_KEY)
    db.commit()
    other = MaintenanceProject(project_id='unrelated-report-parent', project_code='UNRELATED-REPORT',
        display_name='unrelated project', lifecycle_status='ongoing')
    db.add(other)
    db.commit()
    original = artifacts._DOMAINS['project_workbook_validation']
    def wrong_parent(session, actor, ref, **kwargs):
        document = original.document_access(session, actor, ref, **kwargs)
        if kwargs['entry_id'] != authorization.PROJECT_WORKBOOK_VALIDATE:
            return document
        from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation
        row = session.get(MaintenanceProjectWorkbookValidation, bid)
        batch = foundation.resolve_entry_context(session, actor, entry_id=authorization.PROJECT_WORKBOOK_VALIDATE,
            params={'project_id': other.project_id, 'file_sha256': row.file_sha256},
            locked_user=kwargs.get('locked_user'))
        accesses = service.context_accesses(session, actor, batch, locked_user=kwargs.get('locked_user'))
        authorization.require_complete(accesses)
        return replace(document, accesses=tuple(accesses.accesses.values()))
    monkeypatch.setitem(artifacts._DOMAINS, 'project_workbook_validation', replace(original, document_access=wrong_parent))
    target = artifacts.ArtifactTarget('project_workbook_validation', bid, 1)
    binding = artifacts.load_artifact_binding(db, target=target)
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation
    content = bytes(db.get(MaintenanceProjectWorkbookValidation, bid).error_workbook)
    with pytest.raises(HTTPException) as denied:
        artifacts.publish_artifact(db, identity, binding=binding, purpose='error_download', content=content,
            expected_generation=1)
    assert denied.value.status_code == 403
    db.rollback()


def test_real_http_download_validate_apply_and_private_errors(db, roundtrip):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import maintenance_project_workbooks as api
    from app.auth import _make_token
    from app.db import get_db
    _, identity, pid, gid, _ = roundtrip
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    token, _ = _make_token('readonly', identity.username, None, token_version=0, authn='sys_user')
    client = TestClient(app)
    client.headers['Authorization'] = 'Bearer ' + token
    base = f'/api/maintenance/projects/stable/{pid}/workbook'
    view = client.get(base + "?view=authorization")
    assert view.status_code == 200, view.text
    assert set(view.json()) == {"locator", "authorization_context"}
    assert view.json()["locator"] == {"project_id": pid}
    downloaded = client.get(base)
    assert downloaded.status_code == 200, downloaded.text
    validated = client.post(base + '/validate', files={'file': ('workbook.xlsx', downloaded.content)})
    assert validated.status_code == 200, validated.text
    value = validated.json()
    apply_view = client.post(base + '/apply?view=authorization', json={'validation_token': value['validation_token'], 'data_version': value['data_version']})
    assert apply_view.status_code == 200, apply_view.text
    assert set(apply_view.json()) == {'locator', 'authorization_context'}
    assert apply_view.json()['locator'] == {'project_id': pid}
    applied = client.post(base + '/apply', json={'validation_token': value['validation_token'],
                                                'data_version': value['data_version']})
    assert applied.status_code == 200, applied.text
    errors = client.post(base + '/validate', files={'file': ('broken.xlsx', b'not xlsx')})
    assert errors.status_code == 200, errors.text
    url = '/api/maintenance/workbook-validations/' + errors.json()['validation_token'] + '/errors.xlsx'
    assert client.get(url).status_code == 403
    grant = db.get(SysAuthorizationGrant, gid)
    grant.actions = ['error_download_v2']
    grant.version += 1
    db.commit()
    error_view = client.get(url, params={'view': 'authorization'})
    assert error_view.status_code == 200, error_view.text
    assert set(error_view.json()) == {'locator', 'authorization_context'}
    resolution = error_view.json()['authorization_context']
    precondition = {key: resolution[key] for key in ('catalog_version', 'authorization_epoch', 'context')}
    output = client.get(url, params={'authorization_precondition': json.dumps(precondition)})
    assert output.status_code == 200, output.text
    precondition['context']['authorization_revision'] = 'old-observation'
    stale = client.get(url, params={'authorization_precondition': json.dumps(precondition)})
    assert stale.status_code == 409, stale.text
    assert stale.json()['detail']['code'] == 'AUTHZ_CONTEXT_STALE'
    assert client.get(url, params={'view': 'authorization', 'authorization_precondition': json.dumps(precondition)}).status_code == 200
    assert output.content[:2] == b'PK'


@pytest.mark.parametrize("missing", ["action", "field"])
def test_producer_whole_contract_rejects_missing_requirement(db, roundtrip, missing):
    _, identity, pid, gid, adapter = roundtrip
    _, _, bid = adapter.validate(pid, b'not xlsx', hmac_key=HMAC_KEY)
    db.commit()
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation
    content = bytes(db.get(MaintenanceProjectWorkbookValidation, bid).error_workbook)
    grant = db.get(SysAuthorizationGrant, gid)
    grant.actions = ['validate_v2'] if missing == 'field' else ['download_v2']
    grant.field_permissions = ['data_purchase_cost', 'data_profit'] if missing == 'field' else ['data_purchase_cost', 'data_profit', 'data_customer']
    grant.version += 1
    db.commit()
    binding = artifacts.load_artifact_binding(db,
        target=artifacts.ArtifactTarget('project_workbook_validation', bid, 1))
    with pytest.raises(HTTPException) as denied:
        artifacts.publish_artifact(db, identity, binding=binding, purpose='error_download',
            content=content, expected_generation=1)
    assert denied.value.status_code == 403
    db.rollback()
