"""Real PDP source-specific import contracts and SQL-before-projection checks."""
from types import MappingProxyType
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event,select,func
from sqlalchemy.orm import Session
from app.authz import runtime
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.models.system import SysUser,SysImportBatch,SysImportError,SysImportJob,SysImportJobItem
from app.models.authorization import SysAuthorizationGrant
from app.services import source_import_access as access
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401

@pytest.fixture
def actor(db,monkeypatch,cerbos_url):
    settings=get_settings()
    monkeypatch.setattr(settings,'authorization_mode','unified_modules')
    monkeypatch.setattr(settings,'authz_enabled_resources',['import_'+s for s in access.SOURCES]+['import_expense_void'])
    monkeypatch.setattr(settings,'authz_suspended_resources',[])
    monkeypatch.setattr(settings,'cerbos_url',cerbos_url)
    monkeypatch.setattr(settings,'authz_sales_analysis_enabled',False)
    monkeypatch.setattr(settings,'authz_sales_analysis_suspended',False)
    user=SysUser(username='import-user',role='readonly',password_hash='synthetic',token_version=1)
    db.add(user);db.commit();runtime.activate_configured_resources(db.get_bind())
    return user,VerifiedIdentity(user.username,1,False,'sys_user',MappingProxyType({}))

def grant(db,user,source,actions,fields=()):
    row=SysAuthorizationGrant(user_id=user.id,resource='import_'+source,actions=actions,
        scope='all',field_permissions=list(fields),version=1)
    db.add(row);db.commit();return row

def batch(db,user,source):
    row=SysImportBatch(filename='secret-'+source+'.xlsx',file_type=source,file_hash=uuid4().hex*2,
        status='success',owner_user_id=user.id,authorization_protocol='authz_s4',source_domain=source,
        report_json={'secret':'hidden-financial-report'},rows_total=9)
    db.add(row);db.flush();db.add(SysImportError(batch_id=row.id,row_no=1,error_type='invalid',error_detail='hidden-row'))
    db.commit();return row


def test_independent_report_status_and_no_report_fact_projection(db,actor):
    user,identity=actor;g=grant(db,user,'sales',['read_status']);row=batch(db,user,'sales')
    queries=[]
    def capture(conn,cursor,statement,*args):queries.append(statement)
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:value=access.read(lambda:Session(db.get_bind()),identity,access.BATCH,{'batch_id':row.id})
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert value['metadata']['rows_total']==9 and value['report'] is None and value['errors']==[] and value['issue_count'] is None
    assert value['block_access']['report']['effect']=='deny'
    assert not any('SELECT sys_import_batch.report_json ' in q or 'SELECT sys_import_error.row_no' in q for q in queries)
    g.actions=['read_report'];g.field_permissions=['data_customer','data_supplier','data_purchase_cost','data_profit'];g.version+=1;db.commit()
    value=access.read(lambda:Session(db.get_bind()),identity,access.BATCH,{'batch_id':row.id})
    assert value['metadata'] is None and value['report']['secret']=='hidden-financial-report'
    assert value['issue_count']==1


def test_list_filters_before_limit_and_all_scope_is_not_owner_scope(db,actor):
    user,identity=actor;grant(db,user,'sales',['read_status'])
    other=SysUser(username='other-importer',role='readonly',password_hash='synthetic');db.add(other);db.commit()
    permitted=batch(db,other,'sales');batch(db,user,'purchase')
    value=access.read(lambda:Session(db.get_bind()),identity,access.BATCHES)
    assert [x['batch_id'] for x in value['items']]==[permitted.id]
    assert value['restricted'] is True and value['total'] is None


