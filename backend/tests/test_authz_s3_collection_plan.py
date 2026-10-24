"""Real HTTP/PDP over original uploads and atomic whole-plan application."""
from datetime import date
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select,func,update
from app.api import maintenance_collection_plan_imports as api
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant,SysAuthorizationArtifact
from app.models.maintenance_manager import MaintenanceCollectionPlanImportBatch as Batch,MaintenanceCollectionMilestone as Milestone
from app.services import maintenance_collection_plan_access as protected
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401
from tests.test_authz_s3_finance import _pre
from tests.test_maintenance_collection_plan_imports import _workbook,_row,_project,_bindings_payload


@pytest.fixture
def plans(db,canonical,projects,monkeypatch,tmp_path):
    actor,identity,_=projects
    settings=get_settings()
    monkeypatch.setattr(settings,'raw_file_dir',str(tmp_path))
    monkeypatch.setattr(settings,'maintenance_collection_plan_apply_enabled',True)
    monkeypatch.setattr(settings,'maintenance_collection_canary_project_id',None)
    monkeypatch.setattr(api,'business_today',lambda:date(2026,10,23))
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    content=_workbook(_row('PLAN-ONE','User input',months=['2027年1月'],amounts=[100]))
    return TestClient(app),canonical[1],actor,identity,content,tmp_path


def grant(db,actor,resource,actions):
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,SysAuthorizationGrant.resource==resource))
    if row:row.actions=actions;row.version+=1
    else:
        row=SysAuthorizationGrant(user_id=actor.id,resource=resource,actions=actions,
            scope='own_imports' if resource==protected.WRITE else 'all_imports',field_permissions=['data_profit'],version=1)
        db.add(row)
    db.commit();return row


def upload(plans,*,key='s3-plan-key-one',view=None,precondition=None,content=None):
    client,headers,_,_,original,_=plans
    return client.post('/api/maintenance/collection-plan-imports/preview',headers={**headers,'Idempotency-Key':key},
        params={'view':view} if view else {},files={'file':('plan.xls',original if content is None else content,'application/vnd.ms-excel')},
        data={'authorization_precondition':json.dumps(precondition)} if precondition else {})


def test_preview_explicit_grant_zero_write_view_and_bytes_binding(db,plans):
    client,headers,actor,identity,content,root=plans
    assert upload(plans).status_code==403
    grant(db,actor,protected.WRITE,['preview'])
    view=upload(plans,view='authorization');assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(Batch))==0 and not list(root.rglob('*'))
    bad=upload(plans,precondition=_pre(view.json()),content=b'other bytes')
    assert bad.status_code==409,bad.text
    result=upload(plans,precondition=_pre(view.json()));assert result.status_code==200,result.text
    batch=db.get(Batch,result.json()['batch_id']);assert batch.owner_user_id==actor.id
    assert db.get(SysAuthorizationArtifact,(protected.KIND,batch.batch_id)) is not None
    assert upload(plans).json()['batch_id']==batch.batch_id
    assert db.scalar(select(func.count()).select_from(Milestone))==0


def test_raw_and_options_independent_current_grant_and_whole_file_hash(db,plans):
    client,headers,actor,_,content,_=plans;grant(db,actor,protected.WRITE,['preview'])
    created=upload(plans);assert created.status_code==200,created.text
    batch=db.get(Batch,created.json()['batch_id']);path=f'/api/maintenance/collection-plan-imports/{batch.batch_id}'
    assert client.get(path+'/source-file',headers=headers).status_code==403
    row=grant(db,actor,protected.READ,['read'])
    _project(db,suffix='option')
    options=client.get(path+'/binding-options',headers=headers,params={'q':'合成'})
    assert options.status_code==200,options.text
    assert set(options.json()['rows'][0])=={'project_id','project_code','display_name','version','contracts'}
    assert client.get(path+'/source-file',headers=headers).status_code==403
    row=grant(db,actor,protected.READ,['raw_download'])
    view=client.get(path+'/source-file?view=authorization',headers=headers)
    assert set(view.json())=={'locator','authorization_context'}
    download=client.get(path+'/source-file',headers=headers,params={'authorization_precondition':json.dumps(_pre(view.json()))})
    assert download.status_code==200 and download.content==content
    assert download.headers['cache-control']=='no-store'
    (protected.imports._storage_dir()/batch.storage_key).write_bytes(b'corrupt original')
    assert client.get(path+'/source-file',headers=headers).status_code==403


