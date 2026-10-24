"""Real AI staging authorization with a synthetic provider and actual parsers."""
import json
from dataclasses import replace
from hashlib import sha256
from types import MappingProxyType

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api import maintenance_ai_fallback as api
from app.authz import runtime
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.maintenance_ai_fallback import MaintenanceAiMappingProposal as Proposal
from app.models.maintenance_doc_import import MaintenanceDocImportBatch as DocBatch
from app.models.system import SysUser
from app.services import maintenance_ai_fallback as business
from app.services import maintenance_ai_fallback_authorization as authorization
from tests.test_authz_s3_checklist import setup, cerbos_url  # noqa: F401
from tests.test_maintenance_ai_fallback import _variant_return_workbook, _fake_llm


@pytest.fixture
def ai(db, setup, monkeypatch):
    uid, identity, _, _, headers = setup
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', list(authorization.RESOURCES))
    runtime.activate_configured_resources(db.get_bind())
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.headers.update(headers)
    original = business.call_llm_for_mapping
    monkeypatch.setattr(business, 'call_llm_for_mapping',
        lambda doc_type, headers, samples, llm_call=None: original(doc_type, headers, samples,
            llm_call=llm_call or _fake_llm))
    return uid, identity, client


def grant(db, uid, resource, actions, *, scope='all', fields=None):
    if fields is None:
        fields = [] if resource == 'maintenance_ai_proposals' else ['data_profit' if resource == 'maintenance_ai_ledger' else 'data_purchase_cost']
    row = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == uid,
        SysAuthorizationGrant.resource == resource))
    if row is None:
        row = SysAuthorizationGrant(user_id=uid, resource=resource, actions=actions, scope=scope,
            field_permissions=fields, version=1)
        db.add(row)
    else:
        row.actions, row.scope, row.field_permissions, row.version = actions, scope, fields, row.version + 1
    db.commit()


def propose(client, content=None, **kwargs):
    return client.post('/api/maintenance/ai-fallback/propose?doc_type=return_order',
        files={'file': ('variant.xlsx', content or _variant_return_workbook())}, **kwargs)


def accept(client, proposal_id, data, **kwargs):
    return client.post('/api/maintenance/ai-fallback/' + proposal_id + '/accept',
        headers={'Idempotency-Key': 'canonical-ai-accept'}, files={'file': ('variant.xlsx', data)}, **kwargs)


def test_propose_view_is_zero_provider_zero_write_and_actual_input_precondition(db, ai, monkeypatch):
    uid, _, client = ai
    grant(db, uid, 'maintenance_ai_document', ['propose'])
    data = _variant_return_workbook()
    calls = []
    original = business.call_llm_for_mapping
    monkeypatch.setattr(business, 'call_llm_for_mapping', lambda *a, **k: (calls.append(True), original(*a, **k))[1])
    response = client.post('/api/maintenance/ai-fallback/propose?doc_type=return_order&view=authorization',
        files={'file': ('variant.xlsx', data)})
    assert response.status_code == 200, response.text
    assert calls == []
    assert db.scalar(select(func.count()).select_from(Proposal)) == 0
    ctx = response.json()['authorization_context']
    pre = {k: ctx[k] for k in ('catalog_version', 'authorization_epoch', 'context')}
    # A different valid XLSX cannot borrow the authorization view's digest.
    from tests.test_maintenance_ckd_import import _ckd_workbook_bytes
    response = propose(client, _ckd_workbook_bytes(rows=[]), data={'authorization_precondition': json.dumps(pre)})
    assert response.status_code == 409, response.text
    assert calls == []
    response = propose(client, data, data={'authorization_precondition': json.dumps(pre)})
    assert response.status_code == 200, response.text
    assert calls == [True]
    pid = response.json()['proposal_id']
    assert db.get(Proposal, pid).owner_user_id == uid
    assert db.get(SysAuthorizationArtifact, ('ai_proposal', pid)).owner_user_id == uid
    assert response.headers['cache-control'] == 'no-store'


