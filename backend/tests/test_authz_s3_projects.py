"""Canonical project HTTP reads exercise real scope plans and field projection."""
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select, update

from app.api import maintenance_projects as api
from app.auth import _make_token
from app.authz import catalog, runtime
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.services import maintenance_authorization as domain
from tests.test_authz_s3_project_foundation import projects  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def canonical(db, projects, monkeypatch, cerbos_url):
    actor, identity, ids = projects
    settings = get_settings()
    for name, value in {'authorization_mode':'unified_modules',
        'authz_enabled_resources':list(catalog.RESOURCE_DEFINITIONS), 'authz_suspended_resources':[],
        'authz_sales_analysis_enabled':False, 'authz_sales_analysis_suspended':False,
        'cerbos_url':cerbos_url, 'cerbos_timeout_seconds':3.0}.items():
        monkeypatch.setattr(settings, name, value)
    runtime.activate_configured_resources(db.get_bind())
    actor.role = 'readonly'
    db.add(SysAuthorizationGrant(user_id=actor.id, resource='maintenance_project_directory',
        actions=['read'], scope='assigned_projects', field_permissions=[], version=7))
    db.add(SysAuthorizationGrant(user_id=actor.id, resource='maintenance_project',
        actions=['read'], scope='assigned_or_sales_projects', field_permissions=[], version=3))
    db.commit()
    token, _ = _make_token(actor.role, actor.username, None, token_version=actor.token_version, authn='sys_user')
    app = FastAPI(); app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), {'Authorization':'Bearer '+token}, ids


def test_directory_sql_scope_precedes_count_search_and_page(db, canonical):
    client, headers, ids = canonical
    response = client.get('/api/maintenance/projects/stable?page_size=1&include_inactive=true', headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['total'] == 3 and len(payload['rows']) == 1
    assert payload['rows'][0]['project_id'] in {ids['manager'], ids['viewer'], ids['inactive_manager']}
    assert payload['authorization_context']['resources']['maintenance_project_directory']['grant_version'] == 7
    hidden = client.post('/api/maintenance/projects/stable/search', headers=headers, json={'q':'other'})
    assert hidden.status_code == 200 and hidden.json()['total'] == 0 and hidden.json()['rows'] == []
    visible = client.post('/api/maintenance/projects/stable/search', headers=headers, json={'q':'viewer'})
    assert visible.status_code == 200 and visible.json()['total'] == 1


def test_authorization_view_does_not_load_facts_and_matches_payload_revision(db, canonical):
    client, headers, ids = canonical
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany): statements.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        view = client.get('/api/maintenance/projects/stable?view=authorization&as_of=2026-10-23', headers=headers)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert view.status_code == 200, view.text
    assert set(view.json()) == {'locator','authorization_context'}
    assert not any('maintenance_project.display_name' in q or 'maintenance_project_contract' in q
        for q in statements if 'maintenance_authorization_relationship_digest' not in q)
    assert not any('.amount_inc_tax' in q or '.contract_amount' in q for q in statements)
    import json
    proof = {key:view.json()['authorization_context'][key] for key in ['catalog_version','authorization_epoch','context']}
    result = client.get('/api/maintenance/projects/stable', headers=headers,
        params={'as_of':'2026-10-23','authorization_precondition':json.dumps(proof)})
    assert result.status_code == 200, result.text
    assert result.json()['authorization_context']['context'] == proof['context']
    db.execute(update(MaintenanceProject).where(MaintenanceProject.project_id == ids['manager']).values(version=2))
    db.commit()
    stale = client.get('/api/maintenance/projects/stable', headers=headers,
        params={'as_of':'2026-10-23','authorization_precondition':json.dumps(proof)})
    assert stale.status_code == 409


def test_direct_project_denies_unrelated_before_contract_facts(db, canonical):
    client, headers, ids = canonical
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany): statements.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        result = client.get('/api/maintenance/projects/stable/'+ids['other'], headers=headers)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert result.status_code == 403, result.text
    assert not any('maintenance_project_contract' in q for q in statements)
    actual = client.get('/api/maintenance/projects/stable/'+ids['sales'], headers=headers)
    assert actual.status_code == 200, actual.text
    assert actual.json()['total_contract_amount'] is None
    assert actual.json()['completeness']['status'] == 'restricted'


