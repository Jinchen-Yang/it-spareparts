"""Real PostgreSQL/Cerbos return chains: independent actions and actual parents."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import maintenance_bad_returns, maintenance_bad_salvage, maintenance_return_receipts
from app.auth import _make_token
from app.authz import runtime
from app.authz.catalog import RESOURCE_DEFINITIONS
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance_bad_return import MaintenanceBadReturnCommand as Command
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage as Salvage
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine as Receipt
from app.models.maintenance_project import MaintenanceProjectUserAssignment as Assignment
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit as Audit
from app.models.system import SysUser
from app.services import maintenance_return_authorization as protected
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_maintenance_bad_returns_api import _client, _one_required_obligation
from tests.test_site_issue_v2_api import _project

P='/api/maintenance'
R='maintenance_return_receipt'
B='maintenance_return_obligation'
S='maintenance_salvage'
C='maintenance_return_categories'


def login(client, actor):
    token,_=_make_token(actor.role,actor.username,None,token_version=actor.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token


@pytest.fixture
def workflow(db,monkeypatch,cerbos_url):
    legacy=_client(db,username='return-seed')
    _,obligation=_one_required_obligation(db,legacy,project_id='return-A',suffix=uuid4().hex[:8])
    _project(db,project_id='return-B');db.commit()
    settings=get_settings()
    for k,v in {'authorization_mode':'unified_modules','authz_enabled_resources':sorted(protected.RESOURCES),
        'authz_suspended_resources':[],'authz_sales_analysis_enabled':False,'authz_sales_analysis_suspended':False,
        'cerbos_url':cerbos_url,'cerbos_timeout_seconds':2.0,'maintenance_boss_dashboard_enabled':True}.items():monkeypatch.setattr(settings,k,v)
    runtime.activate_configured_resources(db.get_bind())
    actor=SysUser(username='return-canonical',role='readonly',password_hash='synthetic');db.add(actor);db.flush()
    for resource in protected.RESOURCES:
        definition=RESOURCE_DEFINITIONS[resource]
        db.add(SysAuthorizationGrant(user_id=actor.id,resource=resource,actions=list(definition.actions),
            field_permissions=list(definition.field_permissions),scope='all' if resource==C else 'all_projects',version=1))
    db.commit()
    app=FastAPI()
    for api in (maintenance_bad_returns,maintenance_bad_salvage,maintenance_return_receipts):app.include_router(api.router,prefix='/api')
    app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);login(client,actor)
    return client,actor.id,obligation.obligation_id


def pc(response):
    assert response.status_code==200,response.text
    context=response.json()['authorization_context']
    return {k:context[k] for k in ('catalog_version','authorization_epoch','context')}


def request(client,method,path,body=None,*,view=False,precondition=None):
    path=P+path
    if method=='GET':
        return client.get(path,params={**(body or {}),**({'view':'authorization'} if view else {}),
            **({'authorization_precondition':json.dumps(precondition)} if precondition else {})})
    return client.request(method,path+('?view=authorization' if view else ''),json={**(body or {}),
        **({'authorization_precondition':precondition} if precondition else {})})


def command(client,method,path,body):
    pre=pc(request(client,method,path,body,view=True))
    result=request(client,method,path,body,precondition=pre)
    assert result.status_code in (200,201),result.text
    return result.json()


def grant(db,uid,resource,*,actions=None,scope=None,fields=None):
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==uid,SysAuthorizationGrant.resource==resource))
    if actions is not None:row.actions=actions
    if scope is not None:row.scope=scope
    if fields is not None:row.field_permissions=fields
    row.version+=1;db.commit()


def bad_body(oid):
    return {'project_id':'return-A','idempotency_key':'bad-create-canonical','lines':[{'obligation_id':oid,'quantity':'1'}],
            'reason':'actual returned part','note':None,'replaces_return_id':None}


def receipt_body(**extra):
    return {'pn':'RETURN-PN','qty':1,'idempotency_key':'receipt-canonical',**extra}


def salvage_body():
    return {'pn':'RETURN-PN','part_id':None,'qty':'1','revenue':'30','salvage_date':'2026-10-23',
            'buyer_note':'actual buyer','reason':'recovery','idempotency_key':'salvage-canonical'}


def test_every_read_uses_own_contract_and_context(db,workflow):
    client,uid,oid=workflow
    reads=[('POST','/return-obligations/search',{'project_id':'return-A'}),
        ('POST','/bad-returns/search',{'project_id':'return-A'}),('GET','/projects/stable/return-A/return-rate',{}),
        ('GET','/return-categories',{}),('GET','/projects/stable/return-A/salvages',{}),
        ('GET','/projects/stable/return-A/return-receipt-demands',{}),('GET','/projects/stable/return-A/return-receipt-summary',{}),
        ('GET','/projects/stable/return-A/return-receipts',{})]
    before=db.scalar(select(func.count()).select_from(Audit))
    for method,path,body in reads:
        view=request(client,method,path,body,view=True)
        assert set(view.json())=={'locator','authorization_context'},view.text
        response=request(client,method,path,body,precondition=pc(view))
        assert response.status_code==200,response.text
        assert 'authorization_context' in response.json()
    assert db.scalar(select(func.count()).select_from(Audit))==before
    grant(db,uid,C,actions=[])
    assert request(client,'GET','/return-categories').status_code==403
    assert request(client,'GET','/projects/stable/return-A/return-rate').status_code==200


def test_bad_return_complete_lifecycle_with_independent_actions(db,workflow):
    client,uid,oid=workflow
    created=command(client,'POST','/bad-returns',bad_body(oid));rid=created['return_id']
    for action in ('submit','in-transit','warehouse-confirm','void'):
        body={'project_id':'return-A','version':created['version'],'idempotency_key':'bad-'+action+'-canonical','reason':'actual '+action}
        if action=='in-transit':body['logistics_reference']='LOG-CANONICAL'
        if action=='warehouse-confirm':body['warehouse_reference']='WAREHOUSE-CANONICAL'
        created=command(client,'POST',f'/bad-returns/{rid}/{action}',body)
    assert created['status']=='void'
    assert {row.owner_user_id for row in db.scalars(select(Command))}=={uid}
    grant(db,uid,B,actions=['read'])
    assert request(client,'POST','/bad-returns',bad_body(oid)).status_code==403


def test_salvage_whole_financial_contract_and_void(db,workflow):
    client,uid,_=workflow
    grant(db,uid,S,fields=[])
    assert request(client,'GET','/projects/stable/return-A/salvages').status_code==403
    grant(db,uid,S,fields=['data_purchase_cost','data_profit'])
    result=command(client,'POST','/projects/stable/return-A/salvages',salvage_body())
    assert db.get(Salvage,result['salvage_id']).owner_user_id==uid
    result=command(client,'POST',f"/salvages/{result['salvage_id']}/void",{'project_id':'return-A','version':1,'reason':'void recovered item'})
    assert result['is_active'] is False


def test_receipt_create_update_transfer_void_and_audit(db,workflow):
    client,uid,_=workflow
    created=command(client,'POST','/projects/stable/return-A/return-receipts',receipt_body(description='first'))
    rid=created['receipt_id'];assert db.get(Receipt,rid).owner_user_id==uid
    changed=command(client,'PATCH',f'/return-receipts/{rid}',{'version':1,'reason':'transfer actual receipt','project_id':'return-B','description':'second'})
    assert changed['project_id']=='return-B'
    view=request(client,'GET',f'/return-receipts/{rid}/audit',view=True)
    audit=request(client,'GET',f'/return-receipts/{rid}/audit',precondition=pc(view))
    assert audit.status_code==200,audit.text
    assert len(audit.json()['items'])==3
    result=command(client,'POST',f'/return-receipts/{rid}/void',{'version':2,'reason':'actual void'})
    assert result['line_status']=='voided'


def test_changed_description_cannot_reuse_authorization_context(db,workflow):
    client,_,_=workflow
    body=receipt_body(description='original')
    view=request(client,'POST','/projects/stable/return-A/return-receipts',body,view=True)
    result=request(client,'POST','/projects/stable/return-A/return-receipts',{**body,'description':'different'},precondition=pc(view))
    assert result.status_code==409,result.text
    assert db.scalar(select(func.count()).select_from(Receipt))==0


def test_cross_parent_transfer_requires_both_actual_projects_and_audit_filters(db,workflow):
    client,uid,_=workflow
    created=command(client,'POST','/projects/stable/return-A/return-receipts',receipt_body())
    rid=created['receipt_id']
    grant(db,uid,R,scope='assigned_or_sales_projects')
    db.add(Assignment(assignment_id=str(uuid4()),project_id='return-A',user_id=uid,responsibility_type='viewer',assigned_by='test',assignment_reason='test actual scope'));db.commit()
    denied=request(client,'PATCH',f'/return-receipts/{rid}',{'version':1,'reason':'transfer','project_id':'return-B'})
    assert denied.status_code==403,denied.text
    assert db.get(Receipt,rid).project_id=='return-A'
    grant(db,uid,R,scope='all_projects')
    command(client,'PATCH',f'/return-receipts/{rid}',{'version':1,'reason':'transfer','project_id':'return-B'})
    grant(db,uid,R,scope='assigned_or_sales_projects')
    old=db.scalar(select(Assignment).where(Assignment.project_id=='return-A'));old.version+=1;old.archived_at=datetime.now(timezone.utc);old.archived_by='test';old.archive_reason='scope moved';db.add(Assignment(assignment_id=str(uuid4()),project_id='return-B',user_id=uid,responsibility_type='viewer',assigned_by='test',assignment_reason='test actual scope'));db.commit()
    audit=request(client,'GET',f'/return-receipts/{rid}/audit')
    assert audit.status_code==200,audit.text
    assert audit.json()['items']==[]


@pytest.mark.parametrize('kind',['bad','salvage','receipt'])
def test_replay_uses_stable_owner_and_legacy_null_is_unclaimable(db,workflow,kind):
    client,uid,oid=workflow
    path,body={'bad':('/bad-returns',bad_body(oid)), 'salvage':('/projects/stable/return-A/salvages',salvage_body()),
               'receipt':('/projects/stable/return-A/return-receipts',receipt_body())}[kind]
    result=command(client,'POST',path,body)
    actor=db.get(SysUser,uid);actor.username='renamed-return-canonical';db.commit();login(client,actor)
    assert request(client,'POST',path,body).status_code in (200,201)
    other=SysUser(username='return-canonical',role='readonly',password_hash='synthetic');db.add(other);db.flush()
    for row in db.scalars(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==uid)):
        db.add(SysAuthorizationGrant(user_id=other.id,resource=row.resource,actions=row.actions,scope=row.scope,field_permissions=row.field_permissions,version=1))
    db.commit();login(client,other)
    assert request(client,'POST',path,body).status_code==403
    login(client,actor)
    legacy={**body,'idempotency_key':'legacy-unknown-owner'}
    if kind=='bad':
        protected.bad.create_bad_return(db,**legacy,operated_by=actor.username)
    elif kind=='salvage':
        protected.salvage.register_salvage(db,project_id='return-A',
            **maintenance_bad_salvage.SalvageCreate(**legacy).model_dump(exclude={'authorization_precondition'}),operated_by=actor.username)
    else:
        protected.receipts.register_receipt(db,project_id='return-A',**legacy,operated_by=actor.username)
    db.commit()
    assert request(client,'POST',path,legacy).status_code==403


def test_category_resolution_is_independent_and_authorization_view_has_no_writes(db,workflow):
    from app.models.maintenance_bad_return import MaintenanceReturnObligation as Obligation
    from app.models.master_data import ProductCategory
    client,uid,oid=workflow
    row=db.get(Obligation,oid)
    category=db.scalar(select(ProductCategory.id))
    row.classification='pending_category';row.required_quantity=0;row.exemption_source=None
    row.category_id_snapshot=None;row.category_major_snapshot=None;row.category_minor_snapshot=None;db.commit()
    body={'project_id':'return-A','version':row.version,'category_id':category,
          'idempotency_key':'resolve-category-canonical','reason':'actual category checked'}
    url=f'/return-obligations/{oid}/resolve-category'
    before=db.scalar(select(func.count()).select_from(Command))
    view=request(client,'POST',url,body,view=True)
    assert db.scalar(select(func.count()).select_from(Command))==before
    result=request(client,'POST',url,body,precondition=pc(view))
    assert result.status_code==200,result.text
    assert result.json()['classification']=='required'
    grant(db,uid,B,actions=['read'])
    assert request(client,'POST',url,body).status_code==403


@pytest.mark.parametrize('change',['revoke','move_parent'])
def test_real_domain_lock_wait_rechecks_authorization_and_parent(db,workflow,change):
    from concurrent.futures import ThreadPoolExecutor
    from types import MappingProxyType
    import time
    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.authz.principal import VerifiedIdentity
    from app.models.maintenance_project import MaintenanceProject
    client,uid,_=workflow
    result=command(client,'POST','/projects/stable/return-A/return-receipts',receipt_body())
    rid=result['receipt_id'];url=f'/return-receipts/{rid}'
    body={'version':1,'reason':'locked edit','note':'updated'}
    expected=pc(request(client,'PATCH',url,body,view=True))
    actor=db.get(SysUser,uid)
    identity=VerifiedIdentity(actor.username,actor.token_version,False,'sys_user',MappingProxyType({}),uid)
    bind=db.get_bind();db.rollback()
    def run():
        with Session(bind) as worker:
            worker.execute(text("SET application_name='return-authz-lock-test'"))
            try:
                protected.execute(worker,identity,'PATCH /api/maintenance/return-receipts/{receipt_id}',
                    {**body,'receipt_id':rid},precondition=expected)
                return 200
            except HTTPException as exc:return exc.status_code
    with Session(bind) as blocker:
        blocker.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id=='return-A').with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(run);waited=False
            for _ in range(150):
                with Session(bind) as observer:
                    waited=bool(observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name='return-authz-lock-test' AND wait_event_type='Lock')")))
                if waited:break
                time.sleep(.02)
            assert waited,'worker must wait for the held project row'
            if change=='revoke':grant(db,uid,R,actions=['read_receipts'])
            else:
                row=blocker.get(Receipt,rid);row.project_id='return-B';row.version+=1
            blocker.commit()
            assert future.result(timeout=15) in ({403} if change=='revoke' else {409})
    db.expire_all();row=db.get(Receipt,rid)
    assert row.note is None
    assert row.version==(1 if change=='revoke' else 2)


def test_late_receipt_publication_failure_rolls_back_all_domain_work(db,workflow,monkeypatch):
    from app.models.authorization import SysAuthorizationArtifact
    client,_,oid=workflow
    original=protected.artifacts.publish_artifact
    def fail_after_manifest(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('late receipt publication failure')
    monkeypatch.setattr(protected.artifacts,'publish_artifact',fail_after_manifest)
    counts={model:db.scalar(select(func.count()).select_from(model)) for model in
            (protected.BadReturn,protected.BadLine,Command,Audit,SysAuthorizationArtifact)}
    with pytest.raises(RuntimeError,match='late receipt publication'):
        request(client,'POST','/bad-returns',bad_body(oid))
    for model,count in counts.items():assert db.scalar(select(func.count()).select_from(model))==count


@pytest.mark.parametrize('kind',['bad','salvage','receipt'])
def test_stable_actual_return_owner_blocks_lossy_downgrade(db,kind):
    from tests.test_authz_s3_migration import migrate,S2
    legacy=_client(db,username='stable-owner-downgrade')
    _,obligation=_one_required_obligation(db,legacy,project_id='return-A',suffix=uuid4().hex[:8])
    actor=db.scalar(select(SysUser).where(SysUser.username=='stable-owner-downgrade'))
    if kind=='bad':
        protected.bad.create_bad_return(db,**bad_body(obligation.obligation_id),operated_by=actor.username,owner_user_id=actor.id)
    elif kind=='salvage':
        protected.salvage.register_salvage(db,project_id='return-A',
            **maintenance_bad_salvage.SalvageCreate(**salvage_body()).model_dump(exclude={'authorization_precondition'}),
            operated_by=actor.username,owner_user_id=actor.id)
    else:
        protected.receipts.register_receipt(db,project_id='return-A',**receipt_body(),operated_by=actor.username,owner_user_id=actor.id)
    with pytest.raises(RuntimeError,match='stable command or file owner'):
        migrate(db,S2,down=True)
    db.rollback()


def test_all_twenty_registered_entries_deny_independently_without_business_payload(db,workflow):
    client,uid,oid=workflow
    returned=command(client,'POST','/bad-returns',bad_body(oid))
    receipt=command(client,'POST','/projects/stable/return-A/return-receipts',receipt_body())
    sale=command(client,'POST','/projects/stable/return-A/salvages',salvage_body())
    cases=[('POST','/return-obligations/search',{'project_id':'return-A'}),
        ('GET','/projects/stable/return-A/return-rate',{}),('GET','/return-categories',{}),
        ('POST','/bad-returns/search',{'project_id':'return-A'}),('POST','/bad-returns',bad_body(oid)),
        ('POST',f'/return-obligations/{oid}/resolve-category',{'project_id':'return-A','version':1,'category_id':1,'idempotency_key':'deny-category-key','reason':'deny'}),
        ('GET','/projects/stable/return-A/salvages',{}),('POST','/projects/stable/return-A/salvages',salvage_body()),
        ('POST',f"/salvages/{sale['salvage_id']}/void",{'project_id':'return-A','version':1,'reason':'deny'}),
        ('GET','/projects/stable/return-A/return-receipt-demands',{}),
        ('GET','/projects/stable/return-A/return-receipt-summary',{}),
        ('GET','/projects/stable/return-A/return-receipts',{}),
        ('POST','/projects/stable/return-A/return-receipts',receipt_body()),
        ('PATCH',f"/return-receipts/{receipt['receipt_id']}",{'version':1,'reason':'deny','note':'new'}),
        ('POST',f"/return-receipts/{receipt['receipt_id']}/void",{'version':1,'reason':'deny'}),
        ('GET',f"/return-receipts/{receipt['receipt_id']}/audit",{})]
    for name in ('submit','in-transit','warehouse-confirm','void'):
        body={'project_id':'return-A','version':1,'idempotency_key':'deny-'+name,'reason':'deny'}
        if name=='in-transit':body['logistics_reference']='actual'
        if name=='warehouse-confirm':body['warehouse_reference']='actual'
        cases.append(('POST',f"/bad-returns/{returned['return_id']}/{name}",body))
    assert len(cases)==len(protected.ENTRIES)==20
    for resource in protected.RESOURCES:grant(db,uid,resource,actions=[])
    before={model:db.scalar(select(func.count()).select_from(model)) for model in (Command,Receipt,Salvage,Audit)}
    for method,path,body in cases:
        view=request(client,method,path,body,view=True)
        assert view.status_code==200,(path,view.text)
        assert set(view.json())=={'locator','authorization_context'}
        assert all(d['effect']=='deny' for r in view.json()['authorization_context']['resources'].values() for d in r['decisions'].values())
        result=request(client,method,path,body)
        assert result.status_code==403,(path,result.text)
    for model,count in before.items():assert db.scalar(select(func.count()).select_from(model))==count


def test_staged_write_rechecks_live_grant_before_commit(db,workflow,monkeypatch):
    from sqlalchemy.orm import Session
    client,uid,_=workflow
    original=protected._write
    def revoke_after_staging(session,entry,params,principal):
        result=original(session,entry,params,principal)
        with Session(db.get_bind()) as other:
            row=other.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==uid,SysAuthorizationGrant.resource==R))
            row.actions=[];row.version+=1;other.commit()
        return result
    monkeypatch.setattr(protected,'_write',revoke_after_staging)
    result=request(client,'POST','/projects/stable/return-A/return-receipts',receipt_body())
    assert result.status_code==403,result.text
    assert db.scalar(select(func.count()).select_from(Receipt))==0
    assert db.scalar(select(func.count()).select_from(Audit).where(Audit.entity_type=='return_receipt'))==0


def test_bad_return_historical_receipt_checks_actual_content_manifest(db,workflow):
    from app.models.authorization import SysAuthorizationArtifact
    client,_,oid=workflow
    body=bad_body(oid);command(client,'POST','/bad-returns',body)
    row=db.scalar(select(Command));binding=db.get(SysAuthorizationArtifact,(protected.KIND,row.command_id))
    manifests=json.loads(json.dumps(binding.artifact_manifests))
    manifests['create']['content_sha256']='0'*64;binding.artifact_manifests=manifests;db.commit()
    response=request(client,'POST','/bad-returns',body)
    assert response.status_code==403,response.text
    assert db.scalar(select(func.count()).select_from(Command))==1