def test_accept_only_uses_real_proposal_branch_and_batch_binding(db, ai):
    uid, identity, client = ai
    grant(db, uid, 'maintenance_ai_document', ['propose'])
    data = _variant_return_workbook()
    created = propose(client, data)
    assert created.status_code == 200, created.text
    pid = created.json()['proposal_id']
    assert accept(client, pid, data).status_code == 403
    # A ledger permission cannot authorize a stored document proposal.
    grant(db, uid, 'maintenance_ai_document', [])
    grant(db, uid, 'maintenance_ai_ledger', ['accept'])
    assert accept(client, pid, data).status_code == 403
    grant(db, uid, 'maintenance_ai_document', ['accept'])
    response = accept(client, pid, data)
    assert response.status_code == 200, response.text
    bid = response.json()['batch_id']
    assert db.get(DocBatch, bid).owner_user_id == uid
    assert db.get(SysAuthorizationArtifact, ('document_import_batch', bid)).owner_user_id == uid
    assert db.get(SysAuthorizationArtifact, ('ai_proposal_acceptance', pid)).owner_user_id == uid
    assert client.get('/api/maintenance/ai-fallback/proposals').status_code == 403
    assert accept(client, pid, data).json()['batch_id'] == bid
    assert accept(client, pid, data + b'changed').status_code == 409
    grant(db, uid, 'maintenance_ai_document', [])
    assert accept(client, pid, data).status_code == 403
    assert db.scalar(select(func.count()).select_from(DocBatch)) == 1


def test_provider_runs_without_open_transaction_and_revoke_before_return_prevents_save(db, ai):
    uid, identity, _ = ai
    grant(db, uid, 'maintenance_ai_document', ['propose'])
    data = _variant_return_workbook()
    headers, samples = api._read_headers_and_samples(data, 'return_order')
    def provider(prompt):
        assert not db.in_transaction()
        with Session(db.get_bind()) as other:
            other.execute(text('UPDATE sys_user SET token_version=token_version+1 WHERE id=:id'), {'id': uid})
            other.commit()
        return _fake_llm(prompt)
    with pytest.raises(HTTPException) as denied:
        authorization.propose(db, identity, data=data, doc_type='return_order', filename='v.xlsx',
            headers=headers, samples=samples, llm_call=provider)
    assert denied.value.status_code == 401
    assert db.scalar(select(func.count()).select_from(Proposal)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact)) == 0


def test_proposal_listing_sql_owner_scope_and_unknown_owners_never_adopted(db, ai):
    uid, identity, client = ai
    grant(db, uid, 'maintenance_ai_document', ['propose', 'accept'])
    data = _variant_return_workbook()
    own = propose(client, data).json()['proposal_id']
    other = SysUser(username='ai-other', role='admin', password_hash='synthetic')
    db.add(other)
    db.commit()
    grant(db, other.id, 'maintenance_ai_document', ['propose'])
    other_identity = VerifiedIdentity(other.username, other.token_version, False, 'sys_user', MappingProxyType({}), other.id)
    headers, samples = api._read_headers_and_samples(data, 'return_order')
    foreign = authorization.propose(db, other_identity, data=data, doc_type='return_order', filename='foreign.xlsx',
        headers=headers, samples=samples, llm_call=_fake_llm)['proposal_id']
    legacy = business.propose(db, doc_type='return_order', data=data, filename='legacy.xlsx',
        headers=headers, samples=samples, operated_by=identity.username, llm_call=_fake_llm)['proposal_id']
    grant(db, uid, 'maintenance_ai_proposals', ['read'], scope='own_proposals')
    response = client.get('/api/maintenance/ai-fallback/proposals')
    assert response.status_code == 200, response.text
    assert [r['proposal_id'] for r in response.json()['rows']] == [own]
    grant(db, uid, 'maintenance_ai_proposals', ['read'])
    assert {r['proposal_id'] for r in client.get('/api/maintenance/ai-fallback/proposals').json()['rows']} == {own, foreign}
    assert accept(client, legacy, data).status_code == 403
    assert db.get(Proposal, legacy).owner_user_id is None


