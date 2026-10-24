"""Whole original, async identity and final atomic return-import authorization."""
import json
from types import MappingProxyType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.api import maintenance_return_receipt_import as api
from app.auth import _make_token
from app.authz import runtime
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact,SysAuthorizationGrant
from app.models.system import SysUser
from app.services import maintenance_return_import_authorization as authz
from app.services import maintenance_return_receipt_import as business
from tests.test_authz_s3_checklist import setup,cerbos_url  # noqa: F401
from tests.test_return_receipt_import import workbook


@pytest.fixture
def job(db,setup,monkeypatch):
    uid,identity,pid,_,headers=setup
    settings=get_settings()
    monkeypatch.setattr(settings,'authz_enabled_resources',[authz.RESOURCE])
    monkeypatch.setattr(settings,'maintenance_boss_dashboard_enabled',True)
    runtime.activate_configured_resources(db.get_bind())
    grant=SysAuthorizationGrant(user_id=uid,resource=authz.RESOURCE,
        actions=['submit','job_read','raw_download','retry','cancel','apply'],scope='all_projects',field_permissions=[],version=1)
    db.add(grant);db.commit()
    project=db.get(business.Project,pid)
    project.project_code=project.display_name='TEST-PROJECT';db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);client.headers.update(headers)
    monkeypatch.setattr(business,'SessionLocal',lambda:Session(db.get_bind()))
    return uid,identity,pid,grant,client


def upload(client,data=None,**kwargs):
    return client.post(authz.BASE,files={'file':('return.xlsx',data or workbook())},headers={'Idempotency-Key':'canonical-return-1'},**kwargs)


