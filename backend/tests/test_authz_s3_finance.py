"""Finance grants and command contexts preserve business validation and OCC."""
import json
from datetime import date
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

from app.api import maintenance_project_operations as api
from app.db import get_db
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot, MaintenanceProjectExpenseAttribution
from tests.test_authz_s3_workspace import workspace, grant  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401


@pytest.fixture
def finance(db,workspace):
    _,headers,ids,actor=workspace
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),headers,ids,actor


def _pre(payload):
    context=payload['authorization_context']
    return {key:context[key] for key in ('catalog_version','authorization_epoch','context')}


def collection_body(**overrides):
    return {'project_contract_id':'s3-workspace-contract','report_month':'2026-02-01',
        'cumulative_amount':'300.00','status':'confirmed','reason':'独立授权回款',**overrides}


def test_collection_custom_grant_preview_zero_write_then_create_and_update(db,finance):
    client,headers,ids,actor=finance
    grant(db,actor,'maintenance_collection',['create','update'],['data_profit'])
    path=f"/api/maintenance/projects/stable/{ids['manager']}/collections";body=collection_body()
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:view=client.post(path+'?view=authorization',headers=headers,json=body)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert view.status_code==201,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert view.json()['locator']=={'project_id':ids['manager']}
    assert not any(q.lstrip().startswith(('insert ','update ','delete ')) for q in sql)
    result=client.post(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==201,result.text
    assert result.json()['cumulative_amount']=='300.00'
    cid=result.json()['collection_id'];edit=f'/api/maintenance/projects/stable/collections/{cid}'
    edit_body={'version':1,'cumulative_amount':'400','reason':'更正'}
    edit_view=client.patch(edit+'?view=authorization',headers=headers,json=edit_body)
    assert edit_view.status_code==200,edit_view.text
    assert set(edit_view.json())=={'locator','authorization_context'}
    assert edit_view.json()['locator']=={'object_type':'collection_snapshot','object_id':cid}
    changed=client.patch(edit,headers=headers,json={**edit_body,'authorization_precondition':_pre(edit_view.json())})
    assert changed.status_code==200,changed.text
    assert changed.json()['version']==2
    stale=client.patch(edit,headers=headers,json={'version':1,'cumulative_amount':'500','reason':'旧版本'})
    assert stale.status_code==409
    assert db.get(MaintenanceCollectionSnapshot,cid).cumulative_amount==400


def test_collection_scope_contract_binding_and_monotonicity_survive(db,finance):
    client,headers,ids,actor=finance
    grant(db,actor,'maintenance_collection',['create'],['data_profit'])
    path=f"/api/maintenance/projects/stable/{ids['manager']}/collections"
    invalid=client.post(path,headers=headers,json=collection_body(cumulative_amount='100'))
    assert invalid.status_code==400,invalid.text
    other=f"/api/maintenance/projects/stable/{ids['other']}/collections"
    mismatched=client.post(other+'?view=authorization',headers=headers,json=collection_body())
    assert mismatched.status_code==409,mismatched.text
    assert db.scalar(select(MaintenanceCollectionSnapshot.collection_id).where(
        MaintenanceCollectionSnapshot.report_month==date(2026,2,1))) is None


def test_expense_create_and_readiness_need_own_resource_not_collection(db,finance):
    client,headers,ids,actor=finance
    grant(db,actor,'maintenance_collection',['create'],['data_profit'])
    path=f"/api/maintenance/projects/stable/{ids['manager']}/expenses"
    body={'expense_id':'s3-expense-new','project_contract_id':'s3-workspace-contract',
        'expense_ref':'BX-S3','expense_date':'2026-02-01','amount_ex_tax':'10',
        'raw_status':'approved','status_mapping_state':'mapped','normalized_status':'approved',
        'status_mapping_version':'s3','reason':'明确费用授权'}
    assert client.post(path,headers=headers,json=body).status_code==403
    grant(db,actor,'maintenance_expense',['create','mark_readiness'],['data_profit'])
    created=client.post(path,headers=headers,json=body)
    assert created.status_code==201,created.text
    assert created.json()['amount_ex_tax']=='10.00'
    ready=client.put(path+'/readiness',headers=headers,json={'ready_through':'2026-02-01','reason':'核验完成'})
    assert ready.status_code==200,ready.text
    assert ready.json()['expense_ready_through']=='2026-02-01'
    correction=client.put(path+'/readiness',headers=headers,json={'ready_through':'2026-01-01','reason':'回退'})
    assert correction.status_code==409,correction.text
    assert db.get(MaintenanceProjectExpenseAttribution,'s3-expense-new') is not None


def test_collection_waiting_on_domain_lock_rechecks_revoked_epoch_before_insert(db,finance,projects):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import text, update
    from sqlalchemy.orm import Session
    from fastapi import HTTPException
    from app.models.maintenance_project import MaintenanceProject
    from app.models.system import SysUser
    from app.services import maintenance_finance_access as access
    _,_,ids,actor=finance;identity=projects[1]
    grant(db,actor,'maintenance_collection',['create'],['data_profit'])
    engine=db.get_bind();started=Event();worker_pid=[]
    def execute():
        with Session(engine) as worker:
            worker_pid.append(worker.scalar(text('select pg_backend_pid()')));started.set()
            return access.execute(worker,identity,entry_id=access.COLLECTION_CREATE,project_id=ids['manager'],
                command={**collection_body(),'report_month':date(2026,2,1),'receipt_reference':None,'remark':None})
    with Session(engine) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(select(MaintenanceProject).where(MaintenanceProject.project_id==ids['manager']).with_for_update())
        future=pool.submit(execute);assert started.wait(5)
        with Session(engine) as observer:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if observer.scalar(text('select cardinality(pg_blocking_pids(:pid))'),{'pid':worker_pid[0]}):break
                time.sleep(.02)
            else:pytest.fail('writer did not wait on the actual project lock')
            observer.execute(update(SysUser).where(SysUser.id==actor.id).values(token_version=actor.token_version+1));observer.commit()
        blocker.commit()
        with pytest.raises(HTTPException) as denied:future.result(timeout=10)
        assert denied.value.status_code==401
    db.expire_all()
    assert db.scalar(select(MaintenanceCollectionSnapshot.collection_id).where(
        MaintenanceCollectionSnapshot.report_month==date(2026,2,1))) is None


def test_expense_status_actual_child_context_and_own_field_contract(db,finance):
    from decimal import Decimal
    client,headers,ids,actor=finance
    for label in ['manager','other']:
        db.add(MaintenanceProjectExpenseAttribution(expense_id='expense-status-'+label,
            project_id=ids[label],expense_ref='S3-'+label,expense_date=date(2026,2,1),
            amount_ex_tax=Decimal('10'),amount_inc_tax=Decimal('11.30'),raw_status='unknown',
            status_mapping_state='unmapped',normalized_status='unknown',status_mapping_version='s3'))
    db.commit()
    grant(db,actor,'maintenance_expense',['update_status'],['data_profit'])
    body={'version':1,'raw_status':'approved','normalized_status':'approved',
          'status_mapping_version':'s3-v2','reason':'真实费用状态'}
    path='/api/maintenance/projects/stable/expenses/expense-status-manager/status'
    view=client.patch(path+'?view=authorization',headers=headers,json=body)
    assert view.status_code==200,view.text
    assert view.json()['locator']=={'object_type':'expense','object_id':'expense-status-manager'}
    result=client.patch(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==200,result.text
    assert result.json()['normalized_status']=='approved'
    foreign=client.patch(path.replace('-manager/','-other/'),headers=headers,json=body)
    assert foreign.status_code==403,foreign.text
    assert db.get(MaintenanceProjectExpenseAttribution,'expense-status-other').version==1


def test_cost_fill_and_recompute_actions_use_current_line_and_complete_domain_lock_set(db,finance):
    from app.models.maintenance_project import MaintenanceProject
    from app.models.maintenance_project_operations import MaintenanceSiteIssueLine
    from tests.test_maintenance_analytics import _part
    from tests.test_maintenance_return_metrics import _issue
    client,headers,ids,actor=finance
    part=_part(db,'S3-MANUAL-COST')
    line=_issue(db,db.get(MaintenanceProject,ids['manager']),part,'2')
    foreign=_issue(db,db.get(MaintenanceProject,ids['other']),part,'2')
    db.commit()
    grant(db,actor,'maintenance_cost_fill',['fill','recompute'],['data_purchase_cost'])
    path=f"/api/maintenance/projects/stable/{ids['manager']}/cost-gaps"
    body={'line_id':line.issue_line_id,'version':1,'unit_cost_ex_tax':'5',
          'evidence':'manual invoice','reason':'补充实际缺价证据'}
    view=client.patch(path+'?view=authorization',headers=headers,json=body)
    assert view.status_code==200,view.text
    denied=client.patch(path,headers=headers,json={**body,'line_id':foreign.issue_line_id})
    assert denied.status_code==404,denied.text
    result=client.patch(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==200,result.text
    assert result.json()['manual_applied'] is True
    db.expire_all();assert db.get(MaintenanceSiteIssueLine,line.issue_line_id).version==2
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:recompute=client.post(path+'/recompute',headers=headers,json={'reason':'新证据重算'})
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert recompute.status_code==200,recompute.text
    domain_locks=[i for i,q in enumerate(sql) if 'for update' in q and ('maintenance_site_issue' in q)]
    actor_locks=[i for i,q in enumerate(sql) if 'for share' in q and 'sys_user' in q]
    assert domain_locks and actor_locks and max(domain_locks)<min(actor_locks)


def test_readiness_precondition_treats_absent_workbook_state_as_revision_zero(db,finance):
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
    client,headers,ids,actor=finance
    grant(db,actor,'maintenance_expense',['mark_readiness'],['data_profit'])
    path=f"/api/maintenance/projects/stable/{ids['manager']}/expenses/readiness"
    body={'ready_through':'2026-02-01','reason':'独立水位','expected_version':0}
    assert db.get(MaintenanceProjectWorkbookState,ids['manager']) is None
    view=client.put(path+'?view=authorization',headers=headers,json=body)
    assert view.status_code==200,view.text
    assert db.get(MaintenanceProjectWorkbookState,ids['manager']) is None
    result=client.put(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==200,result.text


def test_contract_commands_keep_scope_occ_and_field_contract(db,finance):
    from app.models.maintenance_project import MaintenanceProjectContract
    client,headers,ids,actor=finance
    grant(db,actor,'maintenance_contract',['create','update','archive'],['data_profit'])
    body={'contract_id':'s3-created-contract','contract_no':'XSDD-20261023-0001',
        'contract_amount':'800','contract_status':'active','status_mapping_state':'mapped',
        'status_mapping_version':'s3','included_in_total':True,'effective_from':'2026-01-01',
        'source':'s3-test','reason':'明确合同授权'}
    path='/api/maintenance/projects/stable/'+ids['manager']+'/contracts'
    view=client.post(path,headers=headers,json=body,params={'view':'authorization'})
    assert view.status_code==201,view.text
    assert set(view.json())=={'locator','authorization_context'}
    result=client.post(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert result.status_code==201,result.text
    cid=result.json()['project_contract_id']; assert result.json()['contract_amount']=='800.00'
    edit='/api/maintenance/projects/stable/contracts/'+cid
    changes={'version':1,'contract_amount':'850','reason':'更正金额'}
    check=client.patch(edit,headers=headers,json=changes,params={'view':'authorization'})
    assert check.status_code==200,check.text
    assert check.json()['locator']=={'object_type':'contract','object_id':cid}
    changed=client.patch(edit,headers=headers,json={**changes,'authorization_precondition':_pre(check.json())})
    assert changed.status_code==200,changed.text
    assert changed.json()['version']==2
    assert client.patch(edit,headers=headers,json=changes).status_code==409
    archived=client.post(edit+'/archive',headers=headers,json={'version':2,'effective_to':'2027-01-01','reason':'归档'})
    assert archived.status_code==200,archived.text
    assert archived.json()['version']==3
    assert db.get(MaintenanceProjectContract,cid).effective_to==date(2027,1,1)
    foreign=client.post('/api/maintenance/projects/stable/'+ids['other']+'/contracts',headers=headers,
        json={**body,'contract_id':'forbidden','contract_no':'XSDD-20261023-0002'})
    assert foreign.status_code==403,foreign.text


def test_contract_write_cannot_borrow_profit_from_other_resource_and_deny_is_zero_write(db,finance):
    from app.models.maintenance_project import MaintenanceProjectContract
    client,headers,ids,actor=finance
    grant(db,actor,'maintenance_contract',['update'])
    grant(db,actor,'maintenance_collection',['create'],['data_profit'])
    path='/api/maintenance/projects/stable/contracts/s3-workspace-contract'
    writes=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        if statement.lower().lstrip().startswith(('insert ','update ','delete ')):writes.append(statement)
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.patch(path,headers=headers,json={'version':1,'contract_amount':'12','reason':'不可借字段'})
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==403,response.text
    assert writes==[]
    assert db.get(MaintenanceProjectContract,'s3-workspace-contract').amount_inc_tax==1000


def test_contract_resync_raw_lock_precedes_actor_and_revocation_rolls_back(db,finance,projects):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import text, update
    from sqlalchemy.orm import Session
    from fastapi import HTTPException
    from app.models.maintenance_project import MaintenanceProjectContract
    from app.models.maintenance import FProjectExpense
    from app.models.system import SysUser
    from app.services import maintenance_finance_access as access
    from tests.test_maintenance_expense_collection_workbook import _expense
    _,_,ids,actor=finance
    grant(db,actor,'maintenance_contract',['update'],['data_profit'])
    _expense(db,raw_line_id='S3-RAW-LOCK#1',contract_no='XSDD-S3')
    engine=db.get_bind();started=Event();worker_pid=[]
    def execute():
        with Session(engine) as worker:
            worker_pid.append(worker.scalar(text('select pg_backend_pid()')));started.set()
            return access.execute(worker,projects[1],entry_id=access.CONTRACT_UPDATE,
                project_contract_id='s3-workspace-contract',command={'version':1,'contract_amount':'1200','reason':'原始费用锁后撤权'})
    with Session(engine) as blocker,ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(select(FProjectExpense).where(FProjectExpense.raw_line_id=='S3-RAW-LOCK#1').with_for_update())
        future=pool.submit(execute);assert started.wait(5)
        with Session(engine) as observer:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if observer.scalar(text('select cardinality(pg_blocking_pids(:pid))'),{'pid':worker_pid[0]}):break
                time.sleep(.02)
            else:pytest.fail('contract writer did not wait on the actual resync raw row')
            observer.execute(text("SET LOCAL statement_timeout='2s'"))
            observer.execute(update(SysUser).where(SysUser.id==actor.id).values(token_version=actor.token_version+1))
            observer.commit()  # Must not wait on a premature actor SHARE.
        blocker.commit()
        with pytest.raises(HTTPException) as denied:future.result(timeout=10)
        assert denied.value.status_code==401
    db.expire_all()
    contract=db.get(MaintenanceProjectContract,'s3-workspace-contract')
    assert contract.version==1 and contract.amount_inc_tax==1000
    assert db.get(MaintenanceProjectExpenseAttribution,'bxd:S3-RAW-LOCK#1').version==1