def test_corrupt_mapping_and_acceptance_link_fail_closed(db, ai):
    uid, _, client = ai
    grant(db, uid, 'maintenance_ai_document', ['propose', 'accept'])
    data = _variant_return_workbook()
    pid = propose(client, data).json()['proposal_id']
    row = db.get(Proposal, pid)
    row.proposal = {**row.proposal, 'notes': ['modified']}
    db.commit()
    assert accept(client, pid, data).status_code == 403
    assert db.scalar(select(func.count()).select_from(DocBatch)) == 0
    pid = propose(client, data).json()['proposal_id']
    assert accept(client, pid, data).status_code == 200
    row = db.get(Proposal, pid)
    row.accepted_by = 'forged'
    db.commit()
    assert accept(client, pid, data).status_code == 403


def test_s3_ai_owner_prevents_downgrade(db, ai):
    from alembic import command
    from tests.conftest import _alembic_cfg
    uid, _, client = ai
    grant(db, uid, 'maintenance_ai_document', ['propose'])
    response = propose(client)
    assert response.status_code == 200, response.text
    # Exercise the owner guard independently of the generic sidecar marker.
    db.execute(text('DELETE FROM sys_authorization_artifact'))
    db.execute(text("DELETE FROM sys_authorization_migration_receipt WHERE entity_kind='artifact_protocol'"))
    db.commit()
    with pytest.raises(RuntimeError, match='stable command or file owner'):
        command.downgrade(_alembic_cfg(), 'c9d4e8f2a6b1')
    db.rollback()


@pytest.mark.parametrize('doc_type', ['ledger', 'ckd_shipment', 'rkd_inbound', 'bxd_expense', 'return_order'])
def test_every_stored_type_uses_its_exact_resource_and_standard_parser(db, ai, doc_type):
    from tests.test_maintenance_ckd_import import _ckd_workbook_bytes, _HEAD_MAINT
    from tests.test_maintenance_doc_import import _doc_workbook, _RKD_HEAD, _RKD_LINE, _BXD_HEAD, _BXD_LINE
    from tests.test_maintenance_ledger import _new_ledger_workbook_bytes
    uid, identity, client = ai
    resource = 'maintenance_ai_ledger' if doc_type == 'ledger' else 'maintenance_ai_document'
    grant(db, uid, resource, ['propose', 'accept'])
    if doc_type == 'ledger':
        data = _new_ledger_workbook_bytes(include_expense=True)
    elif doc_type == 'ckd_shipment':
        data = _ckd_workbook_bytes(rows=[{'head': _HEAD_MAINT, 'lines': [[''] * 18]}])
    elif doc_type in ('rkd_inbound', 'bxd_expense'):
        heads, lines = (_RKD_HEAD, _RKD_LINE) if doc_type == 'rkd_inbound' else (_BXD_HEAD, _BXD_LINE)
        values = {'入库单号': 'RKD-20260806-0014', '入库日期': '2026-08-06', '入库类别': '采购入库',
                  '费用单号': 'BXD-20260806-0014', '报销金额': '19.50', '数据状态': '已生效'}
        data = _doc_workbook(sheet_title='Sheet1', head_headers=heads, line_headers=lines,
            rows=[{'head': [values.get(h, '') for h in heads], 'lines': [[''] * len(lines)]}])
    else:
        data = _variant_return_workbook()
    headers, samples = api._read_headers_and_samples(data, doc_type)
    provider = _fake_llm if doc_type == 'return_order' else lambda prompt: json.dumps({
        'column_mapping': {next(h for h in headers if h in business.canonical_catalog(doc_type)):
                           next(h for h in headers if h in business.canonical_catalog(doc_type))}, 'notes': []})
    created = authorization.propose(db, identity, data=data, doc_type=doc_type, filename='v.xlsx',
        headers=headers, samples=samples, llm_call=provider)
    assert created['trial_error'] is None, created
    pid = created['proposal_id']
    response = accept(client, pid, data)
    assert response.status_code == 200, response.text
    bid = response.json()['batch_id']
    assert accept(client, pid, data).json()['batch_id'] == bid
    assert db.get(SysAuthorizationArtifact, ('ai_proposal_acceptance', pid)).owner_user_id == uid


