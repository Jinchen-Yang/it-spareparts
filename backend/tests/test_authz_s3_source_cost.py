"""Real source-sales plans and per-resource field SQL for legacy cost surfaces."""
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select, update
from app.api import maintenance as api
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance import FMaintenanceOrder
from tests.test_authz_s3_analysis import analysis_client  # noqa:F401
from tests.test_authz_s3_workspace import workspace  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401

@pytest.fixture
def source_cost(db,analysis_client):
    _,headers,actor=analysis_client
    db.execute(update(FMaintenanceOrder).values(project_std='same-source-project',linked_sales_order_no='XSDD-S3'))
    db.execute(update(FMaintenanceOrder).where(FMaintenanceOrder.order_no=='WBDD-S3-other').values(salesperson=actor.salesperson_name))
    db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),headers,actor


def give(db,actor,actions,*,fields=(),scope='own_source_sales',resource='maintenance_cost'):
    grant=SysAuthorizationGrant(user_id=actor.id,resource=resource,actions=list(actions),scope=scope,field_permissions=list(fields),version=1)
    db.add(grant);db.commit();return grant


def captured(db,client,headers,path,params):
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.get(path,headers=headers,params=params)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    return response,statements


@pytest.mark.parametrize('path,action,params',[
 ('/api/maintenance/projects','read_projects',{'lifecycle':'all'}),
 ('/api/maintenance/lines','read_lines',{'project':'same-source-project','page_size':1}),
])
def test_source_scope_is_not_project_scope_and_hidden_cost_never_selected(db,source_cost,path,action,params):
    client,headers,actor=source_cost
    assert client.get(path,headers=headers,params=params).status_code==403
    row=give(db,actor,[action])
    response,sql=captured(db,client,headers,path,params)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['rows'][0]['qty']==7
    if action=='read_lines':assert result['total']==1 and result['rows'][0]['order_no']=='WBDD-S3-other'
    else:assert result['rows'][0]['order_count']==1
    for query in sql:
        assert not any(name in query for name in ['.cost_amount','.unit_cost','.amount_inc_tax','.receiver_phone','.receiver_address']),query
    row.scope='all_sources';row.version+=1;db.commit()
    all_response=client.get(path,headers=headers,params=params)
    assert all_response.status_code==200,all_response.text
    assert (all_response.json()['total'] if action=='read_lines' else all_response.json()['rows'][0]['order_count'])==3


def test_cost_field_isolated_from_contract_board_and_hidden_filter_denied(db,source_cost):
    client,headers,actor=source_cost
    give(db,actor,['read_lines'])
    give(db,actor,['read'],resource='maintenance_contract_cost',scope='all_contracts',fields=['data_purchase_cost','data_profit'])
    response=client.get('/api/maintenance/lines',headers=headers,params={'project':'same-source-project',
        'filters':json.dumps([{'field':'unit_cost_inc_tax','op':'gte','value':1}])})
    assert response.status_code in (403,422),response.text
    response=client.get('/api/maintenance/lines',headers=headers,params={'project':'same-source-project'})
    assert response.status_code==200,response.text
    assert response.json()['rows'][0]['unit_cost_inc_tax'] is None


def test_source_reassignment_invalidates_view_without_project_or_grant_change(db,source_cost):
    client,headers,actor=source_cost
    give(db,actor,['read_projects'])
    path='/api/maintenance/projects';params={'lifecycle':'all'}
    view=client.get(path,headers=headers,params={**params,'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    auth=view.json()['authorization_context'];pre={k:auth[k] for k in ('catalog_version','authorization_epoch','context')}
    ok=client.get(path,headers=headers,params={**params,'authorization_precondition':json.dumps(pre)})
    assert ok.status_code==200,ok.text
    db.execute(update(FMaintenanceOrder).where(FMaintenanceOrder.order_no=='WBDD-S3-other').values(salesperson='different'));db.commit()
    stale=client.get(path,headers=headers,params={**params,'authorization_precondition':json.dumps(pre)})
    assert stale.status_code==409,stale.text


def test_whole_contract_read_independent_no_financial_query_or_signal_when_fields_absent(db,source_cost):
    client,headers,actor=source_cost
    give(db,actor,['read'],resource='maintenance_contract_cost',scope='all_contracts')
    response,sql=captured(db,client,headers,'/api/maintenance/board',{'lifecycle':'all','status':'red'})
    assert response.status_code==200,response.text
    assert response.json()['rows'][0]['order_count']==3
    assert response.json()['decision_restricted'] and not response.json()['status_filter_applied']
    assert 'status_counts' not in response.json()
    for query in sql:
        assert not any(name in query for name in ['.cost_amount','.unit_cost','.amount_inc_tax','f_sales_line','f_project_expense']),query
    view=client.get('/api/maintenance/board',headers=headers,params={'view':'authorization'})
    assert view.status_code==200 and set(view.json())=={'locator','authorization_context'}


@pytest.mark.parametrize('path,action,resource,scope,params',[
 ('/api/maintenance/export','export_summary','maintenance_cost','own_source_sales',{'lifecycle':'all'}),
 ('/api/maintenance/lines/export','export_lines','maintenance_cost','own_source_sales',{'project':'same-source-project'}),
 ('/api/maintenance/board/export','export','maintenance_contract_cost','all_contracts',{'lifecycle':'all'}),
])
def test_exact_export_materializes_scope_and_masks_cost_without_read_action(db,source_cost,path,action,resource,scope,params):
    import csv,io
    client,headers,actor=source_cost
    give(db,actor,[action],scope=scope,resource=resource)
    view=client.get(path,headers=headers,params={**params,'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'} and view.headers['cache-control']=='no-store'
    response,sql=captured(db,client,headers,path,params)
    assert response.status_code==200,response.text
    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))));assert len(rows)==1
    if action=='export_lines':
        assert rows[0]['维保单号']=='WBDD-S3-other' and rows[0]['数量']=='7.0'
        assert rows[0]['单价']=='' and rows[0]['金额']=='' and rows[0]['置信度']==''
    elif action=='export_summary':assert rows[0]['维保订单数']=='1' and rows[0]['已知成本参考合计(兼容)']==''
    else:assert rows[0]['order_count']=='3' and rows[0]['parts_cost_inc_tax']=='' and rows[0]['revenue_inc']==''
    for query in sql:
        assert not any(name in query for name in ['.cost_amount','.unit_cost','.amount_inc_tax']),query


def test_export_revoked_after_render_before_headers_rejects_and_closes_spool(db,source_cost,monkeypatch):
    from sqlalchemy.orm import Session
    from app.models.system import SysUser
    client,headers,actor=source_cost;uid=actor.id
    give(db,actor,['export_summary'])
    original=api._render_project_csv;resources=[]
    def render(data,fields,*args,**kwargs):
        response=original(data,fields,*args,**kwargs);resources.append(response._resource)
        with Session(db.get_bind()) as revoker:
            revoker.get(SysUser,uid).token_version+=1;revoker.commit()
        return response
    monkeypatch.setattr(api,'_render_project_csv',render)
    response=client.get('/api/maintenance/export',headers=headers,params={'lifecycle':'all'})
    assert response.status_code in (401,403),response.text
    assert len(resources)==1 and resources[0].closed
