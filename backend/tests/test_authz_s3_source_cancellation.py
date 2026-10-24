"""Actual cancellation state, stable replay identity and final transaction checks."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.api import maintenance_source_cancel_cascade as api
from app.auth import _make_token
from app.authz import runtime
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact,SysAuthorizationGrant
from app.models.system import SysUser,SysImportBatch
from app.models.maintenance import MaintenanceDemandTombstone,FMaintenanceLine
from app.services import maintenance_source_cancellation_access as authorization
from app.services import maintenance_source_cancel_cascade as business
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_maintenance_source_cancel_cascade import setup as legacy_setup


@pytest.fixture
def cancellation(db,tmp_path,monkeypatch,cerbos_url):
    _,project,_,parent,lines,exact,_,_,batch,url,detail=legacy_setup(db,tmp_path)
    settings=get_settings()
    for key,value in {'authorization_mode':'unified_modules','authz_enabled_resources':['maintenance_source_cancellation'],
        'authz_suspended_resources':[],'authz_sales_analysis_enabled':False,'authz_sales_analysis_suspended':False,
        'cerbos_url':cerbos_url,'cerbos_timeout_seconds':2.0}.items():monkeypatch.setattr(settings,key,value)
    runtime.activate_configured_resources(db.get_bind())
    user=db.scalar(select(SysUser).where(SysUser.username=='cancel-admin'))
    user.role='readonly'
    grant=SysAuthorizationGrant(user_id=user.id,resource='maintenance_source_cancellation',actions=['read'],
        scope='all_projects',field_permissions=[],version=1)
    db.add(grant);db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app)
    login(client,user)
    return user.id,grant,client,batch.id,url,detail,[line.id for line in lines]


def login(client,user):
    token,_=_make_token(user.role,user.username,None,token_version=user.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token


def body(client,detail):
    response=client.get(detail);assert response.status_code==200,response.text
    return {'expected_digest':response.json()['expected_digest'],'reason':'核对实际来源取消','idempotency_key':'canonical-cancel-one'}


def test_exact_read_confirm_and_stable_actor_rename_receipt(db,cancellation):
    uid,grant,client,bid,url,detail,lines=cancellation
    assert client.get(url).status_code==200
    payload=body(client,detail)
    assert client.post(detail+'/confirm',json=payload).status_code==403
    grant.actions=['read','confirm'];grant.version+=1;db.commit()
    view=client.post(detail+'/confirm?view=authorization',json=payload)
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(MaintenanceDemandTombstone))==0
    result=client.post(detail+'/confirm',json=payload)
    assert result.status_code==200,result.text
    assert result.json()['voided_lines']==2
    assert client.get(detail+'/confirmation-status',params={'idempotency_key':payload['idempotency_key']}).status_code==403
    user=db.get(SysUser,uid);user.username='renamed-cancellation-actor'
    grant.actions=['confirm','read_confirmation'];grant.version+=1;db.commit();login(client,user)
    replay=client.post(detail+'/confirm',json=payload)
    assert replay.status_code==200,replay.text
    assert replay.json()['replayed'] is True
    status=client.get(detail+'/confirmation-status',params={'idempotency_key':payload['idempotency_key']})
    assert status.status_code==200,status.text
    assert status.json()['result']['voided_lines']==2
    binding=db.scalar(select(SysAuthorizationArtifact).where(SysAuthorizationArtifact.domain_kind==authorization.KIND))
    assert binding.owner_user_id==uid
    grant.actions=['read_confirmation'];grant.version+=1;db.commit()
    assert client.post(detail+'/confirm',json=payload).status_code==403


def test_final_epoch_revocation_rolls_back_tombstones_lines_receipt_and_sidecar(db,cancellation,monkeypatch):
    uid,grant,client,bid,url,detail,lines=cancellation
    payload=body(client,detail)
    grant.actions=['read','confirm'];grant.version+=1;db.commit()
    original=authorization.SourceCancellationAuthorization.finish
    def finish(self,session,*,replayed):
        assert session.scalar(select(func.count()).select_from(MaintenanceDemandTombstone))==1
        with Session(db.get_bind()) as other:
            user=other.get(SysUser,uid);user.token_version+=1;other.commit()
        return original(self,session,replayed=replayed)
    monkeypatch.setattr(authorization.SourceCancellationAuthorization,'finish',finish)
    result=client.post(detail+'/confirm',json=payload)
    assert result.status_code==401,result.text
    assert db.scalar(select(func.count()).select_from(MaintenanceDemandTombstone))==0
    assert all(db.get(FMaintenanceLine,lid,populate_existing=True).is_active for lid in lines)
    assert not (db.get(SysImportBatch,bid,populate_existing=True).report_json or {}).get(business.RESULTS_KEY)
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact))==0


def test_changed_persisted_confirmation_json_never_replays_under_same_input_hash(db,cancellation):
    uid,grant,client,bid,url,detail,lines=cancellation
    payload=body(client,detail)
    grant.actions=['read','confirm','read_confirmation'];grant.version+=1;db.commit()
    result=client.post(detail+'/confirm',json=payload);assert result.status_code==200,result.text
    from copy import deepcopy
    batch=db.get(SysImportBatch,bid,populate_existing=True)
    report=deepcopy(batch.report_json)
    record=next(iter(report[business.RESULTS_KEY].values()))
    record['result']['voided_lines']=999
    batch.report_json=report;db.commit()
    assert client.get(detail+'/confirmation-status',params={'idempotency_key':payload['idempotency_key']}).status_code==403
    assert client.post(detail+'/confirm',json=payload).status_code==403
