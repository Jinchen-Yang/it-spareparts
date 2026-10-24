"""Actual transfer workbooks, immutable plans, current scope and atomic writes."""
from pathlib import Path
from types import MappingProxyType
import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from app.api import maintenance_project_batch_transfer as api
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.system import SysImportBatch, SysUser
from app.models.maintenance_project import MaintenanceProject
from app.services import maintenance_bulk_import as business
from app.services import maintenance_batch_transfer_authorization as protected
from tests.test_maintenance_bulk_import import _ordinary_sales_workbook
from tests.test_authz_s3_source_cost import give
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401

@pytest.fixture
def transfer(db,canonical,projects,tmp_path,monkeypatch):
    _,headers,ids=canonical; actor,_,_=projects
    monkeypatch.setattr(get_settings(),'maintenance_boss_dashboard_enabled',True)
    grant=give(db,actor,['preview','apply','receipt_ruling'],resource=protected.RESOURCE,
        scope='all_projects',fields=['data_profit'])
    path=Path(_ordinary_sales_workbook(tmp_path,order_no='XSDD-S3-TRANSFER',raw_order_id='S3-TRANSFER-NEW',
        project_name='S3 transfer new project'))
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    identity=VerifiedIdentity(actor.username,actor.token_version,False,'sys_user',MappingProxyType({}),actor.id)
    return TestClient(app),headers,actor,grant,path,identity,ids


def preview(t,**kwargs):
    client,headers,_,_,path,_,_=t
    return client.post('/api/maintenance/project-batch-transfer/preview',headers=headers,
        files=[('files',(path.name,path.read_bytes(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))],**kwargs)


def body(response):
    data=response.json()
    return {**{key:data[key] for key in ('preview_token','payload_hash','data_version')},
        'row_keys':[row['row_key'] for row in data['rows'] if row['row_status']=='ready']}


def apply(t,payload,**kwargs):
    return t[0].post('/api/maintenance/project-batch-transfer/apply',headers=t[1],json=payload,**kwargs)


def test_real_preview_apply_replay_with_f_only_and_stable_receipt(db,transfer):
    view=preview(transfer,params={'view':'authorization'});assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0
    response=preview(transfer);assert response.status_code==200,response.text
    payload=body(response); assert payload['row_keys']
    row=db.get(SysImportBatch,response.json()['preview_id']);assert row.owner_user_id==transfer[2].id
    grant=transfer[3];grant.actions=['apply'];grant.version+=1;db.commit()
    applied=apply(transfer,payload);assert applied.status_code==200,applied.text
    assert applied.json()['applied']==len(payload['row_keys'])
    receipt=db.get(SysAuthorizationArtifact,(protected.RECEIPT,str(row.id)));assert receipt is not None
    replay=apply(transfer,payload);assert replay.status_code==200,replay.text
    assert replay.json()==applied.json()
    grant.actions=[];grant.version+=1;db.commit()
    assert apply(transfer,payload).status_code==403


def test_new_project_input_not_vacuously_authorized_by_assigned_scope(db,transfer):
    grant=transfer[3];grant.scope='assigned_projects';grant.version+=1;db.commit()
    response=preview(transfer);assert response.status_code==403,response.text
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0


def test_own_field_contract_cannot_borrow_profit(db,transfer):
    grant=transfer[3];grant.field_permissions=[];grant.version+=1;db.commit()
    db.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==transfer[2].id,SysAuthorizationGrant.resource=='maintenance_project').values(field_permissions=['data_profit','data_purchase_cost']));db.commit()
    assert preview(transfer).status_code==403


def test_precondition_and_actual_input_archive(db,transfer):
    response=preview(transfer);assert response.status_code==200,response.text
    payload=body(response)
    view=apply(transfer,payload,params={'view':'authorization'});assert view.status_code==200,view.text
    context=view.json()['authorization_context'];payload['authorization_precondition']={k:context[k] for k in ('catalog_version','authorization_epoch','context')}
    payload['authorization_precondition']['context']['authorization_revision']='0'*64
    stale=apply(transfer,payload); assert stale.status_code==409,stale.text
    payload.pop('authorization_precondition')
    row=db.get(SysImportBatch,response.json()['preview_id'])
    Path(row.report_json['archives'][0]['storage_path']).write_bytes(b'changed actual source')
    assert apply(transfer,payload).status_code==403