def test_mixed_job_hides_hidden_ordinals_counts_and_metadata(db,actor):
    user,identity=actor;grant(db,user,'sales',['read_status'])
    job=SysImportJob(created_by=user.username,owner_user_id=user.id,authorization_protocol='authz_s4',
        enqueued_epoch=1,authorization_generation=1,manifest_digest='b'*64,mode='skip',status='processing',total_files=2)
    db.add(job);db.flush()
    for ordinal,source in enumerate(('purchase','sales')):
        db.add(SysImportJobItem(id=uuid4().hex,job_id=job.id,ordinal=ordinal,source_domain=source,
            filename='secret-'+source+'.xlsx',content_sha256='a'*64,content_size=1,mode='skip',
            parser_version=1,input_manifest={},status='queued'))
    db.commit()
    value=access.read(lambda:Session(db.get_bind()),identity,access.JOB,{'job_id':job.id})['job']
    assert value['partial'] and value['restricted']
    assert len(value['items'])==1 and value['items'][0]['input_index'] is None
    assert value['items'][0]['source_domain']=='sales'
    assert all(value[k] is None for k in ('total_files','done_files','error_files','status','note','finished_at'))
    assert 'purchase' not in str(value)


def test_unknown_origin_denied_and_suspended_never_legacy(db,actor,monkeypatch):
    user,identity=actor;grant(db,user,'sales',['read_status']);row=batch(db,user,'sales')
    row.authorization_protocol=None;db.commit()
    with pytest.raises(HTTPException) as error:access.read(lambda:Session(db.get_bind()),identity,access.BATCH,{'batch_id':row.id})
    assert error.value.status_code==409 and error.value.detail['code']=='IMPORT_ORIGIN_UNVERIFIED'
    monkeypatch.setattr(get_settings(),'authz_enabled_resources',[])
    with pytest.raises(HTTPException) as error:access.read(lambda:Session(db.get_bind()),identity,access.BATCHES)
    assert error.value.status_code==503

