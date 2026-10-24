"""Actual JSON reads must remain usable by the generated context client."""
import json
import os

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update

from app.authz.catalog import RESOURCE_DEFINITIONS
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.maintenance_definitions import MAINTENANCE_SPECS
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401

CASES = [
    ('GET', '/api/maintenance/as-of', {}),
    ('GET', '/api/maintenance/analytics/filter-options', {}),
    ('GET', '/api/maintenance/analytics/maintenance-dashboard/options', {}),
    ('GET', '/api/maintenance/project-batch-transfer/options', {}),
    ('GET', '/api/maintenance/boss-board/projects/export/options', {}),
    ('GET', '/api/maintenance/spare-part-lines/fields', {}),
    ('GET', '/api/maintenance/project-manager/workbooks/v3/status', {'report_month':'2026-01'}),
    ('GET', '/api/maintenance/wbdd-imports/latest', {}),
    ('GET', '/api/maintenance/return-categories', {}),
    ('GET', '/api/maintenance/ai-fallback/proposals', {}),
    ('GET', '/api/maintenance/match-audit', {}),
    ('GET', '/api/maintenance/reconcile/expenses', {}),
    ('GET', '/api/replenishment-beta/capabilities', {}),
    ('GET', '/api/replenishment-beta/catalog', {}),
    ('GET', '/api/replenishment-beta/projects', {}),
    ('GET', '/api/replenishment-beta/applications', {}),
    ('GET', '/api/replenishment-beta/cart-drafts/{project_id}', {}),
    ('GET', '/api/maintenance/projects/stable/{project_id}/master-workbook/row-fields', {}),
    ('GET', '/api/maintenance/projects/stable/{project_id}/workspace', {}),
    ('GET', '/api/maintenance/projects/stable/{project_id}/acceptance/checklist', {}),
    ('GET', '/api/maintenance/projects/stable/{project_id}/acceptance', {}),
    ('GET', '/api/maintenance/projects/{project_id}/source-cancellations', {}),
    ('POST', '/api/maintenance/project-manager-assignments/search', {}),
    ('POST', '/api/maintenance/collection-reminders/search', {}),
    ('POST', '/api/maintenance/migration-runs/search', {}),
    ('POST', '/api/maintenance/acceptance-deliverables/search', {}),
    ('POST', '/api/maintenance/projects/stable/operations/search', {}),
    ('POST', '/api/maintenance/demands/search', {}),
    ('POST', '/api/maintenance/warehouse-documents/search', {}),
    ('POST', '/api/maintenance/warehouse-ambiguities/search', {}),
    ('GET', '/api/maintenance/projects/stable/{project_id}/salvages', {}),
    ('GET', '/api/maintenance/projects/stable/{project_id}/purchases', {}),
]


@pytest.fixture
def protocol_client(db, canonical, projects, monkeypatch):
    from app.main import app
    actor, _, _ = projects
    settings=get_settings()
    monkeypatch.setattr(settings,'maintenance_boss_dashboard_enabled',True)
    monkeypatch.setattr(settings,'replenishment_beta_enabled',True)
    for resource in MAINTENANCE_SPECS:
        definition=RESOURCE_DEFINITIONS[resource]
        scope=next((s for s in ('all_projects','all_contracts','all_imports','all','all_users') if s in definition.scopes),sorted(definition.scopes)[0])
        row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
            SysAuthorizationGrant.resource==resource))
        if row is None:
            row=SysAuthorizationGrant(user_id=actor.id,resource=resource,version=1)
            db.add(row)
        else: row.version += 1
        row.actions=sorted(definition.actions);row.scope=scope;row.field_permissions=sorted(definition.field_permissions)
    db.commit()
    # The real actor is readonly with no old page flags: an accidental legacy guard is observable.
    assert actor.role=='readonly'
    previous=dict(app.dependency_overrides)
    app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(app);client.headers.update(canonical[1])
        snapshot=client.get('/api/authz/me')
        assert snapshot.status_code==200,snapshot.text
        yield client,snapshot.json(),canonical[2]['manager']
    finally:
        app.dependency_overrides.clear();app.dependency_overrides.update(previous)


def _assert_closed_context(value,snapshot,entry,*,view):
    assert set(value)=={'schema_version','catalog_version','authorization_epoch','context','resources'}
    assert value['schema_version']==1
    assert value['catalog_version']==snapshot['catalog_version']
    assert value['authorization_epoch']==snapshot['authorization_epoch']
    branches=[b for b in ENTRY_CONTRACTS[entry]['branches'].values() if view or not b.get('authorization_view_only')]
    assert any(set(value['resources'])==set(branch['requirements']) and all(
        set(value['resources'][resource]['decisions'])==set(actions)
        for resource,actions in branch['requirements'].items()) for branch in branches), value
    for resource,decision in value['resources'].items():
        probe=snapshot['resources'][resource]
        assert (decision['grant_version'] or 0)==probe['grant_version']
        assert set(decision['field_permissions'])<=set(probe['field_permissions'])
        assert all(action in probe['decisions'] for action in decision['decisions'])


