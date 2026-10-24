"""Board scope/field contracts are exercised through actual HTTP and PDP SQL."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select
from app.api import maintenance_boss_board as api
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from tests.test_authz_s3_analysis import analysis_client  # noqa:F401
from tests.test_authz_s3_workspace import workspace, grant  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401

@pytest.fixture
def board(db,analysis_client):
    _,headers,actor=analysis_client
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),headers,actor

@pytest.mark.parametrize('suffix,resource,action',[
 ('summary','maintenance_summary','read_summary'),('attention','maintenance_summary','read_attention'),
 ('projects','maintenance_project_cards','read'),
])
def test_board_independent_actions_scope_and_no_financial_sql(db,board,workspace,suffix,resource,action):
    client,headers,actor=board;path='/api/maintenance/boss-board/'+suffix
    assert client.get(path,headers=headers).status_code==403
    grant(db,actor,resource,[action])
    queries=[]
    def capture(conn,cursor,statement,parameters,context,executemany):queries.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.get(path,headers=headers)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==200,response.text
    assert 'Secret other' not in response.text and 'S3-other' not in response.text
    assert workspace[2]['other'] not in response.text
    facts=[q for q in queries if 'maintenance_authorization_relationship_digest' not in q]
    for q in facts:
        assert not any(token in q for token in ['.amount_inc_tax','.cost_amount','.unit_cost','.cumulative_amount'])
    if suffix=='projects':
        assert response.json()['total']==3
        assert all(row['contract_incomplete'] is None for row in response.json()['rows'])
    view=client.get(path,headers=headers,params={'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}


def test_board_project_read_is_independent_of_cards_and_cannot_open_foreign_project(db,board,workspace):
    client,headers,actor=board;ids=workspace[2]
    grant(db,actor,'maintenance_summary',['read_project'])
    result=client.get('/api/maintenance/boss-board/projects/'+ids['manager'],headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['project_id']==ids['manager']
    assert result.json()['can_edit_master_workbook'] is None
    assert client.get('/api/maintenance/boss-board/projects/'+ids['other'],headers=headers).status_code==403
    assert client.get('/api/maintenance/boss-board/projects',headers=headers).status_code==403


def test_card_search_and_financial_filters_use_card_resource_only(db,board):
    client,headers,actor=board
    grant(db,actor,'maintenance_project_cards',['read'])
    grant(db,actor,'maintenance_summary',['read_project'],['data_purchase_cost','data_profit'])
    path='/api/maintenance/boss-board/projects/search'
    found=client.post(path,headers=headers,json={'q':'other'})
    assert found.status_code==200,found.text
    assert found.json()['total']==0 and found.json()['rows']==[]
    for body in [{'q':'S3','sort':'cost_ratio'}, {'q':'S3','lifecycle':'payment_complete'},
                 {'q':'S3','card_status':'alert'}, {'q':'S3','sort':'known_cost'}]:
        assert client.post(path,headers=headers,json=body).status_code==422


def test_health_has_separate_global_grant(db,board):
    client,headers,actor=board;path='/api/maintenance/boss-board/health'
    grant(db,actor,'maintenance_summary',['read_summary'])
    assert client.get(path,headers=headers).status_code==403
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_source_health',
        actions=['read'],scope='all',field_permissions=[],version=1));db.commit()
    result=client.get(path,headers=headers)
    assert result.status_code==200,result.text
    assert 'authorization_context' in result.json()


@pytest.mark.parametrize('kind,action',[('orders','read_orders'),('lines','read_order_lines')])
def test_order_drilldowns_authorize_actual_parent_and_do_not_select_hidden_columns(db,board,workspace,kind,action):
    from app.models.maintenance import FMaintenanceOrder
    client,headers,actor=board
    grant(db,actor,'maintenance_summary',[action])
    order=db.scalar(select(FMaintenanceOrder).where(FMaintenanceOrder.order_no=='WBDD-S3-manager'))
    foreign=db.scalar(select(FMaintenanceOrder).where(FMaintenanceOrder.order_no=='WBDD-S3-other'))
    def url(project_id,source_id):
        return ('/api/maintenance/boss-board/projects/'+project_id+'/orders' if kind=='orders'
            else '/api/maintenance/boss-board/orders/'+source_id+'/lines')
    path=url(workspace[2]['manager'],order.raw_order_id)
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.get(path,headers=headers)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==200,response.text
    assert response.json()['total']==1
    assert response.json()['rows'][0]['known_apply_cost_inc_tax']['state']=='restricted'
    for statement in sql:
        assert not any(name in statement for name in ['.unit_cost','.cost_amount','.receiver_phone','.receiver_address']),statement
    assert client.get(url(workspace[2]['other'],foreign.raw_order_id),headers=headers).status_code==403
    view=client.get(path,headers=headers,params={'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}


@pytest.mark.parametrize('kind,action',[('orders','read_orders'),('lines','read_order_lines')])
def test_unassigned_requires_real_all_scope_and_assignment_change_stales_context(db,board,workspace,kind,action):
    import json
    from sqlalchemy import update
    from app.models.maintenance import FMaintenanceOrder
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    client,headers,actor=board
    grant(db,actor,'maintenance_summary',[action])
    order=db.scalar(select(FMaintenanceOrder).where(FMaintenanceOrder.order_no=='WBDD-S3-unassigned'))
    path=('/api/maintenance/boss-board/projects/unassigned/orders' if kind=='orders'
        else '/api/maintenance/boss-board/orders/'+order.raw_order_id+'/lines')
    assert client.get(path,headers=headers).status_code==403
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
        SysAuthorizationGrant.resource=='maintenance_summary'))
    row.scope='all_projects';row.version+=1;db.commit()
    result=client.get(path,headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['total']==1
    before=client.get(path,headers=headers,params={'view':'authorization'}).json()['authorization_context']
    precondition={key:before[key] for key in ['catalog_version','authorization_epoch','context']}
    from uuid import uuid4
    db.add(MaintenanceSourceOrderAssignment(assignment_id=str(uuid4()),source_order_id=order.raw_order_id,
        project_id=workspace[2]['other'],created_by='actual-new-assignment-generation'))
    db.commit()
    stale=client.get(path,headers=headers,params={'authorization_precondition':json.dumps(precondition)})
    assert stale.status_code==409,stale.text
    fresh=client.get(path,headers=headers)
    assert fresh.status_code==200,fresh.text
    assert fresh.json()['total']==(0 if kind=='orders' else 1)



def test_project_export_is_independent_scoped_and_does_not_borrow_card_fields(db,board):
    from io import BytesIO
    from openpyxl import load_workbook
    client,headers,actor=board
    grant(db,actor,'maintenance_summary',['read_export_options','export'])
    grant(db,actor,'maintenance_project_cards',['read'],['data_purchase_cost','data_profit'])
    options=client.get('/api/maintenance/boss-board/projects/export/options',headers=headers)
    assert options.status_code==200,options.text
    assert 'contract_amount_inc_tax' not in {field['key'] for field in options.json()['fields']}
    path='/api/maintenance/boss-board/projects/export'
    assert client.post(path,headers=headers,json={'fields':['known_apply_cost_inc_tax']}).status_code==403
    body={'fields':['project_id','project_name']}
    view=client.post(path,headers=headers,json=body,params={'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    data=client.post(path,headers=headers,json=body)
    assert data.status_code==200,data.text
    workbook=load_workbook(BytesIO(data.content),read_only=True)
    try:rows=list(workbook.active.values)
    finally:workbook.close()
    assert len(rows)==4 and 'Secret other' not in str(rows)


def test_project_export_revalidates_relationships_after_render_before_release(db,board,monkeypatch):
    from app.services import maintenance_project_export
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from sqlalchemy import update,func
    client,headers,actor=board;grant(db,actor,'maintenance_summary',['export'])
    original=maintenance_project_export._build_xlsx
    def render_then_revoke(*args,**kwargs):
        result=original(*args,**kwargs)
        db.execute(update(MaintenanceSourceOrderAssignment).where(MaintenanceSourceOrderAssignment.is_active.is_(True))
            .values(is_active=False,archived_at=func.now(),archived_by='export-test',
                    version=MaintenanceSourceOrderAssignment.version+1))
        db.commit()
        return result
    monkeypatch.setattr(maintenance_project_export,'_build_xlsx',render_then_revoke)
    response=client.post('/api/maintenance/boss-board/projects/export',headers=headers,json={'fields':['project_id']})
    assert response.status_code==403,response.text
    assert response.json()['detail']['code']=='AUTHZ_ARTIFACT_STALE'


def test_project_fact_envelopes_never_borrow_global_unlinked_health_counts(db,board,workspace):
    from tests.test_maintenance_source_health import _doc_batch
    from app.services import maintenance_source_health as health
    client,headers,actor=board
    _doc_batch(db,'return_order',project_id=None)
    assert health.source_health(db)['sources']['return_order']['unlinked_rows']==1
    grant(db,actor,'maintenance_project_cards',['read'])
    grant(db,actor,'maintenance_summary',['read_orders'])
    for path in ['/api/maintenance/boss-board/projects',
                 '/api/maintenance/boss-board/projects/'+workspace[2]['manager']+'/orders']:
        sql=[]
        def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
        event.listen(db.get_bind(),'before_cursor_execute',capture)
        try:response=client.get(path,headers=headers)
        finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
        assert response.status_code==200,response.text
        assert response.json()['rows']
        for row in response.json()['rows']:
            envelope=row.get('facts',row)['returned_good_qty']
            assert envelope['state']=='partial' and envelope['unlinked'] is None
        assert not any('count(' in query and 'maintenance_doc_head_row' in query for query in sql), [q for q in sql if 'count(' in q and 'maintenance_doc_head_row' in q]
        assert not any('maintenance_doc_import_batch.uploaded_by' in query or
                       'maintenance_doc_import_batch.head_rows' in query for query in sql),sql
