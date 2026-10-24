"""Demand API contracts over actual sources, complete target sets and current receipts."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.api import maintenance_demands as api
from app.auth import _make_token
from app.authz import runtime
from app.authz.catalog import RESOURCE_DEFINITIONS
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant, SysAuthorizationArtifact
from app.models.maintenance import (FMaintenanceOrder as Order, FMaintenanceLine as Line,
    MaintenanceDemandDeleteIntent as Intent, MaintenanceDemandTombstone as Tombstone,
    MaintenanceDemandDeleteEvent as Event)
from app.models.maintenance_project import MaintenanceProjectUserAssignment as Manager
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as Assignment
from app.models.system import SysUser
from app.services import maintenance_demand_authorization as protected
from app.services import maintenance_demands as business
from app.services import maintenance_demand_manual as manual
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_demand_manual_edit import _seed_line, _make_project

P='/api/maintenance/demands'
R=protected.RESOURCE


def login(client, actor):
    token,_=_make_token(actor.role,actor.username,None,token_version=actor.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token


@pytest.fixture
def workflow(db,monkeypatch,cerbos_url):
    order,line=_seed_line(db,order_raw='DEMAND-A',line_raw='DEMAND-LINE-A',pn='DEMAND-PN')
    project=_make_project(db,tag='canonical-A',source_order_id=order.raw_order_id)
    second,line2=_seed_line(db,order_raw='DEMAND-B',line_raw='DEMAND-LINE-B',pn='DEMAND-PN-B')
    other=_make_project(db,tag='canonical-B',source_order_id=second.raw_order_id)
    order.receiver='PRIVATE RECEIVER';order.receiver_phone='13800000000';order.receiver_address='PRIVATE ADDRESS';db.commit()
    settings=get_settings()
    for k,v in {'authorization_mode':'unified_modules','authz_enabled_resources':[R], 'authz_suspended_resources':[],
        'authz_sales_analysis_enabled':False,'authz_sales_analysis_suspended':False,'cerbos_url':cerbos_url,
        'cerbos_timeout_seconds':2.0,'maintenance_beta_enabled':True}.items():monkeypatch.setattr(settings,k,v)
    runtime.activate_configured_resources(db.get_bind())
    actor=SysUser(username='demand-canonical',role='readonly',password_hash='synthetic');db.add(actor);db.flush()
    definition=RESOURCE_DEFINITIONS[R]
    db.add(SysAuthorizationGrant(user_id=actor.id,resource=R,actions=list(definition.actions),scope='all_projects',field_permissions=['data_customer'],version=1));db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);login(client,actor)
    return client,actor.id,project.project_id,other.project_id


def req(client,method,path,body=None,*,view=False,precondition=None):
    if method=='GET':return client.get(P+path,params={**(body or {}),**({'view':'authorization'} if view else {}),**({'authorization_precondition':json.dumps(precondition)} if precondition else {})})
    return client.request(method,P+path+('?view=authorization' if view else ''),json={**(body or {}),**({'authorization_precondition':precondition} if precondition else {})})


def pc(response):
    assert response.status_code==200,response.text
    assert set(response.json())=={'locator','authorization_context'}
    ctx=response.json()['authorization_context']
    return {k:ctx[k] for k in ('catalog_version','authorization_epoch','context')}


def command(client,method,path,body):
    pre=pc(req(client,method,path,body,view=True))
    result=req(client,method,path,body,precondition=pre)
    assert result.status_code in (200,201),result.text
    return result.json()


def grant(db,uid,*,actions=None,scope=None,fields=None):
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==uid,SysAuthorizationGrant.resource==R))
    if actions is not None:row.actions=actions
    if scope is not None:row.scope=scope
    if fields is not None:row.field_permissions=fields
    row.version+=1;db.commit()


def body(**extra):return {'source_order_ids':['DEMAND-A'],'reason':'actual safe cancellation','idempotency_key':'demand-key-canonical',**extra}


def test_search_and_child_reads_apply_own_fields_scope_and_no_payload_view(db,workflow):
    client,uid,pid,_=workflow
    for method,path,params in [('POST','/search',{'q':None,'page':1,'page_size':50,'include_voided':False}),('GET','/manual-protection-review',{}),('GET','/orders/DEMAND-A/lines',{})]:
        pre=pc(req(client,method,path,params,view=True))
        response=req(client,method,path,params,precondition=pre)
        assert response.status_code==200,response.text
        assert 'authorization_context' in response.json()
    data=req(client,'POST','/search',{}).json()
    assert 'PRIVATE RECEIVER' in json.dumps(data)
    grant(db,uid,fields=[])
    data=req(client,'POST','/search',{}).json()
    assert 'PRIVATE RECEIVER' not in json.dumps(data)
    assert 'PRIVATE ADDRESS' not in json.dumps(data)
    grant(db,uid,scope='assigned_projects')
    db.add(Manager(assignment_id=str(uuid4()),project_id=pid,user_id=uid,responsibility_type='viewer',assigned_by='test',assignment_reason='actual scope'));db.commit()
    data=req(client,'POST','/search',{}).json()
    assert data['total']==1
    assert [r['source_order_id'] for r in data['items']]==['DEMAND-A']
    assert req(client,'GET','/orders/DEMAND-B/lines').status_code==403


def test_manual_create_patch_clear_and_exact_input_binding(db,workflow):
    client,uid,pid,_=workflow
    create={'order_date':'2026-10-23','project_id':pid,'pn_std':'DEMAND-PN','qty':2,'return_qty':0,'serial_numbers':None,'description':'exact description','reason':'actual create','idempotency_key':'create-demand-canonical'}
    pre=pc(req(client,'POST','/lines',create,view=True))
    assert req(client,'POST','/lines',{**create,'description':'changed'},precondition=pre).status_code==409
    created=command(client,'POST','/lines',create)
    source=db.scalar(select(Order.raw_order_id).where(Order.order_no==created['order_no']))
    line=req(client,'GET',f'/orders/{source}/lines').json()['items'][0]
    patch={'updates':{'description':'changed by actual user'},'reason':'actual edit','expected_digest':line['digest']}
    pre=pc(req(client,'PATCH',f"/lines/{line['raw_line_id']}",patch,view=True))
    assert req(client,'PATCH',f"/lines/{line['raw_line_id']}",{**patch,'updates':{'description':'different input'}},precondition=pre).status_code==409
    updated=command(client,'PATCH',f"/lines/{line['raw_line_id']}",patch)
    assert updated['description']=='changed by actual user'
    cleared=command(client,'POST',f"/lines/{line['raw_line_id']}/clear-override",{'field_name':'description','reason':'clear actual override','expected_digest':updated['digest']})
    assert 'description' not in cleared['manual_override']
    actor=db.get(SysUser,uid);actor.username='renamed-demand-user';db.commit();login(client,actor)
    replay=req(client,'POST','/lines',create)
    assert replay.status_code==201,replay.text
    assert replay.json()['raw_line_id']==created['raw_line_id']


def test_fast_void_current_receipt_restore_and_stable_owner(db,workflow):
    client,uid,_,_=workflow
    before=db.scalar(select(func.count()).select_from(Intent))
    pre=pc(req(client,'POST','/void-fast',body(),view=True))
    assert db.scalar(select(func.count()).select_from(Intent))==before
    result=req(client,'POST','/void-fast',body(),precondition=pre)
    assert result.status_code==200,result.text
    iid=result.json()['intent_id'];assert db.get(Intent,iid).owner_user_id==uid
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact))==1
    actor=db.get(SysUser,uid);actor.username='renamed-demand-user';db.commit();login(client,actor)
    replay=command(client,'POST','/void-fast',body())
    assert replay['replayed'] is True
    read=req(client,'GET',f'/delete-intents/{iid}')
    assert read.status_code==200,read.text
    grant(db,uid,actions=['read_delete_intent'])
    assert req(client,'GET',f'/delete-intents/{iid}').status_code==200
    assert req(client,'POST','/void-fast',body()).status_code==403
    grant(db,uid,actions=list(RESOURCE_DEFINITIONS[R].actions))
    restored=command(client,'POST','/DEMAND-A/restore',{'reason':'actual restore'})
    assert restored
    assert req(client,'POST','/void-fast',body()).status_code==409


def test_two_phase_plan_arm_wait_execute_and_cancel(db,workflow):
    client,uid,_,_=workflow
    plan=command(client,'POST','/delete-intents',body())
    iid=plan['intent_id'];action={'digest':plan['selection_digest']}
    assert req(client,'GET',f'/delete-intents/{iid}').status_code==200
    armed=command(client,'POST',f'/delete-intents/{iid}/arm',action)
    early=req(client,'POST',f'/delete-intents/{iid}/execute',action)
    assert early.status_code==425,early.text
    row=db.get(Intent,iid);row.not_before=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    result=command(client,'POST',f'/delete-intents/{iid}/execute',action)
    assert result['header_count']==1
    assert req(client,'POST',f'/delete-intents/{iid}/execute',action).status_code==200
    plan=command(client,'POST','/delete-intents',body(source_order_ids=['DEMAND-B'],idempotency_key='demand-cancel-other'))
    cancelled=command(client,'POST',f"/delete-intents/{plan['intent_id']}/cancel",{'digest':plan['selection_digest']})
    assert cancelled['status']=='cancelled'


def test_batch_requires_every_actual_project_and_all_views_do_not_write(db,workflow):
    client,uid,pid,_=workflow
    grant(db,uid,scope='assigned_projects')
    db.add(Manager(assignment_id=str(uuid4()),project_id=pid,user_id=uid,responsibility_type='primary_manager',assigned_by='test',assignment_reason='actual scope'));db.commit()
    for path in ('/void-fast','/delete-intents'):
        view=req(client,'POST',path,body(source_order_ids=['DEMAND-A','DEMAND-B']),view=True)
        assert view.status_code==200,view.text
        assert view.json()['authorization_context']['resources'][R]['decisions'][('void' if path=='/void-fast' else 'void_plan')]['effect']=='deny'
        assert req(client,'POST',path,body(source_order_ids=['DEMAND-A','DEMAND-B'])).status_code==403
    assert db.scalar(select(func.count()).select_from(Intent))==0
    assert db.scalar(select(func.count()).select_from(Tombstone))==0


def test_all_thirteen_entries_deny_independently_and_views_never_execute(db,workflow):
    client,uid,pid,_=workflow
    plan=command(client,'POST','/delete-intents',body());iid=plan['intent_id'];digest=plan['selection_digest']
    cases=[('POST','/search',{}),('POST','/void-fast',body(idempotency_key='independent-void-key')),
        ('POST','/delete-intents',body(idempotency_key='independent-plan-key')),('GET',f'/delete-intents/{iid}',{}),
        *[('POST',f'/delete-intents/{iid}/{action}',{'digest':digest}) for action in ('arm','execute','cancel')],
        ('POST','/DEMAND-A/restore',{'reason':'actual restore'}),('GET','/manual-protection-review',{}),
        ('PATCH','/lines/DEMAND-LINE-A',{'updates':{'qty':3},'reason':'edit','expected_digest':'0'*64}),
        ('POST','/lines',{'order_date':'2026-10-23','project_id':pid,'pn_std':'DEMAND-PN','qty':1,'reason':'create','idempotency_key':'independent-create-key'}),
        ('POST','/lines/DEMAND-LINE-A/clear-override',{'field_name':'qty','reason':'clear','expected_digest':'0'*64}),
        ('GET','/orders/DEMAND-A/lines',{})]
    assert len(cases)==len(protected.ENTRIES)==13
    grant(db,uid,actions=[])
    before={model:db.scalar(select(func.count()).select_from(model)) for model in (Intent,Tombstone,Event,Order,Line,SysAuthorizationArtifact)}
    for method,path,params in cases:
        view=req(client,method,path,params,view=True)
        assert view.status_code==200,(path,view.text)
        assert set(view.json())=={'locator','authorization_context'}
        assert all(d['effect']=='deny' for d in view.json()['authorization_context']['resources'][R]['decisions'].values())
        assert req(client,method,path,params).status_code==403,path
    for model,count in before.items():assert db.scalar(select(func.count()).select_from(model))==count


def test_reused_username_cannot_claim_intent_and_null_owner_is_never_adopted(db,workflow):
    client,uid,_,_=workflow
    plan=command(client,'POST','/delete-intents',body());actor=db.get(SysUser,uid)
    actor.username='renamed-owner';db.commit()
    other=SysUser(username='demand-canonical',role='readonly',password_hash='synthetic');db.add(other);db.flush()
    db.add(SysAuthorizationGrant(user_id=other.id,resource=R,actions=list(RESOURCE_DEFINITIONS[R].actions),scope='all_projects',field_permissions=[],version=1));db.commit();login(client,other)
    assert req(client,'GET',f"/delete-intents/{plan['intent_id']}").status_code==403
    assert req(client,'POST','/delete-intents',body()).status_code==403
    assert req(client,'POST',f"/delete-intents/{plan['intent_id']}/cancel",{'digest':plan['selection_digest']}).status_code==403
    legacy=business.create_delete_intent(db,**body(idempotency_key='legacy-unknown-owner'),operated_by=actor.username);db.commit();login(client,actor)
    assert req(client,'GET',f"/delete-intents/{legacy['intent_id']}").status_code==403
    assert req(client,'POST','/delete-intents',body(idempotency_key='legacy-unknown-owner')).status_code==403
    assert db.get(Intent,legacy['intent_id']).owner_user_id is None


@pytest.mark.parametrize('consumer',['read','replay'])
def test_receipt_manifest_poison_rejects_actual_history(db,workflow,consumer):
    client,_,_,_=workflow
    result=command(client,'POST','/void-fast',body());iid=result['intent_id']
    row=db.get(SysAuthorizationArtifact,(protected.KIND,iid))
    manifests=json.loads(json.dumps(row.artifact_manifests));manifests['fast_'+consumer]['content_sha256']='0'*64
    row.artifact_manifests=manifests;db.commit()
    response=req(client,'GET',f'/delete-intents/{iid}') if consumer=='read' else req(client,'POST','/void-fast',body())
    assert response.status_code==403,response.text
    assert db.scalar(select(func.count()).select_from(Intent))==1


def test_published_then_failed_receipt_rolls_back_tombstones_assignments_and_manifest(db,workflow,monkeypatch):
    client,_,_,_=workflow
    original=protected.artifacts.publish_artifact
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('late demand receipt publish failure')
    monkeypatch.setattr(protected.artifacts,'publish_artifact',fail)
    before={model:db.scalar(select(func.count()).select_from(model)) for model in (Intent,Tombstone,Event,SysAuthorizationArtifact)}
    with pytest.raises(RuntimeError,match='late demand receipt'):
        req(client,'POST','/void-fast',body())
    for model,count in before.items():assert db.scalar(select(func.count()).select_from(model))==count
    assert db.scalar(select(Assignment.is_active).where(Assignment.source_order_id=='DEMAND-A')) is True


def test_staged_tombstones_recheck_current_grant_before_commit(db,workflow,monkeypatch):
    from sqlalchemy.orm import Session
    client,uid,_,_=workflow
    original=protected._write
    def revoke(session,entry,params,principal):
        payload=original(session,entry,params,principal)
        with Session(db.get_bind()) as second:
            row=second.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==uid,SysAuthorizationGrant.resource==R))
            row.actions=['read'];row.version+=1;second.commit()
        return payload
    monkeypatch.setattr(protected,'_write',revoke)
    response=req(client,'POST','/void-fast',body())
    assert response.status_code==403,response.text
    assert db.scalar(select(func.count()).select_from(Intent))==0
    assert db.scalar(select(func.count()).select_from(Tombstone))==0
    assert db.scalar(select(Assignment.is_active).where(Assignment.source_order_id=='DEMAND-A')) is True


@pytest.mark.parametrize('change',['revoke','move_parent'])
def test_real_project_lock_wait_rechecks_current_actor_and_actual_parent(db,workflow,change):
    from concurrent.futures import ThreadPoolExecutor
    from types import MappingProxyType
    import time
    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.authz.principal import VerifiedIdentity
    from app.models.maintenance_project import MaintenanceProject
    client,uid,pid,otherpid=workflow
    expected=pc(req(client,'POST','/void-fast',body(),view=True))
    actor=db.get(SysUser,uid)
    identity=VerifiedIdentity(actor.username,actor.token_version,False,'sys_user',MappingProxyType({}),uid)
    bind=db.get_bind();db.rollback()
    def run():
        with Session(bind) as worker:
            worker.execute(text("SET application_name='demand-authz-lock-test'"))
            try:
                protected.execute(worker,identity,protected.FAST,body(),precondition=expected)
                return 200
            except HTTPException as exc:return exc.status_code
    with Session(bind) as blocker:
        blocker.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id==pid).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(run);waited=False
            for _ in range(150):
                with Session(bind) as observer:
                    waited=bool(observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name='demand-authz-lock-test' AND wait_event_type='Lock')")))
                if waited:break
                time.sleep(.02)
            assert waited,'the command must actually wait on its project row'
            if change=='revoke':grant(db,uid,actions=['read'])
            else:
                row=blocker.scalar(select(Assignment).where(Assignment.source_order_id=='DEMAND-A',Assignment.is_active.is_(True)))
                row.is_active=False;row.version+=1;row.archived_at=datetime.now(timezone.utc);row.archived_by='actual transfer'
                blocker.flush()
                blocker.add(Assignment(assignment_id=str(uuid4()),source_order_id='DEMAND-A',project_id=otherpid,is_active=True,created_by='actual transfer'))
            blocker.commit()
            assert future.result(timeout=15)==(403 if change=='revoke' else 409)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Intent))==0
    assert db.scalar(select(func.count()).select_from(Tombstone))==0


def test_changed_plan_targets_cannot_reuse_input_precondition(db,workflow):
    client,_,_,_=workflow
    pre=pc(req(client,'POST','/void-fast',body(),view=True))
    response=req(client,'POST','/void-fast',body(source_order_ids=['DEMAND-A','DEMAND-B']),precondition=pre)
    assert response.status_code==409,response.text
    assert db.scalar(select(func.count()).select_from(Intent))==0


def test_terminal_occ_conflict_persists_only_with_current_authorization(db,workflow):
    client,_,_,_=workflow
    plan=command(client,'POST','/delete-intents',body());iid=plan['intent_id'];action={'digest':plan['selection_digest']}
    command(client,'POST',f'/delete-intents/{iid}/arm',action)
    row=db.get(Intent,iid);row.not_before=datetime.now(timezone.utc)-timedelta(seconds=1)
    line=db.scalar(select(Line).where(Line.raw_line_id=='DEMAND-LINE-A'));line.description='concurrent business edit';db.commit()
    result=req(client,'POST',f'/delete-intents/{iid}/execute',action)
    assert result.status_code==409,result.text
    db.expire_all();assert db.get(Intent,iid).status=='conflicted'
    assert db.scalar(select(func.count()).select_from(Tombstone))==0


def test_forged_outer_workflow_permits_cannot_authorize_demand_side_effect(db,workflow):
    from fastapi import HTTPException
    for key in ('_source_authorization','_wbdd_authorization'):
        with pytest.raises(HTTPException) as raised:
            business.void_fast(db,**body(),operated_by='demand-canonical',**{key:object()})
        assert raised.value.status_code==403
    assert db.scalar(select(func.count()).select_from(Intent))==0


def test_stable_actual_intent_owner_blocks_lossy_downgrade(db):
    from tests.test_authz_s3_migration import migrate,S2
    order,_=_seed_line(db,order_raw='DOWNGRADE-DEMAND',line_raw='DOWNGRADE-LINE',pn='DOWNGRADE-PN')
    _make_project(db,tag='downgrade',source_order_id=order.raw_order_id)
    actor=SysUser(username='demand-stable-owner',role='readonly',password_hash='synthetic');db.add(actor);db.flush()
    business.create_delete_intent(db,source_order_ids=[order.raw_order_id],reason='actual planned deletion',idempotency_key='demand-downgrade-owner',operated_by=actor.username,owner_user_id=actor.id)
    with pytest.raises(RuntimeError,match='stable command or file owner'):
        migrate(db,S2,down=True)
    db.rollback()


def test_existing_plan_replay_requires_historical_and_current_parent(db,workflow):
    client,uid,pid,otherpid=workflow
    plan=command(client,'POST','/delete-intents',body())
    row=db.scalar(select(Assignment).where(Assignment.source_order_id=='DEMAND-A',Assignment.is_active.is_(True)))
    row.is_active=False;row.version+=1;row.archived_at=datetime.now(timezone.utc);row.archived_by='actual transfer';db.flush()
    db.add(Assignment(assignment_id=str(uuid4()),source_order_id='DEMAND-A',project_id=otherpid,is_active=True,created_by='actual transfer'))
    db.add(Manager(assignment_id=str(uuid4()),project_id=otherpid,user_id=uid,responsibility_type='viewer',assigned_by='test',assignment_reason='new project only'));db.commit()
    grant(db,uid,scope='assigned_projects')
    assert req(client,'GET','/orders/DEMAND-A/lines').status_code==200
    assert req(client,'POST','/delete-intents',body()).status_code==403
    assert req(client,'GET',f"/delete-intents/{plan['intent_id']}").status_code==403
    assert db.scalar(select(func.count()).select_from(Intent))==1


def test_new_manual_source_cannot_escape_captured_project_before_commit(db,workflow,monkeypatch):
    client,_,pid,otherpid=workflow
    original=protected._write
    def move_new_source(session,entry,params,principal):
        result=original(session,entry,params,principal)
        source=session.scalar(select(Order.raw_order_id).where(Order.order_no==result['order_no']))
        row=session.scalar(select(Assignment).where(Assignment.source_order_id==source,Assignment.is_active.is_(True)))
        row.is_active=False;row.version+=1;row.archived_at=datetime.now(timezone.utc);row.archived_by='adversarial move';session.flush()
        session.add(Assignment(assignment_id=str(uuid4()),source_order_id=source,project_id=otherpid,is_active=True,created_by='adversarial move'))
        session.flush();return result
    monkeypatch.setattr(protected,'_write',move_new_source)
    response=req(client,'POST','/lines',{'order_date':'2026-10-23','project_id':pid,'pn_std':'DEMAND-PN','qty':1,'reason':'actual create','idempotency_key':'unexpected-parent-create'})
    assert response.status_code==409,response.text
    assert db.scalar(select(func.count()).select_from(Order))==2
    assert db.scalar(select(func.count()).select_from(Line))==2


def test_void_only_command_does_not_borrow_receipt_read_or_legacy_admin(db,workflow):
    client,uid,_,_=workflow
    grant(db,uid,actions=['void'],fields=[])
    result=command(client,'POST','/void-fast',body())
    iid=result['intent_id']
    assert req(client,'GET',f'/delete-intents/{iid}').status_code==403
    assert req(client,'POST','/void-fast',body()).status_code==200
    grant(db,uid,actions=['read_delete_intent'],fields=[])
    assert req(client,'GET',f'/delete-intents/{iid}').status_code==200
    assert req(client,'POST','/void-fast',body()).status_code==403
