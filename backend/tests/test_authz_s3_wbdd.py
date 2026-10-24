"""WBDD actual bytes, stable producers and same-transaction revocation."""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.api import maintenance_wbdd_import as api
from app.auth import _make_token
from app.authz import runtime
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact,SysAuthorizationGrant
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
from app.models.system import SysImportBatch,SysUser
from app.services import maintenance_wbdd_authorization as authorization
from tests.test_authz_s3_checklist import setup,cerbos_url  # noqa: F401
from tests.wbdd_fixtures import COLUMNS_91,make_rows,write_workbook


@pytest.fixture
def wbdd(db,setup,tmp_path,monkeypatch):
    uid,_,_,_,headers=setup
    monkeypatch.setattr(get_settings(),'authz_enabled_resources',['maintenance_wbdd_snapshot','maintenance_wbdd_reconciliation'])
    monkeypatch.setattr(get_settings(),'maintenance_boss_dashboard_enabled',True)
    runtime.activate_configured_resources(db.get_bind())
    grant=SysAuthorizationGrant(user_id=uid,resource='maintenance_wbdd_snapshot',actions=['submit'],scope='all',field_permissions=[],version=1)
    db.add(grant);db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);client.headers.update(headers)
    path=write_workbook(str(tmp_path/'synthetic.xlsx'),COLUMNS_91,make_rows(orders=1,lines_per_order=1))
    return uid,grant,client,Path(path).read_bytes()


def post(client,data,params=None):
    return client.post(authorization.BASE,files={'file':('synthetic.xlsx',data)},
        headers={'Idempotency-Key':'canonical-wbdd-one'},params=params)


