"""Six independent workspace contracts, tested through HTTP with real PG/PDP."""
from datetime import date
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, event, select

from app.api import maintenance_project_operations as api
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from tests.test_authz_s3_projects import canonical  # noqa: F401
from tests.test_authz_s3_project_foundation import projects  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def workspace(db, canonical, projects):
    _, headers, ids = canonical
    actor, _, _ = projects
    db.execute(delete(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == actor.id))
    relation = MaintenanceProjectContract(project_contract_id='s3-workspace-contract',
        project_id=ids['manager'],contract_id='s3-contract',contract_no='XSDD-S3',
        contract_amount=Decimal('900'),amount_inc_tax=Decimal('1000'),contract_status='secret-state',
        status_mapping_state='mapped',status_mapping_version='secret-version',included_in_total=True,
        effective_from=date(2026,1,1),source='secret-contract-source')
    db.add(relation); db.flush()
    db.add(MaintenanceCollectionSnapshot(collection_id='s3-workspace-collection',
        project_id=ids['manager'],project_contract_id=relation.project_contract_id,
        report_month=date(2026,1,1),cumulative_amount=Decimal('250'),status='confirmed',
        receipt_reference='secret-receipt',remark='secret-remark'))
    db.commit()
    app=FastAPI(); app.include_router(api.stable_workspace_router,prefix='/api')
    app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),headers,ids,actor


def grant(db, actor, resource, actions, fields=()):
    db.add(SysAuthorizationGrant(user_id=actor.id,resource=resource,actions=list(actions),
        scope='assigned_or_sales_projects',field_permissions=list(fields),version=1))
    db.commit()


def request(db,workspace):
    client,headers,ids,_=workspace
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:
        result=client.get(f"/api/maintenance/projects/stable/{ids['manager']}/workspace",
            headers=headers,params={'as_of':'2026-02-28'})
    finally:
        event.remove(db.get_bind(),'before_cursor_execute',capture)
    return result,statements


def test_no_workspace_grants_denies_before_fact_sql(db,workspace):
    response,sql=request(db,workspace)
    assert response.status_code==403,response.text
    assert not any('maintenance_project_contract' in q or 'maintenance_collection_snapshot' in q for q in sql)


def test_collection_only_remains_useful_without_identity_or_summary(db,workspace):
    actor=workspace[3]
    grant(db,actor,'maintenance_collection',['read_snapshots'])
    response,sql=request(db,workspace)
    assert response.status_code==200,response.text
    p=response.json()
    assert p['collection_snapshots']['total']==1
    assert p['collection_snapshots']['rows'][0]['cumulative_amount'] is None
    assert p['collection_snapshots']['rows'][0]['receipt_reference'] is None
    assert p['project']['project_id']==workspace[2]['manager']
    assert 'display_name' not in p['project'] and p['project']['metrics'] is None
    assert p['project']['contracts']==[]
    assert p['requisitions']['total'] is None and p['approved_expenses']['total'] is None
    assert p['block_access']['collection_snapshots']['effect']=='allow'
    assert p['block_access']['operation_summary']['effect']=='deny'
    assert not any('maintenance_project.display_name' in q or
        'maintenance_project_expense_attribution' in q or 'maintenance_site_issue' in q for q in sql)


def test_summary_only_narrow_projection_never_loads_contract_rows_or_identity(db,workspace):
    grant(db,workspace[3],'maintenance_project_operations',['read_workspace'],['data_profit','data_purchase_cost'])
    response,sql=request(db,workspace)
    assert response.status_code==200,response.text
    p=response.json()
    assert p['project']['metrics']['total_contract_amount']=='1000.00'
    assert p['project']['metrics']['received_amount']=='250.00'
    assert p['project']['contracts']==[] and 'display_name' not in p['project']
    for block in ('collection_snapshots','requisitions','approved_expenses'):
        assert p[block]['rows']==[] and p[block]['total'] is None
    assert not any('maintenance_project.display_name' in q for q in sql)
    contract_queries=[q for q in sql if 'maintenance_project_contract' in q]
    assert contract_queries
    assert not any('maintenance_project_contract.contract_amount' in q or
        'maintenance_project_contract.source' in q or
        'maintenance_project_contract.status_mapping_version' in q for q in contract_queries)
    assert 'secret-contract-source' not in response.text and 'secret-receipt' not in response.text


