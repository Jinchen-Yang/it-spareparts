"""Actual input, stable owner, field pairing and final commit authority."""
from types import MappingProxyType

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.api import maintenance_ledger as api
from app.auth import _make_token
from app.authz import runtime
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.maintenance_ledger import MaintenanceLedgerImportBatch
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_ledger as ledger
from tests.test_maintenance_ledger import _new_ledger_workbook_bytes
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def setup(db, monkeypatch, cerbos_url):
    settings = get_settings()
    for key, value in {'authorization_mode': 'unified_modules',
        'authz_enabled_resources': ['maintenance_ledger_import'], 'authz_suspended_resources': [],
        'authz_sales_analysis_enabled': False, 'authz_sales_analysis_suspended': False,
        'cerbos_url': cerbos_url, 'cerbos_timeout_seconds': 2.0}.items():
        monkeypatch.setattr(settings, key, value)
    runtime.activate_configured_resources(db.get_bind())
    artifacts.begin_artifact_protocol(db.get_bind())
    owner = SysUser(username='ledger-owner', role='readonly', password_hash='synthetic')
    db.add(owner)
    db.flush()
    db.add(SysAuthorizationGrant(user_id=owner.id, resource='maintenance_ledger_import',
        actions=['preview', 'read_metadata', 'read_report', 'apply'],
        scope='own_imports', field_permissions=['data_profit'], version=1))
    db.commit()
    identity = VerifiedIdentity(owner.username, owner.token_version, False, 'sys_user', MappingProxyType({}), owner.id)
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    token, _ = _make_token(owner.role, owner.username, None, token_version=owner.token_version, authn='sys_user')
    return owner.id, identity, TestClient(app), {'Authorization': 'Bearer ' + token}


def _create(db, identity, key='ledger-request-one'):
    parsed = ledger.parse_ledger_workbook(_new_ledger_workbook_bytes(), 'ledger.xlsx')
    return ledger.store_preview(db, parsed, 'untrusted-name', idempotency_key=key, identity=identity)


def test_http_preview_view_has_no_staging_then_actual_preview_binds_stable_owner(db, setup):
    uid, _, client, headers = setup
    body = _new_ledger_workbook_bytes()
    kwargs = dict(headers={**headers, 'Idempotency-Key': 'http-ledger-preview'},
                  files={'file': ('ledger.xlsx', body)})
    response = client.post('/api/maintenance/ledger-imports/preview?view=authorization', **kwargs)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {'locator', 'authorization_context'}
    assert response.json()['locator']['object_type'] == 'operation_input'
    assert db.scalar(select(func.count()).select_from(MaintenanceLedgerImportBatch)) == 0
    response = client.post('/api/maintenance/ledger-imports/preview', **kwargs)
    assert response.status_code == 200, response.text
    bid = response.json()['batch_id']
    row = db.get(MaintenanceLedgerImportBatch, bid)
    assert (row.owner_user_id, row.uploaded_by) == (uid, 'ledger-owner')
    sidecar = db.get(SysAuthorizationArtifact, ('ledger_import_batch', bid))
    assert sidecar.owner_user_id == uid and sidecar.authorization_provenance is None


def test_metadata_does_not_require_report_or_profit_but_never_returns_report(db, setup):
    uid, identity, client, headers = setup
    bid = _create(db, identity)
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == uid))
    grant.actions = ['read_metadata']
    grant.field_permissions = []
    grant.version += 1
    db.commit()
    from sqlalchemy import event
    queries = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            queries.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', record)
    try:
        response = client.get('/api/maintenance/ledger-imports/' + bid, headers=headers)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', record)
    assert response.status_code == 200, response.text
    assert not any('report_json' in sql for sql in queries)
    assert response.json()['report'] is None
    assert response.json()['status'] == 'pending'
    response = client.post(f'/api/maintenance/ledger-imports/{bid}/apply', headers=headers)
    assert response.status_code == 403
    assert db.get(MaintenanceLedgerImportBatch, bid).status == 'pending'