def test_real_preview_publication_failure_rolls_back_and_cleans_owned_file(db,transfer,monkeypatch):
    directory=Path(get_settings().raw_file_dir);directory.mkdir(parents=True,exist_ok=True)
    prior=set(directory.iterdir()); original=protected.artifacts.register_artifact_binding
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('published then failed')
    monkeypatch.setattr(protected.artifacts,'register_artifact_binding',fail)
    path=transfer[4]
    with pytest.raises(RuntimeError,match='published then failed'):
        protected.execute(db,transfer[5],entry_id=protected.PREVIEW,params={'files':[(path.name,path.read_bytes())]})
    assert set(directory.iterdir())==prior
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0


def test_staged_actual_apply_rechecks_epoch_without_early_actor_lock(db,transfer,monkeypatch):
    response=preview(transfer);assert response.status_code==200,response.text
    payload=body(response); actor_id=transfer[2].id
    original=protected.TransferAuthorization.finish_apply
    def revoke(permit,transaction,row,**kwargs):
        assert transaction.scalar(select(MaintenanceProject.project_id).where(MaintenanceProject.display_name=='S3 transfer new project'))
        with Session(db.get_bind()) as admin:
            admin.execute(update(SysUser).where(SysUser.id==actor_id).values(token_version=SysUser.token_version+1));admin.commit()
        return original(permit,transaction,row,**kwargs)
    monkeypatch.setattr(protected.TransferAuthorization,'finish_apply',revoke)
    result=apply(transfer,payload);assert result.status_code==401,result.text
    db.expire_all()
    assert db.scalar(select(MaintenanceProject.project_id).where(MaintenanceProject.display_name=='S3 transfer new project')) is None
    assert db.get(SysImportBatch,response.json()['preview_id']).status=='processing'


def test_export_actions_independent_scope_and_fields(db,transfer):
    client,headers,actor,_,_,_,ids=transfer
    path='/api/maintenance/project-batch-transfer/'
    assert client.get(path+'options',headers=headers).status_code==403
    grant=give(db,actor,['read_options','download'],resource=protected.EXPORT,scope='assigned_or_sales_projects')
    options=client.get(path+'options',headers=headers);assert options.status_code==200,options.text
    assert 'authorization_context' in options.json()
    assert 'known_cost' not in {row['key'] for row in options.json()['download_fields']}
    view=client.get(path+'options',headers=headers,params={'view':'authorization'});assert view.status_code==200,view.text
    assert options.json()['authorization_context']==view.json()['authorization_context']
    downloaded=client.post(path+'download',headers=headers,json={'forms':['project_master'],'fields':['project_name']})
    assert downloaded.status_code==200,downloaded.text
    from openpyxl import load_workbook
    from io import BytesIO
    workbook=load_workbook(BytesIO(downloaded.content));values=str(list(workbook.active.values))
    assert 'Secret manager' in values and 'Secret other' not in values


def test_ruling_is_exact_f_action_and_checks_actual_contract_parent(db,transfer):
    from datetime import date
    from decimal import Decimal
    from uuid import uuid4
    from app.models.maintenance_project import MaintenanceProjectContract
    from app.models.maintenance_project_operations import MaintenanceCollectionReceipt
    client,headers,_,grant,_,_,ids=transfer
    contract=MaintenanceProjectContract(project_contract_id=str(uuid4()),project_id=ids['other'],
        contract_no=business.normalize_order_no('XSDD-RULING'),contract_id='transfer-ruling-contract',amount_inc_tax=Decimal('1000'),version=1,
        status_mapping_state='mapped',status_mapping_version='test-v1',effective_from=date(2026,1,1),source='test')
    db.add(contract);db.flush()
    receipt=MaintenanceCollectionReceipt(project_contract_id=contract.project_contract_id,contract_no=business.normalize_order_no('XSDD-RULING'),
        receipt_no='S3-SK-1',receipt_date=date(2026,1,1),actual_amount=Decimal('100'),is_active=True,created_by='fixture')
    db.add(receipt);grant.actions=['receipt_ruling'];grant.scope='assigned_projects';grant.version+=1;db.commit()
    payload={'contract_no':'XSDD-RULING','receipt_no':'S3-SK-1','receipt_date':'2026-01-01','actual_amount':'120','reason':'核对实际凭证'}
    path='/api/maintenance/project-batch-transfer/receipt-rulings'
    denied=client.post(path,headers=headers,json=payload);assert denied.status_code==403,denied.text
    grant.scope='all_projects';grant.version+=1;db.commit()
    view=client.post(path,headers=headers,json=payload,params={'view':'authorization'})
    assert view.status_code==200,view.text
    result=client.post(path,headers=headers,json=payload);assert result.status_code==200,result.text
    db.expire_all();assert not db.get(MaintenanceCollectionReceipt,receipt.id).is_active
    assert db.get(MaintenanceCollectionReceipt,result.json()['new_receipt_id']).actual_amount==Decimal('120')


