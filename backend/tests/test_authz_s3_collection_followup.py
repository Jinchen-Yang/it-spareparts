"""Actual reminder HTTP/PDP: independent scopes, financial isolation and current replay."""
from datetime import date
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event,select,update
from app.api import maintenance_collection_reminders as api
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from tests.test_authz_s3_workspace import workspace,grant  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401
from tests.test_maintenance_collection_reminders_api import _milestone
from tests.test_authz_s3_finance import _pre

@pytest.fixture
def reminders(db,workspace,monkeypatch):
    _,headers,ids,actor=workspace
    _milestone(db,project_id=ids['manager'],project_contract_id='s3-workspace-contract',
        milestone_id='s3-reminder',planned_date=date(2027,1,1))
    monkeypatch.setattr(api,'business_today',lambda:date(2026,10,23))
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),headers,ids,actor


def test_reminder_project_read_no_financial_sql_or_role_allow(db,reminders):
    client,headers,ids,actor=reminders;path='/api/maintenance/projects/stable/'+ids['manager']+'/collection-milestones'
    assert client.get(path,headers=headers).status_code==403
    grant(db,actor,'maintenance_collection_followup',['read'])
    grant(db,actor,'maintenance_collection',['read_snapshots'],['data_profit'])
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:result=client.get(path,headers=headers)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert result.status_code==200,result.text
    assert result.json()['rows'][0]['planned_amount'] is None
    assert result.json()['amount_visibility']=='restricted'
    assert not any('.planned_amount' in q or '.contract_amount' in q or '.cumulative_amount' in q for q in sql),sql
    assert client.get('/api/maintenance/projects/stable/'+ids['other']+'/collection-milestones',headers=headers).status_code==403
    view=client.get(path,headers=headers,params={'view':'authorization'})
    assert set(view.json())=={'locator','authorization_context'}
    assert view.headers['cache-control']=='no-store'
    assert client.get(path,headers=headers,params={'authorization_precondition':json.dumps(_pre(view.json()))}).status_code==200


def test_reminder_directory_plan_before_counts_and_independent_fields(db,reminders):
    client,headers,ids,actor=reminders;path='/api/maintenance/collection-reminders/search'
    grant(db,actor,'maintenance_collection_followup',['read'],['data_profit'])
    assert client.post(path,headers=headers,json={}).status_code==403
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_collection_followup_directory',
        actions=['search'],scope='assigned_projects',field_permissions=[],version=1));db.commit()
    result=client.post(path,headers=headers,json={'owner_scope':'all'})
    assert result.status_code==200,result.text
    assert {r['project']['project_id'] for r in result.json()['rows']}=={ids['manager'],ids['viewer']}
    assert result.json()['total']==2
    assert result.json()['amount_visibility']=='restricted'
    assert client.post(path,headers=headers,json={'q':'Secret other','owner_scope':'all'}).json()['total']==0
    view=client.post(path+'?view=authorization',headers=headers,json={'owner_scope':'all'})
    assert set(view.json())=={'locator','authorization_context'}
    stale=client.post(path,headers=headers,json={'owner_scope':'all','q':'changed','authorization_precondition':_pre(view.json())})
    assert stale.status_code==409,stale.text


def test_followup_explicit_record_and_current_fields_even_on_replay(db,reminders):
    client,headers,ids,actor=reminders;path='/api/maintenance/collection-milestones/s3-reminder/follow-ups'
    body={'expected_version':1,'idempotency_key':'s3-followup-one','action':'reschedule',
        'planned_month':'2027-02','reason':'current real command'}
    assert client.post(path,headers=headers,json=body).status_code==403
    grant(db,actor,'maintenance_collection_followup',['record'],['data_profit'])
    view=client.post(path+'?view=authorization',headers=headers,json=body)
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.get(MaintenanceCollectionMilestone,'s3-reminder').version==1
    result=client.post(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==200,result.text
    assert result.json()['row']['planned_amount']=='18000.00'
    assert result.json()['row']['version']==2
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
        SysAuthorizationGrant.resource=='maintenance_collection_followup'))
    row.field_permissions=[];row.version+=1;db.commit()
    replay=client.post(path,headers=headers,json=body)
    assert replay.status_code==200,replay.text
    assert replay.json()['idempotent_replay'] and replay.json()['row']['planned_amount'] is None
    row.actions=[];row.version+=1;db.commit()
    assert client.post(path,headers=headers,json=body).status_code==403


