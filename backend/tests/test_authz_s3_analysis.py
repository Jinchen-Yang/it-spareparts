"""Analysis scopes and fields are actual SQL inputs, not role decisions."""
from datetime import date
from decimal import Decimal
from dataclasses import replace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import update,event,select,false,func
from app.api import maintenance_analytics as api
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from tests.test_authz_s3_workspace import workspace,grant  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401
from tests.test_maintenance_analytics import _line,_part

@pytest.fixture
def analysis_client(db,workspace):
    _,headers,ids,actor=workspace
    for name,qty in [('manager',2),('other',7),('unassigned',11)]:
        project=db.get(MaintenanceProject,ids['manager' if name=='unassigned' else name])
        part=_part(db,'S3-ANALYSIS-'+name)
        line=_line(db,project,part,order_no='WBDD-S3-'+name,order_date=date(2026,1,1),qty=Decimal(qty),cost_inc=Decimal(qty*100))
        order=db.get(FMaintenanceOrder,line.order_id);order.warehouse='warehouse-'+name;order.end_customer='customer-'+name
        if name=='unassigned':db.execute(update(MaintenanceSourceOrderAssignment).where(MaintenanceSourceOrderAssignment.source_order_id==order.raw_order_id).values(is_active=False,version=MaintenanceSourceOrderAssignment.version+1,archived_at=func.now(),archived_by='test'))
    db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),headers,actor

@pytest.mark.parametrize('endpoint,action,params',[
 ('pn-ranking','read_pn_ranking',{'range':'all','sort':'pn'}),('spend-trend','read_spend_trend',{'range':'all'}),
 ('explorer','read_explorer',{'range':'all','metric':'effective'}),('filter-options','read_filter_options',{}),
])
def test_analysis_scope_before_facts_and_all_preserves_unassigned(db,analysis_client,endpoint,action,params):
    client,headers,actor=analysis_client;path='/api/maintenance/analytics/'+endpoint
    assert client.get(path,headers=headers,params=params).status_code==403
    grant(db,actor,'maintenance_analysis',[action])
    queries=[]
    def capture(conn,cursor,statement,parameters,context,executemany):queries.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:scoped=client.get(path,headers=headers,params=params)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert scoped.status_code==200,scoped.text
    payload=scoped.json();assert 'S3-ANALYSIS-other' not in scoped.text
    assert 'warehouse-other' not in scoped.text and 'S3-ANALYSIS-unassigned' not in scoped.text
    if endpoint in ('pn-ranking','explorer'):
        assert payload['total']==1
        assert payload['rows'][0]['missing_lines'] is None
    elif endpoint=='spend-trend':assert Decimal(payload['summary']['qty'])==2
    else:assert payload['warehouses']==['warehouse-manager']
    facts=[q for q in queries if 'maintenance_authorization_relationship_digest' not in q and q.lstrip().startswith(('select','with')) and ('f_maintenance_line' in q or 'maintenance_site_issue_line' in q or 'maintenance_rkd_return_line' in q)]
    if endpoint!='filter-options':assert facts and all('maintenance_authorization_projects' in q for q in facts),facts
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,SysAuthorizationGrant.resource=='maintenance_analysis'))
    row.scope='all_projects';row.version+=1;db.commit()
    all_data=client.get(path,headers=headers,params=params)
    assert all_data.status_code==200,all_data.text
    if endpoint in ('pn-ranking','explorer'):assert all_data.json()['total']==3
    elif endpoint=='spend-trend':assert Decimal(all_data.json()['summary']['qty'])==20
    else:assert all_data.json()['warehouses']==['warehouse-manager','warehouse-other','warehouse-unassigned']
    view=client.get(path,headers=headers,params={**params,'view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert view.json()['locator']=={'collection_ref':'maintenance_analysis_projects'}

@pytest.mark.parametrize('endpoint,action,params',[
 ('pn-ranking','read_pn_ranking',{'sort':'cost_inc'}),('pn-ranking','read_pn_ranking',{'sort':'cost_ex'}),
 ('pn-ranking','read_pn_ranking',{'sort':'cost_share'}),
 ('pn-ranking','read_pn_ranking',{'sort':'missing_lines'}),('pn-ranking','read_pn_ranking',{'cost_source':'missing','sort':'pn'}),
 ('explorer','read_explorer',{'metric':'missing'}),('explorer','read_explorer',{'dimension':'customer'}),
 ('spend-trend','read_spend_trend',{'customer':'customer-other'}),
])
def test_hidden_fields_cannot_filter_rank_or_borrow_other_resource(db,analysis_client,endpoint,action,params):
    client,headers,actor=analysis_client
    grant(db,actor,'maintenance_analysis',[action]);grant(db,actor,'maintenance_project',['read'],['data_customer','data_purchase_cost','data_profit'])
    queries=[]
    def capture(conn,cursor,statement,parameters,context,executemany):queries.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.get('/api/maintenance/analytics/'+endpoint,headers=headers,params=params)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==403,response.text
    assert not any('f_maintenance_line' in q or 'f_maintenance_order' in q for q in queries if 'maintenance_authorization_relationship_digest' not in q)

def test_scope_label_cannot_bypass_conditional_plan(db,analysis_client,projects):
    from app.services import maintenance_authorization as foundation
    from sqlalchemy.orm import Session
    _,_,actor=analysis_client;grant(db,actor,'maintenance_analysis',['read_filter_options'])
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),projects[1],entry_id='GET /api/maintenance/analytics/filter-options',params={}) as (snapshot,_,batch,accesses):
        access=accesses.require(batch.contexts[0].ref,'maintenance_analysis','read_filter_options')
        query=foundation.project_query(replace(access,scope='all_projects',predicate=false()),include_unassigned_when_all=True)
        assert query is not None and list(snapshot.scalars(query))==[]