def test_summary_fields_do_not_enable_collection_money_or_expense_rows(db,workspace):
    actor=workspace[3]
    grant(db,actor,'maintenance_project_operations',['read_workspace'],['data_profit','data_purchase_cost'])
    grant(db,actor,'maintenance_collection',['read_snapshots'])
    grant(db,actor,'maintenance_expense',['read_workspace'])
    response,sql=request(db,workspace)
    assert response.status_code==200,response.text
    p=response.json()
    assert p['project']['metrics']['total_contract_amount']=='1000.00'
    assert p['collection_snapshots']['rows'][0]['cumulative_amount'] is None
    assert p['approved_expenses']['rows']==[] and p['approved_expenses']['total'] is None
    assert p['block_access']['approved_expenses']['effect']=='deny'
    # Summary may aggregate expenses; no complete row is selected for denied raw block.
    assert not any('maintenance_project_expense_attribution.expense_reason' in q for q in sql)


def test_workspace_authorization_view_has_no_facts_and_same_revision(db,workspace):
    import json
    grant(db,workspace[3],'maintenance_collection',['read_snapshots'],['data_profit'])
    client,headers,ids,_=workspace
    path=f"/api/maintenance/projects/stable/{ids['manager']}/workspace"
    view=client.get(path,headers=headers,params={'view':'authorization','as_of':'2026-02-28'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    context=view.json()['authorization_context']
    pre={k:context[k] for k in ('catalog_version','authorization_epoch','context')}
    result=client.get(path,headers=headers,params={'as_of':'2026-02-28',
        'authorization_precondition':json.dumps(pre)})
    assert result.status_code==200,result.text
    assert result.json()['authorization_context']['context']==pre['context']
    assert result.json()['collection_snapshots']['rows'][0]['cumulative_amount']=='250.00'


@pytest.mark.parametrize('resource,path,key',[
    ('maintenance_front_stock','front-stock','rows'),
    ('maintenance_recovery','recovery-summary','remaining_stock'),
])
def test_stock_and_recovery_scope_and_field_grants_are_independent(db,canonical,projects,resource,path,key):
    from app.api import maintenance_front_stock, maintenance_recovery
    from app.models.dimensions import DimPart
    from app.services import maintenance_front_stock as stock
    client,headers,ids=canonical
    actor=projects[0]
    part=DimPart(pn_std='S3-SENSITIVE-STOCK'); db.add(part); db.flush()
    for pid in (ids['manager'],ids['other']):
        stock.apply_movement(db,project_id=pid,part_id=part.id,kind='shipment_in',
            source_type='f_maintenance_line',source_ref='s3-'+pid,qty=Decimal('2'),
            warehouse_name='现场库',operated_by=actor.username,
            unit_cost_ex_tax=Decimal('100'),unit_cost_inc_tax=Decimal('113'))
    db.commit()
    # A separate resource has cost permission; it cannot enable the requested resource's money.
    grant(db,actor,'maintenance_project_operations',['read_workspace'],['data_profit','data_purchase_cost'])
    grant(db,actor,resource,['read'])
    app=FastAPI();app.include_router(maintenance_front_stock.router,prefix='/api')
    app.include_router(maintenance_recovery.router,prefix='/api')
    app.dependency_overrides[get_db]=lambda:db; client=TestClient(app)
    response=client.get(f"/api/maintenance/projects/stable/{ids['manager']}/{path}",headers=headers)
    assert response.status_code==200,response.text
    assert response.json()[key][0]['unit_cost_inc_tax'] is None
    assert response.json()[key][0]['value_inc_tax'] is None
    denied=client.get(f"/api/maintenance/projects/stable/{ids['other']}/{path}",headers=headers)
    assert denied.status_code==403,denied.text
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
        SysAuthorizationGrant.resource==resource)); row.field_permissions=['data_purchase_cost'];row.version+=1;db.commit()
    visible=client.get(f"/api/maintenance/projects/stable/{ids['manager']}/{path}",headers=headers)
    assert visible.status_code==200,visible.text
    assert visible.json()[key][0]['unit_cost_inc_tax']==113.0
    assert visible.json()[key][0]['value_inc_tax']==226.0