def test_actual_http_six_entries_and_worker_plan_binding(db,job):
    uid,identity,pid,grant,client=job
    view=upload(client,params={'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(business.Batch))==0
    data=workbook()
    created=upload(client,data);assert created.status_code==202,created.text
    bid=created.json()['batch_id']
    value=client.get(authz.BASE+'/'+bid);assert value.status_code==200,value.text
    body=value.json();assert body['status']=='ready',body
    assert body['rows_total']==1
    assert db.get(business.Batch,bid,populate_existing=True).owner_user_id==uid
    assert db.get(SysAuthorizationArtifact,(authz.KIND,bid)).authorization_provenance['schema_version']==2
    raw=client.get(authz.BASE+'/'+bid+'/original');assert raw.status_code==200
    assert raw.content==data
    cancelled=client.post(authz.BASE+'/'+bid+'/cancel');assert cancelled.status_code==200,cancelled.text
    assert client.get(authz.BASE+'/'+bid).json()['rows']==[]
    retry=client.post(authz.BASE+'/'+bid+'/retry');assert retry.status_code==202,retry.text
    ready=client.get(authz.BASE+'/'+bid).json();assert ready['status']=='ready',ready
    applied=client.post(authz.BASE+'/'+bid+'/apply',json={k:ready[k] for k in ('plan_hash','preview_token')})
    assert applied.status_code==200,applied.text
    assert applied.json()['applied_lines']==1
    assert db.scalar(select(business.Receipt.owner_user_id))==uid


def test_independent_read_original_actions_and_stable_rename(db,job):
    uid,identity,pid,grant,client=job
    bid=upload(client).json()['batch_id']
    grant.actions=['raw_download'];grant.version+=1;db.commit()
    assert client.get(authz.BASE+'/'+bid).status_code==403
    assert client.post(authz.BASE+'/'+bid+'/cancel').status_code==403
    assert client.get(authz.BASE+'/'+bid+'/original').status_code==200
    user=db.get(SysUser,uid);user.username='renamed-return-owner';db.commit()
    token,_=_make_token(user.role,user.username,None,token_version=user.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token
    assert client.get(authz.BASE+'/'+bid+'/original').status_code==200
    user2=SysUser(username=identity.username,role='admin',password_hash='synthetic');db.add(user2);db.flush()
    db.add(SysAuthorizationGrant(user_id=user2.id,resource=authz.RESOURCE,actions=['raw_download'],scope='all_projects',field_permissions=[],version=1));db.commit()
    token,_=_make_token(user2.role,user2.username,None,token_version=user2.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token
    assert client.get(authz.BASE+'/'+bid+'/original').status_code==403


def test_worker_revoked_after_enqueue_does_not_publish_or_write_failed(db,job,monkeypatch):
    uid,identity,pid,grant,client=job
    original=business.run_job
    monkeypatch.setattr(business,'run_job',lambda *args:None)
    bid=upload(client).json()['batch_id']
    user=db.get(SysUser,uid);user.token_version+=1;db.commit()
    original(bid,1)
    row=db.get(business.Batch,bid,populate_existing=True)
    assert row.report_json['status']=='queued'
    assert db.scalar(select(func.count()).select_from(business.Head))==0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact).where(SysAuthorizationArtifact.domain_kind==authz.PLAN_KIND))==0


def test_stored_preview_tampering_refuses_read_and_apply(db,job):
    uid,identity,pid,grant,client=job
    bid=upload(client).json()['batch_id'];row=db.get(business.Batch,bid,populate_existing=True)
    report=dict(row.report_json);assert report['status']=='ready'
    report['summary']={**report['summary'],'create':999};row.report_json=report;db.commit()
    assert client.get(authz.BASE+'/'+bid).status_code==403
    response=client.post(authz.BASE+'/'+bid+'/apply',json={k:report[k] for k in ('plan_hash','preview_token')})
    assert response.status_code==403,response.text
    assert db.scalar(select(func.count()).select_from(business.Receipt))==0


def test_final_apply_revocation_rolls_back_rows_status_and_audit(db,job,monkeypatch):
    uid,identity,pid,grant,client=job
    bid=upload(client).json()['batch_id'];ready=client.get(authz.BASE+'/'+bid).json()
    original=authz.ReturnImportAuthorization.finish_apply
    def revoke(self,session):
        assert session.scalar(select(func.count()).select_from(business.Receipt))==1
        with Session(db.get_bind()) as other:
            user=other.get(SysUser,uid);user.token_version+=1;other.commit()
        return original(self,session)
    monkeypatch.setattr(authz.ReturnImportAuthorization,'finish_apply',revoke)
    response=client.post(authz.BASE+'/'+bid+'/apply',json={k:ready[k] for k in ('plan_hash','preview_token')})
    assert response.status_code==401,response.text
    assert db.scalar(select(func.count()).select_from(business.Receipt))==0
    assert db.get(business.Batch,bid,populate_existing=True).report_json['status']=='ready'


def test_complete_original_includes_excluded_category_foreign_project(db,job):
    from uuid import uuid4
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from tests.test_site_issue_v2_api import _project
    uid,identity,pid,grant,client=job
    foreign=_project(db,project_id='return-import-foreign')
    foreign.project_code=foreign.display_name='FOREIGN'
    db.add(MaintenanceProjectUserAssignment(assignment_id=str(uuid4()),project_id=pid,user_id=uid,
        responsibility_type='primary_manager',assigned_by='synthetic',assignment_reason='synthetic'))
    db.commit()
    data=workbook(extra=[{'数据ID(不可修改)':'HEAD-FOREIGN','入库单号':'RKD-FOREIGN',
        '入库类别':'普通采购入库','数据状态':'已生效','项目名称':'FOREIGN','备件明细.备件PN':'FOREIGN-SECRET',
        '备件明细.入库数量':'1','备件明细.数据ID(不可修改)':'DETAIL-FOREIGN'}])
    created=upload(client,data);assert created.status_code==202,created.text
    bid=created.json()['batch_id']
    grant.scope='assigned_projects';grant.version+=1;db.commit()
    assert client.get(authz.BASE+'/'+bid).status_code==403
    assert client.get(authz.BASE+'/'+bid+'/original').status_code==403
    assert upload(client,data).status_code==403


def test_retry_only_creates_new_task_provenance_without_borrowing_submit(db,job):
    uid,identity,pid,grant,client=job
    bid=upload(client).json()['batch_id']
    assert client.post(authz.BASE+'/'+bid+'/cancel').status_code==200
    grant.actions=['retry','job_read'];grant.version+=1;db.commit()
    result=client.post(authz.BASE+'/'+bid+'/retry');assert result.status_code==202,result.text
    body=client.get(authz.BASE+'/'+bid).json();assert body['status']=='ready',body
    binding=db.get(SysAuthorizationArtifact,(authz.KIND,bid),populate_existing=True)
    assert {row['entry_id'] for row in binding.authorization_provenance['resources']}=={authz.RETRY}
    assert upload(client).status_code==403


def test_worker_revocation_after_staging_rolls_back_raw_and_plan(db,job,monkeypatch):
    uid,identity,pid,grant,client=job
    original_job=business.run_job
    monkeypatch.setattr(business,'run_job',lambda *args:None)
    bid=upload(client).json()['batch_id']
    original_save=business._save_raw
    def save(session,batch,parsed):
        original_save(session,batch,parsed)
        assert session.scalar(select(func.count()).select_from(business.Head))==1
        with Session(db.get_bind()) as other:
            actor=other.get(SysUser,uid);actor.token_version+=1;other.commit()
    monkeypatch.setattr(business,'_save_raw',save)
    original_job(bid,1)
    assert db.get(business.Batch,bid,populate_existing=True).report_json['status']=='queued'
    for model in (business.Head,business.Line,business.Receipt):
        assert db.scalar(select(func.count()).select_from(model))==0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact).where(SysAuthorizationArtifact.domain_kind==authz.PLAN_KIND))==0


def test_unknown_legacy_task_never_reenters_worker_or_download_after_cutover(db,job):
    uid,identity,pid,grant,client=job
    # Historical NULL-owner business rows remain loadable for audit, but no
    # current admin/username may adopt their bytes or resume their worker.
    batch,_=business.enqueue(db,workbook(),'old.xlsx',identity.username,'historical-return-key')
    bid=batch.batch_id
    business.run_job(bid,1)
    assert db.get(business.Batch,bid,populate_existing=True).report_json['status']=='queued'
    assert client.get(authz.BASE+'/'+bid+'/original').status_code==403
    assert client.get(authz.BASE+'/'+bid).status_code==403
    assert client.post(authz.BASE+'/'+bid+'/retry').status_code==403