def test_base_project_view_does_not_require_identity_resource_for_other_grant(db, canonical, projects):
    client, headers, ids = canonical
    actor, _, _ = projects
    row = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
        SysAuthorizationGrant.resource=='maintenance_project'))
    row.actions = []; row.version += 1
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_expense',actions=['read_rows'],
        scope='all_projects',field_permissions=['data_profit'],version=2))
    db.commit()
    result = client.get('/api/maintenance/projects/stable/'+ids['manager']+'?view=authorization',headers=headers)
    assert result.status_code == 200, result.text
    decisions = result.json()['authorization_context']['resources']
    assert decisions['maintenance_project']['decisions']['read']['effect']=='deny'
    assert decisions['maintenance_project']['grant_version']==4
    assert decisions['maintenance_expense']['decisions']['read_rows']['effect']=='allow'
    assert 'project' not in result.json()


@pytest.mark.parametrize('selection', [{'include_inactive':'false'}, {'as_of':'invalid'}, {'undeclared':True}])
def test_registry_selection_does_not_accept_json_type_coercion(selection):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        domain.slot_ref('GET /api/maintenance/projects/stable', 'context_0',
                        'maintenance_project_directory', selection=selection)
    assert error.value.status_code == 422


def _grant_write(db, actor, actions):
    row = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
        SysAuthorizationGrant.resource=='maintenance_project'))
    row.actions = actions; row.version += 1
    db.commit()


def test_project_mutation_uses_own_action_without_old_role_and_commits_once(db, canonical, projects):
    client, headers, ids = canonical
    actor, _, _ = projects
    _grant_write(db, actor, ['update','archive','restore'])
    path='/api/maintenance/projects/stable/'+ids['manager']
    body={'version':1,'project_note':'canonical authorized edit','reason':'independent grant'}
    preview=client.patch(path+'?view=authorization',headers=headers,json=body)
    assert preview.status_code==200,preview.text
    assert db.get(MaintenanceProject,ids['manager']).project_note is None
    current=preview.json()['authorization_context']
    body['authorization_precondition']={k:current[k] for k in ('catalog_version','authorization_epoch','context')}
    changed=client.patch(path,headers=headers,json=body)
    assert changed.status_code==200,changed.text
    assert changed.json()['version']==2
    db.expire_all()
    assert db.get(MaintenanceProject,ids['manager']).project_note=='canonical authorized edit'
    archived=client.post(path+'/archive',headers=headers,json={'version':2,'reason':'archive authorized'})
    assert archived.status_code==200,archived.text
    restored=client.post(path+'/restore',headers=headers,json={'version':3,'reason':'restore authorized'})
    assert restored.status_code==200,restored.text
    assert restored.json()['version']==4 and restored.json()['is_active'] is True


def test_empty_action_admin_cannot_update_or_create_relation(db, canonical, projects):
    client, headers, ids=canonical
    actor,_,_=projects
    actor.role='admin';db.commit()
    _grant_write(db,actor,[])
    result=client.patch('/api/maintenance/projects/stable/'+ids['manager'],headers=headers,
        json={'version':1,'project_note':'denied','visible_usernames':['s3-foundation'],'reason':'denied action'})
    assert result.status_code==403,result.text
    db.expire_all()
    assert db.get(MaintenanceProject,ids['manager']).version==1
    assert db.get(MaintenanceProject,ids['manager']).project_note is None