@pytest.mark.parametrize('method,path,selection',CASES,ids=[row[0]+' '+row[1] for row in CASES])
def test_real_read_view_precondition_and_formal_json_are_consumable(db,protocol_client,method,path,selection):
    client,snapshot,project_id=protocol_client
    url=path.format(project_id=project_id)
    kwargs={'params':{**selection,'view':'authorization'}} if method=='GET' else {'params':{'view':'authorization'},'json':selection}
    view=client.request(method,url,**kwargs)
    assert view.status_code==200,view.text
    resolution=view.json();assert set(resolution)=={'locator','authorization_context'}
    before=resolution['authorization_context']
    _assert_closed_context(before,snapshot,method+' '+path,view=True)
    precondition={k:before[k] for k in ('catalog_version','authorization_epoch','context')}
    kwargs={'params':{**selection,'authorization_precondition':json.dumps(precondition)}} if method=='GET' else {'json':{**selection,'authorization_precondition':precondition}}
    result=client.request(method,url,**kwargs)
    assert result.status_code==200,result.text
    payload=result.json()
    assert isinstance(payload,dict) and 'authorization_context' in payload,payload
    _assert_closed_context(payload['authorization_context'],snapshot,method+' '+path,view=False)
    assert payload['authorization_context']['context']==before['context']
    if filename:=os.environ.get('AUTHZ_PROTOCOL_SAMPLE_PATH'):
        with open(filename,'a') as output:
            output.write(json.dumps({'entryId':method+' '+path,'snapshot':snapshot,'view':resolution,
                'payload':payload,'pathParams':{'project_id':project_id} if '{project_id}' in path else {}})+'\n')
    db.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==snapshot['principal']['id'])
        .values(actions=[],version=SysAuthorizationGrant.version+1));db.commit()
    denied=client.request(method,url,params=selection) if method=='GET' else client.request(method,url,json=selection)
    assert denied.status_code==403,denied.text

from tests.test_authz_s3_document_imports import imports as staged_imports  # noqa:E402,F401
from tests.test_authz_s3_checklist import setup  # noqa:E402,F401


def test_actual_main_single_import_module_works_while_other_entry_targets_remain_legacy(db,staged_imports,monkeypatch):
    from app.main import app
    from app.authz import runtime
    uid,grant,original,data,preview_path,read_path,_,_,_=staged_imports
    settings=get_settings();monkeypatch.setattr(settings,'maintenance_beta_enabled',True)
    assert runtime.runtime_states(db)[grant.resource+'_targets'].runtime_state=='legacy'
    previous=dict(app.dependency_overrides);app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(app);client.headers.update(original.headers)
        upload={'headers':{'Idempotency-Key':'main-partial-import'},'files':{'file':('synthetic.xlsx',data)}}
        view=client.post(preview_path,params={'view':'authorization'},**upload)
        assert view.status_code==200,view.text
        context=view.json()['authorization_context'];pre={k:context[k] for k in ('catalog_version','authorization_epoch','context')}
        result=client.post(preview_path,data={'authorization_precondition':json.dumps(pre)},**upload)
        assert result.status_code==200,result.text
        grant.actions=['read'];grant.version+=1;db.commit()
        actual=client.get(read_path+result.json()['batch_id'])
        assert actual.status_code==200,actual.text
        assert 'authorization_context' in actual.json()
        grant.actions=[];grant.version+=1;db.commit()
        assert client.get(read_path+result.json()['batch_id']).status_code==403
        monkeypatch.setattr(settings,'maintenance_beta_enabled',False)
        assert client.get(read_path+result.json()['batch_id']).status_code==404
    finally:
        app.dependency_overrides.clear();app.dependency_overrides.update(previous)


def test_unregistered_beta_entry_keeps_legacy_whitelist_even_when_module_is_enabled(db,canonical,monkeypatch):
    from fastapi import Depends,FastAPI
    from app.maintenance_beta import for_resources
    probe=FastAPI()
    @probe.get('/api/maintenance/unregistered-beta-probe',dependencies=[Depends(for_resources('maintenance_project'))])
    def unknown():
        pytest.fail('unregistered route must retain its legacy whitelist')
    probe.dependency_overrides[get_db]=lambda:db
    monkeypatch.setattr(get_settings(),'maintenance_beta_enabled',True)
    response=TestClient(probe).get('/api/maintenance/unregistered-beta-probe',headers=canonical[1])
    assert response.status_code==403 and '未获得维保管理页面权限' in response.text


def test_every_registered_beta_mount_declares_its_complete_formal_contract():
    import inspect
    from app.main import app
    from app.maintenance_beta import require_maintenance_beta
    checked=0
    for route in app.routes:
        for dependency in getattr(getattr(route,'dependant',None),'dependencies',[]):
            if getattr(dependency.call,'release_gate',None) is not require_maintenance_beta:
                continue
            declared=set(inspect.getclosurevars(dependency.call).nonlocals['resources'])
            for method in route.methods:
                contract=ENTRY_CONTRACTS.get(method+' '+route.path)
                if contract is None:continue
                required={resource for branch in contract['branches'].values()
                    if not branch.get('authorization_view_only') for resource in branch['requirements']}
                assert required and required<=declared,(method,route.path,required-declared)
                checked+=1
    assert checked>100


