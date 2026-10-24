"""Relationship commands lock all affected users before checking explicit Grants."""
from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import catalog, runtime
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectUserAssignment
from app.models.system import SysUser
from app.services import maintenance_project_assignments as assignments
from tests.test_authz_s3_project_foundation import projects  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def setup(db,projects,monkeypatch,cerbos_url):
    actor,identity,ids=projects
    settings=get_settings()
    for key,value in {'authorization_mode':'unified_modules','authz_enabled_resources':['maintenance_assignments'],
        'authz_suspended_resources':[],'authz_sales_analysis_enabled':False,'authz_sales_analysis_suspended':False,
        'cerbos_url':cerbos_url,'cerbos_timeout_seconds':3.0}.items():monkeypatch.setattr(settings,key,value)
    runtime.activate_configured_resources(db.get_bind())
    actor.role='readonly'
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_assignments',actions=['assign','archive'],
        scope='all_projects',field_permissions=[],version=1))
    other=SysUser(username='assignment-other',role='readonly',password_hash='synthetic',salesperson_name='new-sales')
    db.add(other);db.commit()
    return actor,identity,other,ids


def test_custom_grant_assignment_and_archive_bind_real_stable_target(db,setup):
    actor,identity,target,ids=setup
    command={'user_id':target.id,'reason':'explicit relationship grant','sync_salesperson':True}
    view=assignments.assignment_command(db,identity,project_id=ids['other'],command=command,view='authorization')
    assert db.scalar(select(MaintenanceProjectUserAssignment).where(MaintenanceProjectUserAssignment.project_id==ids['other'])) is None
    proof={k:view['authorization_context'][k] for k in ('catalog_version','authorization_epoch','context')}
    result=assignments.assignment_command(db,identity,project_id=ids['other'],command=command,authorization_precondition=proof)
    assert result['user_id']==target.id
    assert db.get(MaintenanceProject,ids['other']).salesperson=='new-sales'
    archived=assignments.assignment_command(db,identity,assignment_id=result['assignment_id'],
        command={'version':result['version'],'reason':'explicit archive grant'})
    assert archived['archived_at'] is not None


def test_same_actor_as_target_locks_update_once_without_share_upgrade(db,setup):
    actor,identity,_,ids=setup
    result=assignments.assignment_command(db,identity,project_id=ids['other'],
        command={'user_id':actor.id,'reason':'self assignment'})
    assert result['user_id']==actor.id


def test_cross_actor_assignment_commands_follow_one_user_lock_order(db,setup):
    actor,identity,other,ids=setup
    db.add(SysAuthorizationGrant(user_id=other.id,resource='maintenance_assignments',actions=['assign'],
        scope='all_projects',field_permissions=[],version=1));db.commit()
    other_identity=VerifiedIdentity(other.username,other.token_version,False,'sys_user',MappingProxyType({}),other.id)
    def run(who,project,target):
        with Session(db.get_bind()) as worker:
            return assignments.assignment_command(worker,who,project_id=project,
                command={'user_id':target,'reason':'cross actor final lock set'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,identity,ids['other'],other.id),pool.submit(run,other_identity,ids['sales'],actor.id)]
        results=[future.result(timeout=10) for future in futures]
    assert {row['user_id'] for row in results}=={actor.id,other.id}


def test_no_grant_admin_cannot_create_assignment(db,setup):
    actor,identity,target,ids=setup
    actor.role='admin'
    grant=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id))
    grant.actions=[];db.commit()
    with pytest.raises(HTTPException) as denied:
        assignments.assignment_command(db,identity,project_id=ids['other'],
            command={'user_id':target.id,'reason':'must deny'})
    assert denied.value.status_code==403
    assert db.scalar(select(MaintenanceProjectUserAssignment).where(MaintenanceProjectUserAssignment.project_id==ids['other'])) is None