def test_expense_action_without_finance_field_does_not_admit_empty_workspace(db,workspace):
    grant(db,workspace[3],'maintenance_expense',['read_workspace'])
    response,sql=request(db,workspace)
    assert response.status_code==403,response.text
    assert not any('maintenance_project_expense_attribution' in q for q in sql)


@pytest.mark.parametrize('resource,action,path,fields',[
    ('maintenance_project_operations','read_tasks','tasks',[]),
    ('maintenance_cost_fill','read_gaps','cost-gaps',['data_purchase_cost']),
])
def test_independent_project_read_actions_do_not_require_project_identity(db,workspace,resource,action,path,fields):
    grant(db,workspace[3],resource,[action],fields)
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);headers=workspace[1];ids=workspace[2]
    response=client.get(f"/api/maintenance/projects/stable/{ids['manager']}/{path}",headers=headers)
    assert response.status_code==200,response.text
    assert 'rows' in response.json() and 'authorization_context' in response.json()
    denied=client.get(f"/api/maintenance/projects/stable/{ids['other']}/{path}",headers=headers)
    assert denied.status_code==403,denied.text


def test_project_purchase_scope_and_own_field_projection(db,workspace):
    from tests.test_maintenance_project_procurement import _seed_demand,_seed_purchase,_assign_source_order
    _seed_demand(db,'s3-purchase-demand','合成');_seed_purchase(db,'s3-purchase-order','s3-purchase-demand',price='153')
    _assign_source_order(db,source_order_id='s3-purchase-demand',project_id=workspace[2]['manager'])
    grant(db,workspace[3],'maintenance_project_purchase',['read'])
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);headers=workspace[1];ids=workspace[2]
    path=f"/api/maintenance/projects/stable/{ids['manager']}/purchases"
    result=client.get(path,headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['total']==1
    assert result.json()['purchases'][0]['lines'][0]['unit_price'] is None
    assert result.json()['purchases'][0]['lines'][0]['unit_price_masked'] is True
    assert client.get(path,headers=headers,params={'source_order_id':'different'}).json()['total']==0
    assert client.get(f"/api/maintenance/projects/stable/{ids['other']}/purchases",headers=headers).status_code==403


def test_operations_directory_scope_precedes_financial_ctes_and_count(db,workspace):
    actor=workspace[3]
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_operation_directory',actions=['read'],
        scope='assigned_projects',field_permissions=['data_profit','data_purchase_cost'],version=1));db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);headers=workspace[1];ids=workspace[2];sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.post('/api/maintenance/projects/stable/operations/search',headers=headers,
        json={'include_inactive':True,'as_of':'2026-02-28','reminder':'completeness','page_size':1})
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==200,response.text
    result=response.json();assert result['total']==3 and len(result['rows'])==1
    assert result['rows'][0]['project_id'] in {ids['manager'],ids['viewer'],ids['inactive_manager']}
    aggregate_sql=[q for q in sql if 'directory_contract_fact' in q]
    assert aggregate_sql
    for q in aggregate_sql:
        assert 'where maintenance_project_contract.project_id in' in q
        assert 'maintenance_collection_snapshot.project_id in' in q
        assert 'maintenance_site_issue.project_id in' in q
        assert 'maintenance_project_expense_attribution.project_id in' in q
        assert 'maintenance_collection_milestone.project_id in' in q
    hidden=client.post('/api/maintenance/projects/stable/operations/search',headers=headers,
        json={'q':'other','as_of':'2026-02-28'})
    assert hidden.status_code==200 and hidden.json()['total']==0
    directory=client.get('/api/maintenance/projects/stable/operations',headers=headers,
        params={'as_of':'2026-02-28','include_inactive':'true'})
    assert directory.status_code==200 and directory.json()['total']==3


def test_operations_directory_hidden_financial_filter_denies_before_cte(db,workspace):
    actor=workspace[3]
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_operation_directory',actions=['read'],
        scope='assigned_projects',field_permissions=[],version=1));db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app);sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.post('/api/maintenance/projects/stable/operations/search',headers=workspace[1],
        json={'reminder':'cost','as_of':'2026-02-28'})
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==403,response.text
    assert not any('directory_contract_fact' in q for q in sql)