@pytest.mark.parametrize('suffix', ['workbook','workbook-v3.xlsx'])
def test_actual_main_roundtrip_download_families_resolve_without_old_beta_flags(db,protocol_client,suffix,monkeypatch):
    client,snapshot,pid=protocol_client
    path=f'/api/maintenance/projects/stable/{pid}/{suffix}'
    view=client.get(path,params={'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    db.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==snapshot['principal']['id'],
        SysAuthorizationGrant.resource=='maintenance_project_roundtrip').values(actions=[],version=SysAuthorizationGrant.version+1));db.commit()
    assert client.get(path).status_code==403
    monkeypatch.setattr(get_settings(),'maintenance_beta_enabled',False)
    assert client.get(path,params={'view':'authorization'}).status_code==404


def test_actual_main_assignment_command_keeps_exact_grant_and_release_flag(db,protocol_client,monkeypatch):
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    client,snapshot,pid=protocol_client
    current=db.scalar(select(MaintenanceProjectUserAssignment).where(
        MaintenanceProjectUserAssignment.project_id==pid,MaintenanceProjectUserAssignment.responsibility_type=='primary_manager'))
    body={'user_id':snapshot['principal']['id'],'expected_assignment_id':current.assignment_id,
        'expected_assignment_version':current.version,'reason':'mount protocol only'}
    path=f'/api/maintenance/projects/stable/{pid}/manager-assignment'
    view=client.post(path,params={'view':'authorization'},json=body)
    assert view.status_code==201,view.text
    assert set(view.json())=={'locator','authorization_context'}
    db.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==snapshot['principal']['id'],
        SysAuthorizationGrant.resource=='maintenance_assignments').values(actions=[],version=SysAuthorizationGrant.version+1));db.commit()
    denied=client.post(path,json=body);assert denied.status_code==403,denied.text
    monkeypatch.setattr(get_settings(),'maintenance_beta_enabled',False)
    assert client.post(path,params={'view':'authorization'},json=body).status_code==404


def test_actual_main_ledger_preview_then_metadata_json_without_old_beta_flags(db,protocol_client):
    from tests.test_maintenance_ledger import _new_ledger_workbook_bytes
    client,snapshot,_=protocol_client
    created=client.post('/api/maintenance/ledger-imports/preview',headers={'Idempotency-Key':'main-ledger-protocol'},
        files={'file':('ledger.xlsx',_new_ledger_workbook_bytes())})
    assert created.status_code==200,created.text
    entry='GET /api/maintenance/ledger-imports/{batch_id}'
    bid=created.json()['batch_id'];path='/api/maintenance/ledger-imports/'+bid
    view=client.get(path,params={'view':'authorization'})
    assert view.status_code==200,view.text
    before=view.json()['authorization_context'];_assert_closed_context(before,snapshot,entry,view=True)
    pre={k:before[k] for k in ('catalog_version','authorization_epoch','context')}
    actual=client.get(path,params={'authorization_precondition':json.dumps(pre)})
    assert actual.status_code==200,actual.text
    _assert_closed_context(actual.json()['authorization_context'],snapshot,entry,view=False)
    assert actual.json()['authorization_context']['context']==before['context']
    if filename:=os.environ.get('AUTHZ_PROTOCOL_SAMPLE_PATH'):
        with open(filename,'a') as output:
            output.write(json.dumps({'entryId':entry,'snapshot':snapshot,'view':view.json(),
                'payload':actual.json(),'pathParams':{'batch_id':bid}})+'\n')
    db.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==snapshot['principal']['id'],
        SysAuthorizationGrant.resource=='maintenance_ledger_import').values(actions=[],version=SysAuthorizationGrant.version+1));db.commit()
    assert client.get(path).status_code==403


def test_canonical_beta_navigation_reports_release_flag_without_old_page_bit(db,protocol_client,monkeypatch):
    from app.auth import hash_password
    from app.models.system import SysUser
    client,snapshot,_=protocol_client
    features=client.get('/api/auth/beta-features')
    assert features.status_code==200,features.text
    assert features.json()['replenishment'] is True
    uid=snapshot['principal']['id'];owner=db.get(SysUser,uid)
    owner.password_hash=hash_password('synthetic-navigation-password')
    db.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==uid)
        .values(actions=[],version=SysAuthorizationGrant.version+1));db.commit()
    # A release flag opens no data or display action; canonical probes remain the only module truth.
    assert client.get('/api/replenishment-beta/applications').status_code==403
    probe=client.get('/api/authz/me').json()
    assert all(d['effect']=='deny' for d in probe['resources']['replenishment_request']['decisions'].values())
    login=client.post('/api/auth/login',json={'username':owner.username,'password':'synthetic-navigation-password'})
    assert login.status_code==200,login.text
    assert login.json()['permissions']['page_replenishment_beta'] is False
    assert login.json()['beta_features']['replenishment'] is True
    monkeypatch.setattr(get_settings(),'replenishment_beta_enabled',False)
    assert client.get('/api/auth/beta-features').json()['replenishment'] is False