def test_viewer_target_username_reuse_invalidates_command_proof(db, canonical, projects):
    from app.models.system import SysUser
    client,headers,ids=canonical
    actor,_,_=projects
    _grant_write(db,actor,['update'])
    target=SysUser(username='viewer-target',role='readonly',password_hash='synthetic')
    db.add(target);db.commit()
    path='/api/maintenance/projects/stable/'+ids['manager']
    body={'version':1,'visible_usernames':['viewer-target'],'reason':'replace viewer'}
    preview=client.patch(path+'?view=authorization',headers=headers,json=body)
    assert preview.status_code==200,preview.text
    current=preview.json()['authorization_context']
    body['authorization_precondition']={k:current[k] for k in ('catalog_version','authorization_epoch','context')}
    target.username='renamed-target';db.commit()
    db.add(SysUser(username='viewer-target',role='readonly',password_hash='synthetic'));db.commit()
    result=client.patch(path,headers=headers,json=body)
    assert result.status_code==409,result.text
    assert db.get(MaintenanceProject,ids['manager']).version==1


def test_epoch_revocation_while_waiting_for_project_lock_blocks_old_command(db, canonical, projects):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy.orm import Session
    from app.models.system import SysUser
    from app.services import maintenance_project_access as access
    from app.services import maintenance_project_operations as operations
    from fastapi import HTTPException
    _,_,ids=canonical
    actor,identity,_=projects
    _grant_write(db,actor,['update'])
    operations.get_or_create_workbook_state(db,project_id=ids['manager']);db.commit()
    waiting=Event()
    def observe(conn,cursor,statement,parameters,context,executemany):
        if 'maintenance_project' in statement and 'FOR UPDATE' in statement:
            waiting.set()
    def command():
        with Session(db.get_bind()) as worker:
            return access.change_project(worker,identity,ids['manager'],action='update',
                command={'version':1,'project_note':'must not commit','reason':'revocation race'})
    with Session(db.get_bind()) as blocker:
        blocker.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id==ids['manager']).with_for_update())
        event.listen(db.get_bind(),'before_cursor_execute',observe)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future=executor.submit(command)
                assert waiting.wait(5), 'writer did not reach domain lock'
                with Session(db.get_bind()) as manager:
                    manager.execute(update(SysUser).where(SysUser.id==actor.id).values(token_version=4));manager.commit()
                blocker.commit()
                with pytest.raises(HTTPException) as revoked: future.result(timeout=10)
                assert revoked.value.status_code==401
        finally:
            event.remove(db.get_bind(),'before_cursor_execute',observe)
            blocker.rollback()
    db.expire_all()
    assert db.get(MaintenanceProject,ids['manager']).project_note is None


def test_same_actor_two_project_writes_share_actor_without_upgrade_deadlock(db, canonical, projects, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    from app.services import maintenance_project_access as access
    _,_,ids=canonical
    actor,identity,_=projects
    _grant_write(db,actor,['update'])
    synchronized=Barrier(2)
    original=domain.lock_final_users
    def both_locked(*args,**kwargs):
        result=original(*args,**kwargs)
        synchronized.wait(timeout=5)
        return result
    monkeypatch.setattr(domain,'lock_final_users',both_locked)
    def command(label):
        with Session(db.get_bind()) as worker:
            return access.change_project(worker,identity,ids[label],action='update',
                command={'version':1,'project_note':'parallel '+label,'reason':'two domains same actor'})
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(command,label) for label in ('manager','viewer')]
        results=[future.result(timeout=10) for future in futures]
    assert all(row['version']==2 for row in results)


def test_collection_query_batch_keeps_denied_grant_version_and_independent_sql(db,canonical,projects):
    from sqlalchemy.orm import Session
    actor,identity,ids=projects
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_global_rows',actions=['read'],
        scope='assigned_or_sales_projects',field_permissions=[],version=5))
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_managed_rows',actions=[],
        scope='primary_managed_projects',field_permissions=[],version=8))
    db.commit()
    with domain.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id='GET /api/maintenance/spare-part-lines/fields',params={},branch='authorization_view') as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        wire=accesses.wire(ref)['resources']
        assert wire['maintenance_managed_rows']['grant_version']==8
        assert wire['maintenance_managed_rows']['decisions']['read']['effect']=='deny'
        assert wire['maintenance_global_workbook']['grant_version'] is None
        access=accesses.require(ref,'maintenance_global_rows','read')
        visible=set(snapshot.scalars(select(MaintenanceProject.project_id).where(domain.project_condition(access))))
        assert visible=={ids[k] for k in ('manager','viewer','sales','inactive_manager')}