@pytest.mark.parametrize('allowed,expected_status', [(True, 422), (False, 403)])
def test_same_target_business_validation_follows_current_explicit_action(db, setup, allowed, expected_status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import maintenance_project_assignments as api
    from app.auth import _make_token
    from app.db import get_db
    actor, identity, target, ids = setup
    created = assignments.assignment_command(db, identity, project_id=ids['other'],
        command={'user_id': target.id, 'reason': 'initial relationship'})
    if not allowed:
        grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == actor.id,
            SysAuthorizationGrant.resource == 'maintenance_assignments'))
        grant.actions = []; grant.version += 1; db.commit()
    token, _ = _make_token(actor.role, actor.username, None, token_version=actor.token_version, authn='sys_user')
    app = FastAPI(); app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    response = TestClient(app).post('/api/maintenance/projects/stable/' + ids['other'] + '/manager-assignment',
        headers={'Authorization': 'Bearer ' + token}, json={'user_id': target.id, 'reason': 'same target',
            'expected_assignment_id': created['assignment_id'], 'expected_assignment_version': created['version']})
    assert response.status_code == expected_status, response.text
    assert isinstance(response.json()['detail'], dict)
    current = db.get(MaintenanceProjectUserAssignment, created['assignment_id'], populate_existing=True)
    assert current.archived_at is None and current.version == created['version']


def test_epoch_revoked_after_initial_snapshot_still_rejects_at_final_user_lock(db, setup, monkeypatch):
    actor, identity, target, ids = setup
    original = assignments.assign_primary_manager
    def revoke_then_write(*args, **kwargs):
        with Session(db.get_bind()) as writer:
            current = writer.get(SysUser, actor.id)
            current.token_version += 1
            writer.commit()
        return original(*args, **kwargs)
    monkeypatch.setattr(assignments, 'assign_primary_manager', revoke_then_write)
    with pytest.raises(HTTPException) as denied:
        assignments.assignment_command(db, identity, project_id=ids['other'],
            command={'user_id': target.id, 'reason': 'late revocation'})
    assert denied.value.status_code == 401
    assert db.scalar(select(MaintenanceProjectUserAssignment).where(
        MaintenanceProjectUserAssignment.project_id == ids['other'])) is None


@pytest.mark.parametrize('grant,role,status',[(True,'readonly',201),(False,'admin',403)])
def test_http_assignment_uses_explicit_grant_not_legacy_role(db,setup,grant,role,status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import maintenance_project_assignments as api
    from app.auth import _make_token
    from app.db import get_db
    actor,_,target,ids=setup
    actor.role=role
    if not grant:
        row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id));row.actions=[]
    db.commit()
    token,_=_make_token(actor.role,actor.username,None,token_version=actor.token_version,authn='sys_user')
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    response=TestClient(app).post('/api/maintenance/projects/stable/'+ids['other']+'/manager-assignment',
        headers={'Authorization':'Bearer '+token},json={'user_id':target.id,'reason':'HTTP explicit action'})
    assert response.status_code==status,response.text


def test_manager_directory_http_exact_read_precondition_and_no_store(db,setup,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import maintenance_manager_directory as api
    from app.auth import _make_token
    from app.db import get_db
    actor,_,target,_=setup
    settings=get_settings()
    monkeypatch.setattr(settings,'authz_enabled_resources',settings.authz_enabled_resources+['maintenance_manager_directory'])
    runtime.activate_configured_resources(db.get_bind())
    token,_=_make_token(actor.role,actor.username,None,token_version=actor.token_version,authn='sys_user')
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);client.headers['Authorization']='Bearer '+token
    path='/api/maintenance/project-manager-assignments/search';body={'q':'assignment','page':1,'page_size':10}
    assert client.post(path,json=body).status_code==403
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_manager_directory',actions=['read'],scope='all',field_permissions=[],version=1));db.commit()
    view=client.post(path+'?view=authorization',json=body)
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'} and view.headers['cache-control']=='no-store'
    auth=view.json()['authorization_context'];pre={k:auth[k] for k in ('catalog_version','authorization_epoch','context')}
    result=client.post(path,json={**body,'authorization_precondition':pre})
    assert result.status_code==200,result.text
    assert result.headers['cache-control']=='no-store'
    assert any(row['user_id']==target.id for row in result.json()['rows'])
    stale=client.post(path,json={**body,'q':'changed-selection','authorization_precondition':pre})
    assert stale.status_code==409,stale.text
    assert client.post(path+'?view=typo',json=body).status_code==422