def test_apply_complete_plan_current_owner_and_replay_no_admin_shortcut(db,plans):
    client,headers,actor,_,_,_=plans;grant(db,actor,protected.WRITE,['preview','apply'])
    pid,cid=_project(db,suffix='apply')
    preview=upload(plans).json()
    body={'expected_batch_version':preview['batch_version'],'expected_data_version':preview['data_version'],
        'bindings':_bindings_payload(preview,{preview['rows'][0]['row_key']:(pid,cid)})}
    path=f"/api/maintenance/collection-plan-imports/{preview['batch_id']}/apply"
    view=client.post(path+'?view=authorization',headers=headers,json=body)
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(Milestone))==0
    result=client.post(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==200,result.text
    assert result.json()['counts']['created']==1
    replay=client.post(path,headers=headers,json=body);assert replay.status_code==200,replay.text
    assert replay.json()['idempotent_replay']
    grant(db,actor,protected.WRITE,['preview'])
    assert client.post(path,headers=headers,json=body).status_code==403
    assert db.scalar(select(func.count()).select_from(Milestone))==1


def test_preview_sidecar_failure_rolls_back_new_bytes_and_batch(db,plans,monkeypatch):
    _,_,actor,_,_,root=plans;grant(db,actor,protected.WRITE,['preview'])
    def fail(*args,**kwargs):raise RuntimeError('injected sidecar failure')
    monkeypatch.setattr(protected.artifacts,'register_artifact_binding',fail)
    with pytest.raises(RuntimeError,match='injected sidecar'):upload(plans)
    assert db.scalar(select(func.count()).select_from(Batch))==0
    assert not [p for p in root.rglob('*') if p.is_file()]


def test_legacy_batch_without_sidecar_cannot_be_claimed(db,plans):
    _,_,actor,_,_,_=plans;grant(db,actor,protected.WRITE,['preview'])
    result=upload(plans);assert result.status_code==200,result.text
    sidecar=db.get(SysAuthorizationArtifact,(protected.KIND,result.json()['batch_id']))
    db.delete(sidecar);db.commit()
    assert upload(plans).status_code==403


def test_contract_error_keeps_only_authorized_original_binding(db,plans):
    _,_,actor,_,_,root=plans;grant(db,actor,protected.WRITE,['preview'])
    response=upload(plans,content=b'not an excel file');assert response.status_code==422,response.text
    row=db.scalar(select(Batch));assert row.status=='error' and row.plan_json is None
    assert db.get(SysAuthorizationArtifact,(protected.KIND,row.batch_id)) is not None
    assert len([p for p in root.rglob('*') if p.is_file()])==1


def test_shared_raw_reader_uses_own_contract_but_cannot_apply_foreign_batch(db,plans):
    from app.auth import _make_token
    from app.models.system import SysUser
    client,headers,actor,_,content,_=plans;grant(db,actor,protected.WRITE,['preview'])
    preview=upload(plans).json();pid,cid=_project(db,suffix='foreign')
    reader=SysUser(username='plan-independent-reader',role='readonly',password_hash='unused')
    db.add(reader);db.commit();grant(db,reader,protected.READ,['raw_download']);grant(db,reader,protected.WRITE,['apply'])
    token,_=_make_token(reader.role,reader.username,None,token_version=reader.token_version,authn='sys_user')
    other={'Authorization':'Bearer '+token};base='/api/maintenance/collection-plan-imports/'+preview['batch_id']
    response=client.get(base+'/source-file',headers=other)
    assert response.status_code==200 and response.content==content
    body={'expected_batch_version':preview['batch_version'],'expected_data_version':preview['data_version'],
        'bindings':_bindings_payload(preview,{preview['rows'][0]['row_key']:(pid,cid)})}
    assert client.post(base+'/apply',headers=other,json=body).status_code==403
    assert db.scalar(select(func.count()).select_from(Milestone))==0


def test_actual_contract_parent_change_rejects_old_view_without_writes(db,plans):
    from app.models.maintenance_project import MaintenanceProjectContract
    client,headers,actor,_,_,_=plans;grant(db,actor,protected.WRITE,['preview','apply'])
    pid,cid=_project(db,suffix='parent-one');new_pid,_=_project(db,suffix='parent-two')
    preview=upload(plans).json()
    body={'expected_batch_version':preview['batch_version'],'expected_data_version':preview['data_version'],
        'bindings':_bindings_payload(preview,{preview['rows'][0]['row_key']:(pid,cid)})}
    path='/api/maintenance/collection-plan-imports/'+preview['batch_id']+'/apply'
    view=client.post(path+'?view=authorization',headers=headers,json=body);assert view.status_code==200,view.text
    db.execute(update(MaintenanceProjectContract).where(MaintenanceProjectContract.project_contract_id==cid)
        .values(project_id=new_pid,version=2));db.commit()
    result=client.post(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==409,result.text
    assert db.scalar(select(func.count()).select_from(Milestone))==0


def test_apply_real_domain_lock_wait_rechecks_revoked_epoch(db,plans):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.models.maintenance_project import MaintenanceProject
    from app.models.system import SysUser
    from app.security import UserContext
    client,headers,actor,identity,_,_=plans;grant(db,actor,protected.WRITE,['preview','apply'])
    pid,cid=_project(db,suffix='locked');preview=upload(plans).json()
    body={'expected_batch_version':preview['batch_version'],'expected_data_version':preview['data_version'],
        'bindings':_bindings_payload(preview,{preview['rows'][0]['row_key']:(pid,cid)})}
    engine=db.get_bind();started=Event();pids=[]
    def run():
        with Session(engine) as session:
            pids.append(session.scalar(text('select pg_backend_pid()')));started.set()
            return protected.apply(session,identity,batch_id=preview['batch_id'],command=body,
                legacy_context=UserContext(user_id=actor.username,role='readonly',is_authenticated=True),as_of=date(2026,10,23))
    with Session(engine) as blocker,ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(select(MaintenanceProject).where(MaintenanceProject.project_id==pid).with_for_update())
        future=pool.submit(run);assert started.wait(5)
        with Session(engine) as observer:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if observer.scalar(text('select cardinality(pg_blocking_pids(:pid))'),{'pid':pids[0]}):break
                time.sleep(.02)
            else:pytest.fail('apply never reached a real domain lock wait')
            observer.execute(update(SysUser).where(SysUser.id==actor.id).values(token_version=actor.token_version+1));observer.commit()
        blocker.commit()
        with pytest.raises(HTTPException) as denied:future.result(timeout=10)
        assert denied.value.status_code==401
    assert db.scalar(select(func.count()).select_from(Milestone))==0


def test_raw_publication_requires_current_whole_input_sidecar(db,plans,monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy import delete
    from app.api import maintenance_collection_plan_imports as api
    client,headers,actor,_,_,_=plans;grant(db,actor,protected.WRITE,['preview'])
    batch_id=upload(plans).json()['batch_id'];grant(db,actor,protected.READ,['raw_download'])
    def remove_after_read(*args,**kwargs):
        with Session(db.get_bind()) as writer:
            writer.execute(delete(SysAuthorizationArtifact).where(SysAuthorizationArtifact.domain_kind==protected.KIND,
                SysAuthorizationArtifact.domain_id==batch_id));writer.commit()
    monkeypatch.setattr(api,'record_access_log',remove_after_read)
    response=client.get('/api/maintenance/collection-plan-imports/'+batch_id+'/source-file',headers=headers)
    assert response.status_code==403,response.text