def test_later_collection_plan_fault_records_no_partial_access(db,canonical,projects,monkeypatch):
    from sqlalchemy.orm import Session
    from app.authz import engine,service
    from fastapi import HTTPException
    actor,identity,_=projects
    for resource,scope in [('maintenance_global_rows','assigned_or_sales_projects'),('maintenance_managed_rows','primary_managed_projects')]:
        db.add(SysAuthorizationGrant(user_id=actor.id,resource=resource,actions=['read'],scope=scope,field_permissions=sorted(catalog.get_resource_definition(resource).field_permissions),version=1))
    db.commit()
    original=engine._client;calls=[]
    class FailureAfterFirst:
        def __enter__(self):self.manager=original();self.client=self.manager.__enter__();return self
        def __exit__(self,*args):return self.manager.__exit__(*args)
        def check_resources(self,*args,**kwargs):return self.client.check_resources(*args,**kwargs)
        def plan_resources(self,*args,**kwargs):
            calls.append(1)
            if len(calls)==2:raise engine.AuthorizationUnavailable('second plan unavailable')
            return self.client.plan_resources(*args,**kwargs)
    monkeypatch.setattr(engine,'_client',FailureAfterFirst)
    recorded=[]
    with service.access_recorder(recorded.append),pytest.raises(HTTPException) as unavailable:
        with domain.read_context_snapshot(lambda:Session(db.get_bind()),identity,
                entry_id='GET /api/maintenance/spare-part-lines/fields',params={},branch='authorization_view'):
            pytest.fail('a partially planned batch must not be exposed')
    assert unavailable.value.status_code==503
    assert len(calls)==2 and recorded==[]



def test_canonical_manual_project_creation_is_fixed_business_rejection_even_admin_full_grant(db,canonical,projects):
    from sqlalchemy import func
    from app.models.maintenance_project import MaintenanceProjectAlias,MaintenanceProjectAuditLog
    client,_,_=canonical;actor,_,_=projects
    actor.role='admin'
    grant=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
        SysAuthorizationGrant.resource=='maintenance_project'))
    grant.actions=sorted(catalog.get_resource_definition('maintenance_project').actions)
    grant.field_permissions=sorted(catalog.get_resource_definition('maintenance_project').field_permissions)
    grant.scope='all_projects';grant.version+=1;db.commit()
    token,_=_make_token(actor.role,actor.username,None,token_version=actor.token_version,authn='sys_user')
    models=[MaintenanceProject,MaintenanceProjectAlias,MaintenanceProjectAuditLog]
    before=[db.scalar(select(func.count()).select_from(model)) for model in models]
    response=client.post('/api/maintenance/projects/stable',headers={'Authorization':'Bearer '+token},
        json={'project_code':'MANUAL-FORBIDDEN','display_name':'不得创建','reason':'full grants still no manual create'})
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']=='MAINTENANCE_MANUAL_PROJECT_CREATION_DISABLED'
    assert response.headers['cache-control']=='no-store'
    assert before==[db.scalar(select(func.count()).select_from(model)) for model in models]



def test_project_read_exposes_actual_manager_assignment_occ_without_workspace(db,canonical,projects):
    client,headers,ids=canonical;actor,_,_=projects
    response=client.get('/api/maintenance/projects/stable/'+ids['manager'],headers=headers)
    assert response.status_code==200,response.text
    manager=response.json()['project']['manager_assignment']
    assert manager['user_id']==actor.id and manager['version']==1
    assert manager['project_id']==ids['manager'] and manager['assignment_id']
    assert client.get('/api/maintenance/projects/stable/'+ids['other'],headers=headers).status_code==403
    response=client.get('/api/maintenance/projects/stable/'+ids['sales'],headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['project']['manager_assignment'] is None