def test_stable_owner_survives_rename_and_provider_or_commit_failure_is_atomic(db, ai, monkeypatch):
    from app.auth import _make_token
    from app.services import authorization_artifacts as artifacts
    uid, identity, client = ai
    grant(db, uid, 'maintenance_ai_document', ['propose', 'accept'])
    grant(db, uid, 'maintenance_ai_proposals', ['read'], scope='own_proposals')
    data = _variant_return_workbook()
    pid = propose(client, data).json()['proposal_id']
    user = db.get(SysUser, uid)
    user.username = 'renamed-ai-owner'
    db.commit()
    token, _ = _make_token(user.role, user.username, None, token_version=user.token_version, authn='sys_user')
    client.headers['Authorization'] = 'Bearer ' + token
    assert client.get('/api/maintenance/ai-fallback/proposals').json()['rows'][0]['proposal_id'] == pid
    original = artifacts.register_artifact_binding
    def fail_after_receipt(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs['target'].kind == 'ai_proposal_acceptance':
            raise RuntimeError('injected after real receipt write')
        return result
    monkeypatch.setattr(artifacts, 'register_artifact_binding', fail_after_receipt)
    with pytest.raises(RuntimeError, match='injected'):
        accept(client, pid, data)
    assert db.get(Proposal, pid).status == 'pending'
    assert db.scalar(select(func.count()).select_from(DocBatch)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact)) == 1
    monkeypatch.setattr(artifacts, 'register_artifact_binding', original)
    response = accept(client, pid, data)
    assert response.status_code == 200, response.text
    assert db.get(DocBatch, response.json()['batch_id']).owner_user_id == uid


def test_revoke_during_actual_proposal_lock_wait_prevents_preview_and_accept(db, ai):
    from concurrent.futures import ThreadPoolExecutor
    import time
    uid, identity, client = ai
    grant(db, uid, 'maintenance_ai_document', ['propose', 'accept'])
    data = _variant_return_workbook()
    pid = propose(client, data).json()['proposal_id']
    db.rollback()
    def worker():
        with Session(db.get_bind()) as command:
            try:
                authorization.accept(command, identity, proposal_id=pid, data=data,
                    filename='v.xlsx', idempotency_key='actual-lock-accept')
                return 200
            except HTTPException as exc:
                return exc.status_code
    with Session(db.get_bind()) as blocker:
        blocker.scalar(select(Proposal).where(Proposal.proposal_id == pid).with_for_update())
        blocker_pid = blocker.scalar(text('SELECT pg_backend_pid()'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            blocked = False
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline and not future.done():
                    with db.get_bind().connect() as observer:
                        blocked = observer.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))'), {'pid': blocker_pid})
                    if blocked: break
                    time.sleep(.02)
                assert blocked, 'Did not reach the actual proposal lock'
                with Session(db.get_bind()) as administrator:
                    administrator.execute(text('UPDATE sys_user SET token_version=token_version+1 WHERE id=:id'), {'id': uid})
                    administrator.commit()
            finally:
                blocker.rollback()
            assert future.result(timeout=10) == 401
    assert db.get(Proposal, pid).status == 'pending'
    assert db.scalar(select(func.count()).select_from(DocBatch)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact)) == 1


def test_same_file_and_client_key_cannot_collapse_distinct_proposals(db, ai):
    uid, identity, client = ai
    grant(db, uid, 'maintenance_ai_document', ['propose', 'accept'])
    data = _variant_return_workbook()
    first = propose(client, data).json()['proposal_id']
    headers, samples = api._read_headers_and_samples(data, 'return_order')
    def changed_mapping(prompt):
        value = json.loads(_fake_llm(prompt))
        del value['column_mapping']['需求单']
        return json.dumps(value)
    second = authorization.propose(db, identity, data=data, doc_type='return_order', filename='v.xlsx',
        headers=headers, samples=samples, llm_call=changed_mapping)['proposal_id']
    a, b = accept(client, first, data), accept(client, second, data)
    assert a.status_code == b.status_code == 200, (a.text, b.text)
    assert a.json()['batch_id'] != b.json()['batch_id']
    from app.models.maintenance_doc_import import MaintenanceDocHeadRow
    original = db.scalar(select(MaintenanceDocHeadRow).where(MaintenanceDocHeadRow.batch_id == a.json()['batch_id']))
    changed = db.scalar(select(MaintenanceDocHeadRow).where(MaintenanceDocHeadRow.batch_id == b.json()['batch_id']))
    assert original.wbdd_no == 'WBDD-20260203-0026'
    assert changed.wbdd_no is None