def test_renamed_owner_keeps_batch_and_applies_with_current_audit_identity(db, setup):
    uid, identity, _, _ = setup
    bid = _create(db, identity)
    owner = db.get(SysUser, uid)
    owner.username = 'ledger-owner-renamed'
    db.commit()
    renamed = VerifiedIdentity(owner.username, owner.token_version, False, 'sys_user', MappingProxyType({}), uid)
    result = ledger.apply_batch(db, bid, 'forged', identity=renamed)
    assert result['projects_created'] == 1
    assert db.get(MaintenanceLedgerImportBatch, bid).applied_by == renamed.username


def test_revocation_after_domain_writes_rolls_back_every_business_row(db, setup, monkeypatch):
    uid, identity, _, _ = setup
    bid = _create(db, identity)
    original = ledger._upsert_milestone
    fired = False
    def revoke(*args, **kwargs):
        nonlocal fired
        result = original(*args, **kwargs)
        if not fired:
            fired = True
            from sqlalchemy.orm import Session
            with Session(db.get_bind()) as separate:
                user = separate.get(SysUser, uid)
                user.token_version += 1
                separate.commit()
        return result
    monkeypatch.setattr(ledger, '_upsert_milestone', revoke)
    with pytest.raises(HTTPException) as failure:
        ledger.apply_batch(db, bid, identity.username, identity=identity)
    assert failure.value.status_code == 401
    assert fired
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0
    assert db.get(MaintenanceLedgerImportBatch, bid).status == 'pending'


def test_unknown_legacy_owner_never_inherits_matching_username(db, setup):
    _, _, client, headers = setup
    from uuid import uuid4
    row = MaintenanceLedgerImportBatch(batch_id=str(uuid4()), file_hash='a'*64,
        filename='old.xlsx', idempotency_key='legacy-original', source_kind='ledger_template_v1',
        uploaded_by='ledger-owner', contract_rows=0, plan_rows=0, expense_rows=0, issue_rows=0, status='pending')
    db.add(row)
    db.commit()
    response = client.get('/api/maintenance/ledger-imports/' + row.batch_id, headers=headers)
    assert response.status_code == 403


def test_admin_is_not_batch_owner_but_explicit_all_imports_grant_can_read(db, setup):
    uid, identity, _, _ = setup
    bid = _create(db, identity)
    other = SysUser(username='ledger-other-admin', role='admin', password_hash='synthetic')
    db.add(other)
    db.flush()
    grant = SysAuthorizationGrant(user_id=other.id, resource='maintenance_ledger_import',
        actions=['read_metadata'], scope='own_imports', field_permissions=[], version=1)
    db.add(grant)
    db.commit()
    other_identity = VerifiedIdentity(other.username, 0, False, 'sys_user', MappingProxyType({}), other.id)
    from sqlalchemy.orm import sessionmaker
    with pytest.raises(HTTPException) as denied:
        ledger.read_batch(sessionmaker(bind=db.get_bind()), other_identity, bid)
    assert denied.value.status_code == 403
    grant.scope = 'all_imports'
    grant.version += 1
    db.commit()
    result = ledger.read_batch(sessionmaker(bind=db.get_bind()), other_identity, bid)
    assert result['batch_id'] == bid and result['report'] is None


def test_actual_parsed_ledger_rows_are_bound_even_when_file_hash_is_unchanged(db, setup):
    from app.models.maintenance_ledger import MaintenanceLedgerContractRow
    uid, identity, client, headers = setup
    bid = _create(db, identity)
    assert client.get('/api/maintenance/ledger-imports/' + bid + '?include_report=true', headers=headers).status_code == 200
    row = db.scalar(select(MaintenanceLedgerContractRow).where(MaintenanceLedgerContractRow.batch_id == bid))
    row.manager = 'changed-stored-normalized-manager'
    db.commit()
    target = artifacts.ArtifactTarget('ledger_import_batch', bid, 1)
    with pytest.raises(HTTPException) as denied:
        artifacts.load_artifact_binding(db, target=target)
    assert denied.value.status_code == 403