def test_historical_preview_without_stable_owner_never_adopted(db,transfer):
    path=transfer[4]
    legacy=business.preview_transfer(db,[(path.name,path.read_bytes())],operated_by=transfer[2].username)
    payload={**{k:legacy[k] for k in ('preview_token','payload_hash','data_version')},
        'row_keys':[r['row_key'] for r in legacy['rows'] if r['row_status']=='ready']}
    response=apply(transfer,payload);assert response.status_code==403,response.text
    assert db.get(SysImportBatch,legacy['preview_id']).owner_user_id is None


def test_actual_batch_lock_wait_then_revocation_leaves_no_domain_writes(db,transfer):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from sqlalchemy import text
    response=preview(transfer);assert response.status_code==200,response.text
    payload=body(response);batch_id=int(response.json()['preview_id']);uid=transfer[2].id;identity=transfer[5]
    db.rollback()
    def worker():
        with Session(db.get_bind()) as command:
            try:
                protected.execute(command,identity,entry_id=protected.APPLY,params=payload)
                return 200
            except HTTPException as exc:return exc.status_code
    with Session(db.get_bind()) as blocker:
        blocker.scalar(select(SysImportBatch).where(SysImportBatch.id==batch_id).with_for_update())
        pid=blocker.scalar(text('SELECT pg_backend_pid()'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(worker);blocked=False;deadline=time.monotonic()+10
            try:
                while time.monotonic()<deadline:
                    with Session(db.get_bind()) as monitor:
                        blocked=monitor.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))'),{'pid':pid})
                    if blocked:break
                    time.sleep(.03)
                assert blocked,'worker did not reach actual batch row lock'
                with Session(db.get_bind()) as admin:
                    admin.execute(update(SysUser).where(SysUser.id==uid).values(token_version=SysUser.token_version+1));admin.commit()
            finally:blocker.rollback()
            assert future.result(timeout=15)==401
    assert db.get(SysImportBatch,batch_id).status=='processing'
    assert db.scalar(select(MaintenanceProject.project_id).where(MaintenanceProject.display_name=='S3 transfer new project')) is None


def test_denied_preview_discovers_only_keys_and_never_reads_fact_projection(db,transfer):
    from sqlalchemy import event
    grant=transfer[3];grant.field_permissions=[];grant.version+=1;db.commit()
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=preview(transfer)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==403,response.text
    selected=[s.split(' from ',1)[0] for s in statements if s.lstrip().startswith('select')]
    assert selected
    for projection in selected:
        assert not any(field in projection for field in ('.amount_ex_tax','.amount_inc_tax','.contract_amount',
            '.project_note','.end_customer','.receiver_phone','.source_payload','.actual_amount')),projection


def test_denied_apply_reads_only_opaque_input_hash_and_selected_scope(db,transfer):
    from sqlalchemy import event
    import re
    response=preview(transfer);assert response.status_code==200,response.text
    payload=body(response);grant=transfer[3];grant.actions=[];grant.version+=1;db.commit()
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:denied=apply(transfer,payload)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert denied.status_code==403,denied.text
    selected=[s.split('\nfrom ',1)[0] for s in statements if s.lstrip().startswith('select')]
    assert any('sha256(' in s and 'sys_import_batch' in s for s in selected)
    for projection in selected:
        assert not re.search(r'sys_import_batch\.report_json(?:\s+as\s+\w+)?\s*(?:,|$)',projection),projection
        assert not any(field in projection for field in ('.amount_ex_tax','.amount_inc_tax','.actual_amount')),projection