def test_followup_parent_change_invalidates_old_view_and_zero_write(db,reminders):
    client,headers,ids,actor=reminders;path='/api/maintenance/collection-milestones/s3-reminder/follow-ups'
    grant(db,actor,'maintenance_collection_followup',['record'])
    body={'expected_version':1,'idempotency_key':'s3-followup-move','action':'reschedule',
        'planned_month':'2027-02','reason':'parent proof'}
    view=client.post(path+'?view=authorization',headers=headers,json=body)
    db.execute(update(MaintenanceCollectionMilestone).where(MaintenanceCollectionMilestone.milestone_id=='s3-reminder').values(version=2));db.commit()
    result=client.post(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==409,result.text
    db.expire_all();assert db.get(MaintenanceCollectionMilestone,'s3-reminder').planned_date==date(2027,1,1)


def test_followup_domain_lock_wait_rechecks_epoch_and_rolls_back(db,reminders,projects):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from fastapi import HTTPException
    from app.models.maintenance_project import MaintenanceProject
    from app.models.system import SysUser
    from app.services import maintenance_collection_followup_access as protected
    from app.security import UserContext
    _,_,ids,actor=reminders;identity=projects[1]
    grant(db,actor,'maintenance_collection_followup',['record'])
    engine=db.get_bind();started=Event();pids=[]
    def run():
        with Session(engine) as writer:
            pids.append(writer.scalar(text('select pg_backend_pid()')));started.set()
            return protected.record(writer,identity,milestone_id='s3-reminder',
                command={'expected_version':1,'idempotency_key':'s3-concurrent-followup','action':'reschedule',
                    'planned_month':'2027-02','note':None,'reason':'current actor'},
                legacy_context=UserContext(user_id=actor.username,role='readonly',is_authenticated=True),as_of=date(2026,10,23))
    with Session(engine) as blocker,ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(select(MaintenanceProject).where(MaintenanceProject.project_id==ids['manager']).with_for_update())
        future=pool.submit(run);assert started.wait(5)
        with Session(engine) as observer:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if observer.scalar(text('select cardinality(pg_blocking_pids(:pid))'),{'pid':pids[0]}):break
                time.sleep(.02)
            else:pytest.fail('follow-up never reached actual domain lock wait')
            observer.execute(update(SysUser).where(SysUser.id==actor.id).values(token_version=actor.token_version+1));observer.commit()
        blocker.commit()
        with pytest.raises(HTTPException) as denied:future.result(timeout=10)
        assert denied.value.status_code==401
    db.expire_all();assert db.get(MaintenanceCollectionMilestone,'s3-reminder').version==1


@pytest.fixture
def evidence(db,reminders,tmp_path,monkeypatch):
    from app.api import maintenance_collection_evidence as evidence_api
    from app.config import get_settings
    monkeypatch.setattr(get_settings(),'raw_file_dir',str(tmp_path))
    monkeypatch.setattr(evidence_api,'business_today',lambda:date(2026,10,23))
    _,headers,ids,actor=reminders
    app=FastAPI();app.include_router(evidence_api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),headers,ids,actor,tmp_path


def test_evidence_upload_own_action_bytes_bound_zero_write_view_and_current_read(db,evidence):
    from tests.test_maintenance_collection_evidence import _png_bytes
    from app.models.maintenance_manager import BusinessFile
    from app.models.maintenance_collection_evidence import MaintenanceCollectionEvidence
    client,headers,ids,actor,root=evidence
    path='/api/maintenance/collection-milestones/s3-reminder/evidence'
    files={'file':('proof.png',_png_bytes(),'image/png')}
    assert client.post(path,headers=headers,files=files).status_code==403
    grant(db,actor,'maintenance_collection_followup',['evidence_upload'])
    view=client.post(path+'?view=authorization',headers=headers,files=files)
    assert view.status_code==201,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(BusinessFile.file_id)) is None
    assert not list(root.rglob('*.png'))
    poison=client.post(path,headers=headers,files={'file':('proof.png',_png_bytes('red'),'image/png')},
        data={'authorization_precondition':json.dumps(_pre(view.json()))})
    assert poison.status_code==409,poison.text
    assert db.scalar(select(BusinessFile.file_id)) is None
    result=client.post(path,headers=headers,files=files,
        data={'authorization_precondition':json.dumps(_pre(view.json()))})
    assert result.status_code==201,result.text
    assert result.json()['closed'] is True
    file=db.get(BusinessFile,result.json()['file_id']);assert file.owner_user_id==actor.id
    assert len(list(root.rglob('*.png')))==1
    assert client.get(path,headers=headers).status_code==403
    grantrow=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
        SysAuthorizationGrant.resource=='maintenance_collection_followup'))
    grantrow.actions=['evidence_read'];grantrow.version+=1;db.commit()
    listing=client.get(path,headers=headers)
    assert listing.status_code==200,listing.text
    assert len(listing.json()['rows'])==1
    assert client.post(path,headers=headers,files=files).status_code==403


def test_evidence_file_failure_cannot_close_or_commit_rows(db,evidence,monkeypatch):
    from tests.test_maintenance_collection_evidence import _png_bytes
    from app.models.maintenance_manager import BusinessFile
    from app.services import maintenance_collection_evidence as business
    client,headers,ids,actor,root=evidence
    grant(db,actor,'maintenance_collection_followup',['evidence_upload'])
    def failure(**kwargs):raise RuntimeError('actual persistence fault')
    monkeypatch.setattr(business,'write_evidence_files',failure)
    with pytest.raises(RuntimeError,match='actual persistence fault'):
        client.post('/api/maintenance/collection-milestones/s3-reminder/evidence',headers=headers,
            files={'file':('proof.png',_png_bytes(),'image/png')})
    db.expire_all();assert db.get(MaintenanceCollectionMilestone,'s3-reminder').follow_up_status=='pending'
    assert db.scalar(select(BusinessFile.file_id)) is None
