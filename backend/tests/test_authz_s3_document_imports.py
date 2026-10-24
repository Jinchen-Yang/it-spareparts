"""Real staged-input provenance, stable ownership and exact import actions."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import maintenance_doc_import as doc_api, maintenance_ckd_import as ckd_api
from app.authz import runtime
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.maintenance_doc_import import MaintenanceDocImportBatch, MaintenanceDocHeadRow
from app.models.maintenance_ckd_import import MaintenanceCkdImportBatch, MaintenanceCkdHeadRow
from app.services import maintenance_import_authorization as authorization
from tests.test_authz_s3_checklist import setup, cerbos_url  # noqa: F401
from tests.test_maintenance_ckd_import import _ckd_workbook_bytes, _HEAD_SALES
from tests.test_maintenance_doc_import import _doc_workbook, _BXD_HEAD, _BXD_LINE


@pytest.fixture(params=['document', 'shipment'])
def imports(db, setup, monkeypatch, request):
    uid, identity, pid, _, headers = setup
    is_doc = request.param == 'document'
    resource = 'maintenance_document_import' if is_doc else 'maintenance_shipment_import'
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [resource])
    runtime.activate_configured_resources(db.get_bind())
    grant = SysAuthorizationGrant(user_id=uid, resource=resource, actions=['preview'],
        scope='own_imports', field_permissions=['data_purchase_cost'], version=1)
    db.add(grant); db.commit()
    app = FastAPI();app.include_router((doc_api if is_doc else ckd_api).router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app);client.headers.update(headers)
    if is_doc:
        data = _doc_workbook(sheet_title='Sheet1', head_headers=_BXD_HEAD, line_headers=_BXD_LINE,
            rows=[{'head': ['BXD-20261001-0001', '合成', '交通', '合成用途', '', '', '', '', '12.00'], 'lines': []}])
        return uid, grant, client, data, '/api/maintenance/doc-imports/bxd_expense/preview', '/api/maintenance/doc-imports/', MaintenanceDocImportBatch, MaintenanceDocHeadRow, 'document_import_batch'
    data = _ckd_workbook_bytes(rows=[{'head': _HEAD_SALES, 'lines': []}])
    return uid, grant, client, data, '/api/maintenance/ckd-imports/preview', '/api/maintenance/ckd-imports/', MaintenanceCkdImportBatch, MaintenanceCkdHeadRow, 'shipment_import_batch'


def test_input_view_zero_staging_stable_owner_and_independent_read(db, imports):
    uid, grant, client, data, preview, read, Model, Head, kind = imports
    kwargs = dict(headers={'Idempotency-Key': 'canonical-staging-one'}, files={'file': ('synthetic.xlsx', data)})
    view = client.post(preview + '?view=authorization', **kwargs)
    assert view.status_code == 200, view.text
    assert set(view.json()) == {'locator', 'authorization_context'}
    assert view.json()['locator']['object_type'] == 'operation_input'
    assert db.scalar(select(func.count()).select_from(Model)) == 0
    ctx = view.json()['authorization_context']
    pre = {k: ctx[k] for k in ('catalog_version', 'authorization_epoch', 'context')}
    response = client.post(preview, params={'authorization_precondition': json.dumps(pre)}, **kwargs)
    assert response.status_code == 200, response.text
    bid = response.json()['batch_id'];row=db.get(Model,bid)
    assert row.owner_user_id == uid
    assert db.get(SysAuthorizationArtifact, (kind, bid)).owner_user_id == uid
    assert client.get(read+bid).status_code == 403
    grant.actions=['read'];grant.version+=1;db.commit()
    assert client.get(read+bid).status_code == 200
    assert client.post(preview, **kwargs).status_code == 403
    # Same file digest alone cannot bless changed normalized stored input.
    head=db.scalar(select(Head).where(Head.batch_id==bid))
    if kind=='document_import_batch':head.head_no='CHANGED-HEAD'
    else:head.order_no='CHANGED-HEAD'
    db.commit()
    assert client.get(read+bid).status_code == 403


def test_no_targets_requires_both_explicit_actions_and_all_project_admission(db, imports, monkeypatch):
    uid, grant, client, data, preview, read, Model, Head, kind = imports
    created = client.post(preview, headers={'Idempotency-Key': 'empty-target-preview'},
                          files={'file': ('synthetic.xlsx', data)})
    assert created.status_code == 200, created.text
    bid = created.json()['batch_id'];path=read+bid+'/apply'
    resource = grant.resource
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [resource, resource+'_targets'])
    runtime.activate_configured_resources(db.get_bind())
    grant.actions=['apply', 'read'];grant.version+=1;db.commit()
    assert client.post(path).status_code == 403
    target = SysAuthorizationGrant(user_id=uid, resource=resource+'_targets', actions=['apply'],
        scope='assigned_projects', field_permissions=['data_purchase_cost'], version=1)
    db.add(target);db.commit()
    assert client.post(path).status_code == 403
    target.scope='all_projects';target.version+=1;db.commit()
    view = client.post(path+'?view=authorization')
    assert view.status_code == 200, view.text
    assert set(view.json()) == {'locator','authorization_context'}
    ctx = view.json()['authorization_context']
    pre = {k:ctx[k] for k in ('catalog_version','authorization_epoch','context')}
    result=client.post(path,json={'authorization_precondition':pre})
    assert result.status_code == 200, result.text
    assert db.get(Model,bid,populate_existing=True).status == 'applied'
    assert client.get(read+bid).status_code == 200

from tests.test_maintenance_ckd_import import wbdd_project, _HEAD_MAINT  # noqa: F401,E402
from tests.test_maintenance_doc_import import _RETURN_HEAD, _RETURN_LINE  # noqa: E402


@pytest.mark.parametrize('revoke', [False, True])
def test_apply_checks_actual_project_before_mutation_and_final_actor_after_domain_work(db, imports, wbdd_project, monkeypatch, revoke):
    from sqlalchemy.orm import Session
    from app.models.system import SysUser
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from app.models.maintenance_front_stock import MaintenanceFrontStockLedger
    uid, grant, client, _, preview, read, Model, Head, kind = imports
    if kind == 'document_import_batch':
        preview = preview.replace('bxd_expense', 'return_order')
        data = _doc_workbook(sheet_title='Sheet1', head_headers=_RETURN_HEAD, line_headers=_RETURN_LINE,
            rows=[{'head':['维保返件','其他退回','备件','2026-08-03','合成客户','CKD测试项目','','WBDD-20260702-0014','','广州仓','已生效','','RKN-002','HEAD-2'],
                   'lines':[['','02311AYV','','坏品','','','5','LID-1','1','']]}])
    else:
        data = _ckd_workbook_bytes(rows=[{'head':_HEAD_MAINT,
            'lines':[['LID-1','1','02311AYV','内存','B1','02311AYV','SN1','','北京成品仓','BJCP-AA','内存','','品牌','个','2','100','200','是']]}])
    created=client.post(preview, headers={'Idempotency-Key':'actual-project-staging'}, files={'file':('actual.xlsx',data)})
    assert created.status_code==200,created.text
    bid=created.json()['batch_id'];path=read+bid+'/apply'
    monkeypatch.setattr(get_settings(),'authz_enabled_resources',[grant.resource,grant.resource+'_targets'])
    runtime.activate_configured_resources(db.get_bind())
    grant.actions=['apply'];grant.version+=1
    target=SysAuthorizationGrant(user_id=uid,resource=grant.resource+'_targets',actions=['apply'],scope='assigned_projects',field_permissions=['data_purchase_cost'],version=1)
    db.add(target);db.commit()
    assert client.post(path).status_code==403
    assert db.get(Model,bid,populate_existing=True).status=='pending'
    db.add(MaintenanceProjectUserAssignment(project_id=wbdd_project['project_id'],user_id=uid,
        assignment_id='canonical-import-manager', responsibility_type='primary_manager',
        assigned_by='independent-fixture',assignment_reason='canonical import test'))
    db.commit()
    view=client.post(path+'?view=authorization');assert view.status_code==200,view.text
    ctx=view.json()['authorization_context'];pre={k:ctx[k] for k in ('catalog_version','authorization_epoch','context')}
    original=authorization.ImportApplyAuthorization.finish
    called=[]
    def finish(self, session):
        called.append(True)
        assert session.get(Model,bid).status=='applied'
        if revoke:
            with Session(db.get_bind()) as other:
                actor=other.get(SysUser,uid);actor.token_version+=1;other.commit()
        return original(self,session)
    monkeypatch.setattr(authorization.ImportApplyAuthorization,'finish',finish)
    result=client.post(path,json={'authorization_precondition':pre})
    assert result.status_code==(401 if revoke else 200),result.text
    assert called
    assert db.get(Model,bid,populate_existing=True).status==('pending' if revoke else 'applied')
    if revoke:
        assert db.scalar(select(func.count()).select_from(MaintenanceFrontStockLedger))==0


def test_relink_empty_requires_explicit_action_and_complete_plan_binds_current_heads(db, setup, monkeypatch):
    uid, identity, pid, _, headers=setup
    monkeypatch.setattr(get_settings(),'maintenance_boss_dashboard_enabled',True)
    monkeypatch.setattr(get_settings(),'authz_enabled_resources',['maintenance_document_relink'])
    runtime.activate_configured_resources(db.get_bind())
    app=FastAPI();app.include_router(doc_api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);client.headers.update(headers)
    url='/api/maintenance/doc-imports/relink-projects'
    assert client.post(url).status_code==403
    grant=SysAuthorizationGrant(user_id=uid,resource='maintenance_document_relink',actions=['relink'],
        scope='all_projects',field_permissions=['data_purchase_cost'],version=1)
    db.add(grant);db.commit()
    view=client.post(url+'?view=authorization');assert view.status_code==200,view.text
    ctx=view.json()['authorization_context'];pre={k:ctx[k] for k in ('catalog_version','authorization_epoch','context')}
    result=client.post(url,json={'authorization_precondition':pre})
    assert result.status_code==200,result.text
    assert result.json()['relinked']==0
    assert client.post(url,json={'project_ids':[pid]}).status_code==422


def test_relink_actual_stored_head_old_view_rejected_then_current_project_applied(db, setup, monkeypatch):
    from datetime import datetime, timezone
    from uuid import uuid4
    from app.models.maintenance_project import MaintenanceProject
    uid, identity, pid, _, headers=setup
    monkeypatch.setattr(get_settings(),'maintenance_boss_dashboard_enabled',True)
    monkeypatch.setattr(get_settings(),'authz_enabled_resources',['maintenance_document_relink'])
    runtime.activate_configured_resources(db.get_bind())
    grant=SysAuthorizationGrant(user_id=uid,resource='maintenance_document_relink',actions=['relink'],
        scope='all_projects',field_permissions=['data_purchase_cost'],version=1)
    db.add(grant);db.commit()
    app=FastAPI();app.include_router(doc_api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);client.headers.update(headers)
    url='/api/maintenance/doc-imports/relink-projects'
    empty=client.post(url+'?view=authorization');assert empty.status_code==200,empty.text
    ctx=empty.json()['authorization_context'];pre={k:ctx[k] for k in ('catalog_version','authorization_epoch','context')}
    bid=str(uuid4())
    db.add(MaintenanceDocImportBatch(batch_id=bid,doc_type='rkd_inbound',file_hash='e'*64,
        filename='legacy.xlsx',idempotency_key='relink-historical-input',uploaded_by='legacy-audit',
        status='applied',applied_at=datetime.now(timezone.utc),applied_by='legacy-audit'))
    db.flush()
    hid=str(uuid4())
    db.add(MaintenanceDocHeadRow(row_id=hid,batch_id=bid,row_no=1,raw_json={},
        head_no='RKD-20261001-0001',project_name=db.get(MaintenanceProject,pid).project_code))
    db.commit()
    stale=client.post(url,json={'authorization_precondition':pre})
    assert stale.status_code==409,stale.text
    assert db.get(MaintenanceDocHeadRow,hid,populate_existing=True).project_id is None
    grant.scope='assigned_projects';grant.version+=1;db.commit()
    denied=client.post(url);assert denied.status_code==403,denied.text
    grant.scope='all_projects';grant.version+=1;db.commit()
    result=client.post(url);assert result.status_code==200,result.text
    assert result.json()['relinked']==1
    assert db.get(MaintenanceDocHeadRow,hid,populate_existing=True).project_id==pid