@pytest.fixture
def client(db,actor):
    from app.api import imports as api
    from app.auth import _make_token
    from app.db import get_db
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    user,_=actor
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app)
    token,_=_make_token(user.role,user.username,None,token_version=user.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token
    return client


def _purchase():
    from tests.test_import_precheck_v2 import _workbook_bytes,_PURCHASE_HEADER,_PURCHASE_ROW
    return _workbook_bytes([('采购',[_PURCHASE_HEADER,_PURCHASE_ROW])])


def test_actual_http_precheck_submit_independent_and_context_zero_write(db,actor,client):
    from sqlalchemy import func
    user,_=actor;g=grant(db,user,'purchase',['submit_skip'])
    data=_purchase();files={'file':('purchase.xlsx',data)}
    view=client.post('/api/import/upload?view=authorization',files=files)
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0
    assert client.post('/api/import/precheck',files=[('files',('purchase.xlsx',data))]).status_code==403
    result=client.post('/api/import/upload',files=files)
    assert result.status_code==200,result.text
    assert result.json()['committed'] is True and 'report' not in result.json()
    row=db.get(SysImportBatch,result.json()['batch_id'],populate_existing=True)
    assert row.owner_user_id==user.id and row.source_domain=='purchase' and row.authorization_protocol=='authz_s4'
    assert client.get('/api/import/batches/'+str(row.id)).status_code==403
    assert client.post('/api/import/upload',files=files).status_code==409


def test_unknown_precheck_does_not_query_facts_or_create_job(db,actor,client):
    from tests.test_import_precheck_v2 import _workbook_bytes
    user,_=actor;grant(db,user,'sales',['precheck'])
    queries=[]
    def capture(conn,cursor,statement,*args):queries.append(statement)
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:
        response=client.post('/api/import/precheck',files=[('files',('unknown.xlsx',_workbook_bytes([('未知',[['x'],['y']])])))] )
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==200,response.text
    assert response.json()['inputs'][0]['source_domain'] is None
    assert not any('sys_import_batch' in q or 'sys_import_job' in q for q in queries)


def test_worker_rechecks_epoch_before_each_file(db,actor,client,monkeypatch):
    user,_=actor;grant(db,user,'purchase',['submit_skip'])
    monkeypatch.setattr(access,'enqueue_job',lambda *a:None)
    response=client.post('/api/import/upload-batch',files=[('files',('p.xlsx',_purchase()))])
    assert response.status_code==200,response.text
    jid=response.json()['job_id'];user.token_version+=1;db.commit()
    access.process_job(db.get_bind(),jid)
    row=db.scalar(select(SysImportJobItem).where(SysImportJobItem.job_id==jid))
    assert row.status=='authorization_failed' and row.error_code=='AUTHN_SESSION_INVALID'
    assert row.batch_id is None


def test_form_precondition_changed_input_is_not_replayed(db,actor,client):
    user,_=actor;grant(db,user,'purchase',['submit_skip'])
    first=client.post('/api/import/upload?view=authorization',files={'file':('p.xlsx',_purchase())})
    assert first.status_code==200,first.text
    c=first.json()['authorization_context'];pre={k:c[k] for k in ('catalog_version','authorization_epoch','context')}
    import json
    result=client.post('/api/import/upload',files={'file':('renamed.xlsx',_purchase())},data={'authorization_precondition':json.dumps(pre)})
    assert result.status_code==409,result.text
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0


def test_worker_two_files_keeps_first_commit_after_epoch_changes(db,actor,client,monkeypatch):
    from tests.test_import_precheck_v2 import _workbook_bytes,_PURCHASE_HEADER
    user,_=actor;grant(db,user,'purchase',['submit_skip'])
    monkeypatch.setattr(access,'enqueue_job',lambda *a:None)
    second=_workbook_bytes([('采购',[_PURCHASE_HEADER,['CG-2','PO-2','PL-2','PN-2',200]])])
    response=client.post('/api/import/upload-batch',files=[('files',('first.xlsx',_purchase())),('files',('second.xlsx',second))])
    assert response.status_code==200,response.text
    original=access._refresh
    def revoke(bind,source):
        with Session(bind) as other:
            current=other.get(SysUser,user.id);current.token_version+=1;other.commit()
        return 'complete'
    monkeypatch.setattr(access,'_refresh',revoke)
    jid=response.json()['job_id'];access.process_job(db.get_bind(),jid)
    db.expire_all();items=list(db.scalars(select(SysImportJobItem).where(SysImportJobItem.job_id==jid).order_by(SysImportJobItem.ordinal)))
    assert items[0].status=='success' and items[0].batch_id is not None
    assert items[1].status=='authorization_failed' and items[1].batch_id is None
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==1


def test_final_actor_check_rolls_back_import_after_other_connection_revokes(db,actor,client,monkeypatch):
    user,_=actor;grant(db,user,'purchase',['submit_skip'])
    original=access._current_authorize
    def revoke(session,identity,*args,**kwargs):
        from app.models.purchase import FPurchaseLine
        assert session.scalar(select(func.count()).select_from(FPurchaseLine))>0
        with Session(db.get_bind()) as other:
            current=other.get(SysUser,user.id);current.token_version+=1;other.commit()
        return original(session,identity,*args,**kwargs)
    monkeypatch.setattr(access,'_current_authorize',revoke)
    response=client.post('/api/import/upload',files={'file':('p.xlsx',_purchase())})
    assert response.status_code==401,response.text
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0


def test_void_preview_without_cost_never_selects_amount_and_v2_executes(db,actor,client,tmp_path):
    from tests.test_expense_void_preview_api import _seed,_anchored,R1,R2,_status
    user,_=actor;grant(db,user,'expense_void',['preview']);grant(db,user,'expense',['submit_upsert'])
    _seed(db,tmp_path);path=_anchored(tmp_path,'changed.xlsx',[R1,R2])
    queries=[]
    def capture(conn,cursor,statement,*args):queries.append(statement)
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.post('/api/import/expense-void-preview',files=[('files',(path.name,path.read_bytes()))])
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==200,response.text
    result=response.json();assert result['fingerprint_version']==2
    pv=result['previews'][0];assert pv['status']=='ready' and pv['void']['amount'] is None
    assert all(r['amount'] is None for r in pv['void_rows'])
    assert any('sha256(convert_to' in q for q in queries)
    assert not any('f_project_expense.amount,' in q for q in queries)
    upload=client.post('/api/import/upload?mode=upsert',files={'file':(path.name,path.read_bytes())},data={'preview_token':pv['preview_token']})
    assert upload.status_code==200,upload.text
    db.expire_all();assert _status(db,'租金')=='已作废'


def test_csv_rechecks_after_audit_before_publishing(db,actor,client,monkeypatch):
    from app.api import imports as api
    user,_=actor;grant(db,user,'sales',['export_errors'],['data_customer','data_supplier','data_purchase_cost','data_profit'])
    row=batch(db,user,'sales')
    def revoke(*a,**k):
        with Session(db.get_bind()) as other:
            current=other.get(SysUser,user.id);current.token_version+=1;other.commit()
    monkeypatch.setattr(api,'record_access_log',revoke)
    response=client.get(f'/api/import/batches/{row.id}/errors.csv')
    assert response.status_code==401,response.text
    assert 'hidden-row' not in response.text


def test_worker_rechecks_entire_remaining_manifest_after_prior_commit(db,actor,client,monkeypatch):
    from tests.test_import_precheck_v2 import _workbook_bytes,_PURCHASE_HEADER
    user,_=actor;grant(db,user,'purchase',['submit_skip'])
    monkeypatch.setattr(access,'enqueue_job',lambda *a:None)
    second=_workbook_bytes([('采购',[_PURCHASE_HEADER,['CG-2','PO-2','PL-2','PN-2',200]])])
    response=client.post('/api/import/upload-batch',files=[('files',('first.xlsx',_purchase())),('files',('second.xlsx',second))])
    assert response.status_code==200,response.text
    jid=response.json()['job_id']
    def tamper(bind,source):
        with Session(bind) as other:
            item=other.scalar(select(SysImportJobItem).where(SysImportJobItem.job_id==jid,SysImportJobItem.ordinal==1))
            item.mode='upsert';other.commit()
        return 'complete'
    monkeypatch.setattr(access,'_refresh',tamper)
    access.process_job(db.get_bind(),jid);db.expire_all()
    items=list(db.scalars(select(SysImportJobItem).where(SysImportJobItem.job_id==jid).order_by(SysImportJobItem.ordinal)))
    assert items[0].status=='success' and items[1].status=='authorization_failed'
    assert items[1].error_code=='AUTHZ_ARTIFACT_STALE'
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==1


def test_worker_final_plan_tamper_rolls_back_current_file(db,actor,client,monkeypatch):
    user,_=actor;grant(db,user,'purchase',['submit_skip'])
    monkeypatch.setattr(access,'enqueue_job',lambda *a:None)
    response=client.post('/api/import/upload-batch',files=[('files',('first.xlsx',_purchase()))])
    assert response.status_code==200,response.text
    jid=response.json()['job_id'];original=access._verify_job_plan;calls=0
    def tamper(session,job_id):
        nonlocal calls
        calls+=1
        if calls==2:
            # Real current transaction mutation must not be hidden by a stale
            # previously captured Python manifest, even before the next file.
            item=session.scalar(select(SysImportJobItem).where(SysImportJobItem.job_id==job_id))
            item.filename='tampered.xlsx';session.flush()
        return original(session,job_id)
    monkeypatch.setattr(access,'_verify_job_plan',tamper)
    access.process_job(db.get_bind(),jid)
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0
    item=db.scalar(select(SysImportJobItem).where(SysImportJobItem.job_id==jid))
    assert item.error_code=='AUTHZ_ARTIFACT_STALE'


def test_reader_error_before_new_receipt_never_claims_historical_failed_batch(db,actor,client,monkeypatch):
    from app.etl import pipeline
    from app.etl.reader import ReaderError
    user,_=actor;grant(db,user,'purchase',['submit_skip'])
    old=batch(db,user,'purchase');old.status='failed';db.commit()
    def fail_before_receipt(*args,**kwargs):raise ReaderError('synthetic failure before receipt')
    monkeypatch.setattr(pipeline,'run_import',fail_before_receipt)
    response=client.post('/api/import/upload',files={'file':('p.xlsx',_purchase())})
    assert response.status_code==500,response.text
    assert response.json()['detail']['commit_state']=='none'
    assert 'batch_id' not in response.json()['detail']
    assert db.get(SysImportBatch,old.id,populate_existing=True).owner_user_id==user.id


def test_legacy_integrity_failure_processes_the_next_file(db,tmp_path,monkeypatch):
    from app.api import imports as api
    from app.etl import loader
    job=SysImportJob(created_by='synthetic',mode='skip',total_files=2,status='processing');db.add(job);db.commit()
    paths=[]
    for i in range(2):
        p=tmp_path/f'file-{i}.xlsx';p.write_bytes(b'synthetic');paths.append((str(p),p.name))
    calls=[]
    def invalid(*args,**kwargs):
        calls.append(args[2]);raise loader.ImportIntegrityError('synthetic integrity error')
    monkeypatch.setattr(api,'SessionLocal',lambda:Session(db.get_bind()))
    monkeypatch.setattr(api.pipeline,'run_import',invalid)
    monkeypatch.setattr(api,'_post_import_refresh',lambda *a:None)
    api._process_import_job(job.id,paths,'skip','synthetic')
    current=db.get(SysImportJob,job.id,populate_existing=True)
    assert len(calls)==2 and current.error_files==2 and current.status=='failed'
    assert current.note.count('完整性失败')==2


def test_form_unknown_and_duplicate_preconditions_are_rejected_without_write(db,actor,client):
    user,_=actor;grant(db,user,'purchase',['submit_skip'])
    response=client.post('/api/import/upload',files={'file':('p.xlsx',_purchase())},data={'permissions':'{}'})
    assert response.status_code==422,response.text
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0


def test_never_cutover_authorization_view_never_runs_legacy_upload(db,actor,client,monkeypatch):
    from app.api import imports as api
    from app.models.authorization import SysAuthorizationCutover
    from sqlalchemy import delete
    user,_=actor
    db.execute(delete(SysAuthorizationCutover).where(SysAuthorizationCutover.resource.like('import_%')));db.commit()
    monkeypatch.setattr(get_settings(),'authz_enabled_resources',[])
    def forbidden(*a,**k):raise AssertionError('legacy upload must not run in authorization view')
    monkeypatch.setattr(api,'_legacy_upload',forbidden)
    response=client.post('/api/import/upload?view=authorization',files={'file':('p.xlsx',_purchase())})
    assert response.status_code==503,response.text
    assert db.scalar(select(func.count()).select_from(SysImportBatch))==0


def source_file(tmp_path, source):
    from pathlib import Path
    from tests.test_import_precheck_v2 import _workbook_bytes, _INVENTORY_HEADER
    if source == 'purchase':
        return _purchase()
    if source == 'sales':
        from tests.test_maintenance_bulk_import import _ordinary_sales_workbook
        return Path(_ordinary_sales_workbook(tmp_path, order_no='XSDD-S4',
            raw_order_id='S4-sales', project_name='S4实际销售建项')).read_bytes()
    if source == 'inventory':
        return _workbook_bytes([('库存', [_INVENTORY_HEADER, ['S4-I', 'PN-S4', 3, '总仓']])])
    if source == 'maintenance':
        from tests.wbdd_fixtures import make_rows, write_workbook, COLUMNS_91
        path = tmp_path / 'maintenance.xlsx'
        write_workbook(str(path), COLUMNS_91, make_rows(orders=1, lines_per_order=1))
        return path.read_bytes()
    from tests.test_expense_void_preview_api import _anchored, R1
    return _anchored(tmp_path, 'expense.xlsx', [R1]).read_bytes()


@pytest.mark.parametrize('source', access.SOURCES)
def test_each_actual_source_commits_only_its_explicit_import_contract(db, actor, client, tmp_path, source):
    user, _ = actor
    grant(db, user, source, ['submit_skip'])
    response = client.post('/api/import/upload', files={'file': (source+'.xlsx', source_file(tmp_path, source))})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['source_domain'] == source and result['committed'] is True
    assert result['status'] == 'success' and result['derived_refresh'] == 'complete'
    row = db.get(SysImportBatch, result['batch_id'], populate_existing=True)
    assert row.source_domain == source and row.owner_user_id == user.id
    assert row.authorization_protocol == 'authz_s4'
    assert set(result['authorization_context']['resources']) == {'import_'+source}
    # Source effects retain the business import contract without acquiring a
    # maintenance-project or report Grant as an accidental additional gate.
    if source == 'sales':
        from app.models.maintenance_project import MaintenanceProject
        assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 1


def test_mixed_sources_require_all_actual_submit_actions_before_enqueuing(db, actor, client, tmp_path, monkeypatch):
    user, _ = actor
    grant(db, user, 'purchase', ['submit_skip'])
    enqueued = []
    monkeypatch.setattr(access, 'enqueue_job', lambda *args: enqueued.append(args))
    response = client.post('/api/import/upload-batch', files=[
        ('files', ('purchase.xlsx', _purchase())),
        ('files', ('inventory.xlsx', source_file(tmp_path, 'inventory'))),
    ])
    assert response.status_code == 403, response.text
    assert db.scalar(select(func.count()).select_from(SysImportJob)) == 0
    assert db.scalar(select(func.count()).select_from(SysImportJobItem)) == 0
    assert enqueued == []


def test_worker_rejects_initial_manifest_tampering_with_terminal_safe_state(db, actor, client, monkeypatch):
    user, _ = actor
    grant(db, user, 'purchase', ['submit_skip'])
    monkeypatch.setattr(access, 'enqueue_job', lambda *args: None)
    response = client.post('/api/import/upload-batch', files=[('files', ('purchase.xlsx', _purchase()))])
    assert response.status_code == 200, response.text
    job_id = response.json()['job_id']
    item = db.scalar(select(SysImportJobItem).where(SysImportJobItem.job_id == job_id))
    item.mode = 'upsert'
    db.commit()
    access.process_job(db.get_bind(), job_id)
    db.expire_all()
    assert item.status == 'authorization_failed' and item.error_code == 'AUTHZ_ARTIFACT_STALE'
    assert db.get(SysImportJob, job_id).status == 'failed'
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0


def test_void_v2_detects_amount_drift_even_when_actor_cannot_read_cost(db, actor, client, tmp_path):
    from tests.test_expense_void_preview_api import _seed, _anchored, R1, R2, _status
    from app.models.maintenance import FProjectExpense
    user, _ = actor
    grant(db, user, 'expense_void', ['preview'])
    grant(db, user, 'expense', ['submit_upsert'])
    _seed(db, tmp_path)
    path = _anchored(tmp_path, 'changed.xlsx', [R1, R2])
    preview = client.post('/api/import/expense-void-preview', files=[('files', (path.name, path.read_bytes()))])
    assert preview.status_code == 200, preview.text
    value = preview.json()['previews'][0]
    assert value['void']['amount'] is None
    victim = db.scalar(select(FProjectExpense).where(FProjectExpense.reason == '租金'))
    from decimal import Decimal
    victim.amount = victim.amount_ex_tax = victim.amount + 1
    victim.amount_inc_tax = (victim.amount * Decimal('1.13')).quantize(Decimal('0.01'))
    db.commit()
    response = client.post('/api/import/upload?mode=upsert', files={'file': (path.name, path.read_bytes())},
        data={'preview_token': value['preview_token']})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'IMPORT_VOID_PREVIEW_STALE'
    assert response.json()['detail']['commit_state'] == 'receipt_only'
    assert _status(db, '租金') == '已结束'


def test_pdp_failure_does_not_create_any_import_receipt_or_job(db, actor, client, monkeypatch):
    user, _ = actor
    grant(db, user, 'purchase', ['submit_skip'])
    monkeypatch.setattr(get_settings(), 'cerbos_url', 'http://127.0.0.1:1')
    response = client.post('/api/import/upload', files={'file': ('purchase.xlsx', _purchase())})
    assert response.status_code == 503, response.text
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0
    assert db.scalar(select(func.count()).select_from(SysImportJob)) == 0


def test_worker_locks_complete_plan_until_current_file_commit(db, actor, client, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    from sqlalchemy import text
    user, _ = actor
    grant(db, user, 'purchase', ['submit_skip'])
    grant(db, user, 'inventory', ['submit_skip'])
    monkeypatch.setattr(access, 'enqueue_job', lambda *args: None)
    response = client.post('/api/import/upload-batch', files=[
        ('files', ('p.xlsx', _purchase())), ('files', ('i.xlsx', source_file(tmp_path, 'inventory')))])
    assert response.status_code == 200, response.text
    job_id = response.json()['job_id']
    started = Event(); pids = []; futures = []
    original = access._current_authorize
    with ThreadPoolExecutor(max_workers=1) as worker:
        def edit_later_item():
            with Session(db.get_bind()) as writer:
                pids.append(writer.scalar(text('select pg_backend_pid()')))
                started.set()
                item = writer.scalar(select(SysImportJobItem).where(
                    SysImportJobItem.job_id == job_id, SysImportJobItem.ordinal == 1).with_for_update())
                item.mode = 'upsert'
                writer.commit()
        def before_commit(session, identity, *args, **kwargs):
            if not futures:
                futures.append(worker.submit(edit_later_item))
                assert started.wait(5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with Session(db.get_bind()) as observer:
                        blockers = observer.scalar(text('select pg_blocking_pids(:pid)'), {'pid': pids[0]})
                    if blockers:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail('later immutable item was not locked before final authorization')
                assert not futures[0].done()
            return original(session, identity, *args, **kwargs)
        def after_commit(bind, source):
            futures[0].result(timeout=5)
            return 'complete'
        monkeypatch.setattr(access, '_current_authorize', before_commit)
        monkeypatch.setattr(access, '_refresh', after_commit)
        access.process_job(db.get_bind(), job_id)
    db.expire_all()
    rows = list(db.scalars(select(SysImportJobItem).where(SysImportJobItem.job_id == job_id).order_by(SysImportJobItem.ordinal)))
    assert rows[0].status == 'success' and rows[1].error_code == 'AUTHZ_ARTIFACT_STALE'
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 1


def test_real_http_wire_payloads_match_fixed_contract(db, actor, client, tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from tests.test_expense_void_preview_api import _seed, _anchored, R1, R2
    user, identity = actor
    grant(db, user, 'purchase', ['precheck', 'submit_skip', 'read_status', 'read_report', 'export_errors'],
        ['data_customer', 'data_supplier', 'data_purchase_cost', 'data_profit'])
    grant(db, user, 'sales', ['read_status'])
    grant(db, user, 'expense', ['submit_skip'])
    grant(db, user, 'expense_void', ['preview'])
    def result(response):
        assert response.status_code == 200, response.text
        return response.json()
    sample = {'snapshot': access.service.my_snapshot(db, identity)}
    sample['precheck_resolution'] = result(client.post('/api/import/precheck?view=authorization', files=[('files', ('purchase.xlsx', _purchase()))]))
    sample['precheck'] = result(client.post('/api/import/precheck', files=[('files', ('purchase.xlsx', _purchase()))]))
    sample['receipt'] = result(client.post('/api/import/upload', files={'file': ('purchase.xlsx', _purchase())}))
    bid = sample['receipt']['batch_id']
    sample['list_resolution'] = result(client.get('/api/import/batches?view=authorization'))
    sample['list'] = result(client.get('/api/import/batches'))
    sample['report'] = result(client.get(f'/api/import/batches/{bid}'))
    sales = batch(db, user, 'sales')
    sample['status_only'] = result(client.get(f'/api/import/batches/{sales.id}'))
    monkeypatch.setattr(access, 'enqueue_job', lambda *args: None)
    payload = source_file(tmp_path, 'expense')
    job = result(client.post('/api/import/upload-batch', files=[
        ('files', ('purchase.xlsx', _purchase())), ('files', ('expense.xlsx', payload))]))
    sample['partial_job_resolution'] = result(client.get(f'/api/import/jobs/{job["job_id"]}?view=authorization'))
    sample['partial_job'] = result(client.get(f'/api/import/jobs/{job["job_id"]}'))
    _seed(db, tmp_path)
    path = _anchored(tmp_path, 'changed.xlsx', [R1, R2])
    sample['void_resolution'] = result(client.post('/api/import/expense-void-preview?view=authorization', files=[('files', (path.name, path.read_bytes()))]))
    sample['void'] = result(client.post('/api/import/expense-void-preview', files=[('files', (path.name, path.read_bytes()))]))
    for row in sample['void']['previews']:
        if 'preview_token' in row:
            row['preview_token'] = 'opaque-preview-token-redacted'
    assert sample['partial_job']['job']['restricted'] is True
    assert sample['status_only']['report'] is None
    Path('/tmp/itsp-s4-import-wire-real.json').write_text(json.dumps(sample, ensure_ascii=False, indent=2, default=str)+'\n')


@pytest.mark.parametrize('moment', ['before_loader', 'before_derived'])
def test_source_permit_rejects_actual_plan_mutation_before_consumption(db, actor, client, monkeypatch, tmp_path, moment):
    from app.services.source_import_permit import SourceImportPermit
    from app.etl import loader
    from app.models.sales import FSalesOrder
    user, _ = actor
    grant(db, user, 'sales', ['submit_skip'])
    if moment == 'before_loader':
        original = SourceImportPermit.bind_plan
        def changed(self, session, plan):
            original(self, session, plan)
            plan.lines[0]['description'] = 'changed after authorization binding'
        monkeypatch.setattr(SourceImportPermit, 'bind_plan', changed)
    else:
        original = loader.load
        def changed(session, plan, *args, **kwargs):
            result = original(session, plan, *args, **kwargs)
            assert session.scalar(select(func.count()).select_from(FSalesOrder)) == 1
            plan.lines[0]['description'] = 'changed before project derivation'
            return result
        monkeypatch.setattr(loader, 'load', changed)
    response = client.post('/api/import/upload', files={'file': ('sales.xlsx', source_file(tmp_path, 'sales'))})
    assert response.status_code == 500, response.text
    assert response.json()['detail']['commit_state'] == 'none'
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0
    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 0


def test_source_permit_is_invalid_after_real_transaction_failure(db, actor, tmp_path):
    from app.services.source_import_permit import SourceImportPermit
    from sqlalchemy.exc import IntegrityError
    user, identity = actor
    grant(db, user, 'purchase', ['submit_skip'])
    path = tmp_path / 'purchase.xlsx'; path.write_bytes(_purchase())
    uploaded = access.inspect_input(str(path), path.name, 'application/octet-stream')
    params = access._input_params((uploaded,), 'skip')
    with access._snapshot(lambda: Session(db.get_bind()), identity, access.UPLOAD, params) as (_, _, batch, permits):
        checked = permits.require(batch.contexts[0].ref, 'import_purchase', 'submit_skip')
    if not db.in_transaction():
        db.begin()
    permit = SourceImportPermit(db, checked, source='purchase', content_sha256=uploaded.content_sha256, mode='skip')
    duplicate = SysUser(username=user.username, role='readonly', password_hash='synthetic')
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.flush()
    try:
        with pytest.raises(ValueError, match='会话或事务'):
            permit.check(db)
    finally:
        db.rollback()