def test_actual_bytes_view_zero_write_and_renamed_stable_owner_replay(db,wbdd):
    uid,grant,client,data=wbdd
    view=post(client,data,{'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0
    ctx=view.json()['authorization_context'];pre={k:ctx[k] for k in ('catalog_version','authorization_epoch','context')}
    created=post(client,data,{'authorization_precondition':json.dumps(pre)})
    assert created.status_code==200,created.text
    bid=created.json()['batch_id'];receipt=db.scalar(select(MaintenanceWbddImportReceipt))
    assert receipt.owner_user_id==uid
    assert db.get(SysImportBatch,bid).owner_user_id==uid
    assert db.get(SysAuthorizationArtifact,(authorization.KIND,str(bid))).owner_user_id==uid
    assert client.get(authorization.BASE+'/latest').status_code==403
    actor=db.get(SysUser,uid);actor.username='renamed-wbdd-producer';db.commit()
    token,_=_make_token(actor.role,actor.username,None,token_version=actor.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token
    replay=post(client,data);assert replay.status_code==200,replay.text
    assert replay.json()['replayed'] and replay.json()['batch_id']==bid
    grant.actions=['read_latest'];grant.version+=1;db.commit()
    assert client.get(authorization.BASE+'/latest').status_code==200
    assert post(client,data).status_code==403


def test_immutable_full_upload_report_mismatch_refuses_replay(db,wbdd):
    uid,grant,client,data=wbdd
    created=post(client,data);assert created.status_code==200,created.text
    row=db.scalar(select(MaintenanceWbddImportReceipt));row.report_json={**row.report_json,'orders_inserted':999};db.commit()
    assert post(client,data).status_code==403
    assert db.scalar(select(func.count()).select_from(MaintenanceWbddImportReceipt))==1


def test_revoked_at_final_upload_check_rolls_back_facts_batch_receipt_and_binding(db,wbdd,monkeypatch):
    uid,grant,client,data=wbdd
    original=authorization.WbddUploadAuthorization.finish
    def finish(self,session,receipt,*,replayed):
        assert session.scalar(select(func.count()).select_from(FMaintenanceOrder))==1
        with Session(db.get_bind()) as other:
            actor=other.get(SysUser,uid);actor.token_version+=1;other.commit()
        return original(self,session,receipt,replayed=replayed)
    monkeypatch.setattr(authorization.WbddUploadAuthorization,'finish',finish)
    result=post(client,data);assert result.status_code==401,result.text
    for Model in (SysImportBatch,FMaintenanceOrder,MaintenanceWbddImportReceipt,SysAuthorizationArtifact):
        assert db.scalar(select(func.count()).select_from(Model))==0


@pytest.fixture
def differences(db,tmp_path,monkeypatch,cerbos_url):
    from tests.test_maintenance_wbdd_sync_api import _setup
    from app.models.maintenance import FMaintenanceLine
    old,missing,line_ids,site_id=_setup(db,tmp_path)
    actor=db.scalar(select(SysUser).where(SysUser.username=='sync-http-admin'))
    if actor is None:
        actor=db.scalar(select(SysUser).order_by(SysUser.id))
    actor.role='readonly'
    settings=get_settings()
    for k,v in {'authorization_mode':'unified_modules','authz_enabled_resources':['maintenance_wbdd_reconciliation'],
        'authz_suspended_resources':[],'authz_sales_analysis_enabled':False,'authz_sales_analysis_suspended':False,
        'cerbos_url':cerbos_url,'cerbos_timeout_seconds':2.0,'maintenance_boss_dashboard_enabled':True}.items():monkeypatch.setattr(settings,k,v)
    runtime.activate_configured_resources(db.get_bind())
    grant=SysAuthorizationGrant(user_id=actor.id,resource='maintenance_wbdd_reconciliation',actions=['read_missing','reconcile'],
        scope='all_projects',field_permissions=[],version=1)
    db.add(grant);db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app)
    token,_=_make_token(actor.role,actor.username,None,token_version=actor.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token
    return actor.id,grant,client,missing,line_ids,site_id


@pytest.mark.parametrize('revoke',[False,True])
def test_reconcile_actual_selection_independent_action_and_atomic_final_check(db,differences,monkeypatch,revoke):
    from app.models.maintenance import FMaintenanceLine
    from tests.test_maintenance_wbdd_sync_api import _body
    uid,grant,client,_,line_ids,site_id=differences
    missing=client.get(authorization.BASE+'/latest/missing');assert missing.status_code==200,missing.text
    value=missing.json();assert value['missing_line_count']==1
    payload=_body(value);path=authorization.BASE+f"/{value['batch_id']}/reconcile"
    grant.actions=['reconcile'];grant.version+=1;db.commit()
    assert client.get(authorization.BASE+'/latest/missing').status_code==403
    view=client.post(path+'?view=authorization',json=payload);assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    original=authorization.WbddReconciliationAuthorization.finish
    def finish(self,session,result):
        assert not session.get(FMaintenanceLine,line_ids[1],populate_existing=True).is_active
        if revoke:
            with Session(db.get_bind()) as other:
                actor=other.get(SysUser,uid);actor.token_version+=1;other.commit()
        return original(self,session,result)
    monkeypatch.setattr(authorization.WbddReconciliationAuthorization,'finish',finish)
    response=client.post(path,json=payload)
    assert response.status_code==(401 if revoke else 200),response.text
    assert db.get(FMaintenanceLine,line_ids[1],populate_existing=True).is_active is revoke
    saved=db.get(SysImportBatch,value['batch_id'],populate_existing=True).report_json.get('_wbdd_sync_results')
    assert bool(saved) is (not revoke)
    if not revoke:
        monkeypatch.setattr(authorization.WbddReconciliationAuthorization,'finish',original)
        replay=client.post(path,json=payload);assert replay.status_code==200,replay.text
        assert replay.json()['replayed']


def test_missing_scope_is_applied_before_candidate_digest_and_actual_target_auth(db,differences):
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from tests.test_maintenance_wbdd_sync_api import _body
    uid,grant,client,old,line_ids,site_id=differences
    grant.scope='assigned_or_sales_projects';grant.version+=1;db.commit()
    missing=client.get(authorization.BASE+'/latest/missing');assert missing.status_code==200,missing.text
    assert missing.json()['missing_line_count']==0
    denied=client.post(authorization.BASE+f"/{old['batch_id']}/reconcile",json=_body(old))
    assert denied.status_code==403,denied.text
    parent=db.scalar(select(MaintenanceSourceOrderAssignment).where(MaintenanceSourceOrderAssignment.is_active.is_(True)))
    db.add(MaintenanceProjectUserAssignment(assignment_id='wbdd-authz-manager',project_id=parent.project_id,
        user_id=uid,responsibility_type='primary_manager',assigned_by='synthetic',assignment_reason='test current scope'))
    db.commit()
    actual=client.get(authorization.BASE+'/latest/missing');assert actual.status_code==200,actual.text
    assert actual.json()['missing_line_count']==1
    result=client.post(authorization.BASE+f"/{old['batch_id']}/reconcile",json=_body(actual.json()))
    assert result.status_code==200,result.text


def test_unassigned_real_source_requires_all_scope_even_after_receipt_exists(db,differences):
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from tests.test_maintenance_wbdd_sync_api import _body
    uid,grant,client,old,line_ids,site_id=differences
    assignment=db.scalar(select(MaintenanceSourceOrderAssignment).where(MaintenanceSourceOrderAssignment.is_active.is_(True)))
    from datetime import datetime,timezone
    assignment.is_active=False;assignment.archived_at=datetime.now(timezone.utc)
    assignment.archived_by='synthetic';assignment.version+=1;db.commit()
    missing=client.get(authorization.BASE+'/latest/missing');assert missing.status_code==200,missing.text
    assert missing.json()['missing_line_count']==1
    payload=_body(missing.json());path=authorization.BASE+f"/{old['batch_id']}/reconcile"
    grant.scope='assigned_or_sales_projects';grant.version+=1;db.commit()
    assert client.post(path,json=payload).status_code==403
    grant.scope='all_projects';grant.version+=1;db.commit()
    created=client.post(path,json=payload);assert created.status_code==200,created.text
    assert client.post(path,json=payload).status_code==200
    grant.scope='assigned_or_sales_projects';grant.version+=1;db.commit()
    assert client.post(path,json=payload).status_code==403


def test_reconcile_authorization_view_never_materializes_report_or_saved_result(db,differences):
    from sqlalchemy import event
    from tests.test_maintenance_wbdd_sync_api import _body
    _,_,client,_,_,_=differences
    missing=client.get(authorization.BASE+'/latest/missing')
    assert missing.status_code==200,missing.text
    payload=_body(missing.json())
    path=authorization.BASE+f"/{missing.json()['batch_id']}/reconcile"
    created=client.post(path,json=payload)
    assert created.status_code==200,created.text
    selected=[]
    def capture(_conn,cursor,statement,_params,_context,_many):
        if statement.lstrip().lower().startswith('select') and 'sys_import_batch' in statement.lower():
            selected.extend(column[0] for column in cursor.description or ())
    event.listen(db.get_bind(),'after_cursor_execute',capture)
    try:
        view=client.post(path,params={'view':'authorization'},json=payload)
    finally:
        event.remove(db.get_bind(),'after_cursor_execute',capture)
    assert view.status_code==200,view.text
    assert selected, 'expected actual snapshot SQL observation'
    assert not any(name.endswith('report_json') for name in selected),selected
    row=db.get(SysImportBatch,missing.json()['batch_id'],populate_existing=True)
    report=json.loads(json.dumps(row.report_json))
    receipt=next(iter(report['_wbdd_sync_results'].values()))
    receipt['result']['tampered_opaque_result']='must invalidate the whole receipt'
    row.report_json=report;db.commit()
    denied=client.post(path,params={'view':'authorization'},json=payload)
    assert denied.status_code==403,denied.text
