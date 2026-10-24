"""Actual dashboard queries respect current grant scope and finance projection."""
from decimal import Decimal
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from app.api import maintenance_dashboard as api
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from tests.test_authz_s3_analysis import analysis_client  # noqa:F401
from tests.test_authz_s3_workspace import workspace,grant  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401

@pytest.fixture
def dashboard(db,analysis_client):
    _,headers,actor=analysis_client
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),headers,actor

@pytest.mark.parametrize('endpoint,action',[
 ('options','read_options'),('summary','read_summary'),('projects','read_projects'),('parts','read_parts'),
 ('receipts','read_receipts'),('quantity-checks','read_quantity_checks'),('orders','read_orders'),
 ('consumption','read_consumption'),('consumption/issues','read_consumption_issues'),
])
def test_independent_dashboard_action_projects_scoped_before_facts(db,dashboard,workspace,endpoint,action):
    client,headers,actor=dashboard;path='/api/maintenance/analytics/maintenance-dashboard/'+endpoint
    assert client.get(path,headers=headers,params={'range':'all'}).status_code==403
    grant(db,actor,'maintenance_dashboard',[action])
    view=client.get(path,headers=headers,params={'range':'all','view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    data=client.get(path,headers=headers,params={'range':'all'})
    assert data.status_code==200,data.text
    assert workspace[2]['other'] not in data.text
    assert 'valuation' not in data.json() and 'bad_estimated_value' not in data.text
    if endpoint=='options':assert len(data.json()['projects'])==4
    if endpoint=='orders':assert Decimal(data.json()['cards']['order_qty'])==2
    hidden=client.get(path,headers=headers,params={'range':'all','project':workspace[2]['other']})
    assert hidden.status_code==200,hidden.text
    if endpoint=='orders':assert Decimal(hidden.json()['cards']['order_qty'])==0


def test_dashboard_ratio_requires_named_action_and_finance_is_not_borrowed(db,dashboard,workspace):
    client,headers,actor=dashboard;base='/api/maintenance/analytics/maintenance-dashboard/'
    grant(db,actor,'maintenance_dashboard',['read_summary','read_projects'])
    grant(db,actor,'maintenance_project',['read'],['data_profit'])
    assert client.get(base+'projects',headers=headers,params={'sort':'bad_value_desc'}).status_code==403
    assert client.get(base+'summary',headers=headers,params={'value_retention_ratio':'0.5'}).status_code==403
    assert client.get(base+'valuation',headers=headers).status_code==403
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,SysAuthorizationGrant.resource=='maintenance_dashboard'))
    row.actions=['read_summary','read_projects','read_valuation','adjust_valuation_parameters'];row.field_permissions=['data_profit'];row.version+=1;db.commit()
    valuation=client.get(base+'valuation',headers=headers,params={'range':'all','value_retention_ratio':'0'})
    assert valuation.status_code==200,valuation.text
    assert 'policy' in valuation.json() and valuation.json()['policy']['retention_ratio']=='0'
    assert client.get(base+'valuation',headers=headers,params={'value_retention_ratio':'NaN'}).status_code==422


@pytest.mark.parametrize('kind,action',[
 ('projects','read_projects'),('parts','read_parts'),('receipts','read_receipts'),
 ('quantity_checks','read_quantity_checks'),('orders','read_orders'),
 ('consumption_daily','read_consumption'),('consumption_issues','read_consumption_issues'),
])
def test_dashboard_export_requires_exact_kind_action_and_hides_financial_columns(db,dashboard,kind,action):
    client,headers,actor=dashboard
    path='/api/maintenance/analytics/maintenance-dashboard/export'
    grant(db,actor,'maintenance_dashboard',['export'])
    assert client.get(path,headers=headers,params={'kind':kind,'range':'all'}).status_code==403
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,SysAuthorizationGrant.resource=='maintenance_dashboard'))
    row.actions=['export',action];row.version+=1;db.commit()
    view=client.get(path,headers=headers,params={'kind':kind,'range':'all','view':'authorization'})
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    result=client.get(path,headers=headers,params={'kind':kind,'range':'all'})
    assert result.status_code==200,result.text
    assert result.headers['cache-control']=='no-store'
    assert 'Secret other' not in result.text and 'S3-other' not in result.text
    assert all(word not in result.text.splitlines()[0] for word in ['金额','成本','估价','价值','覆盖率'])


def test_export_rechecks_after_materialization_before_headers(db,dashboard,monkeypatch):
    from app.api import maintenance_dashboard as transport
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from sqlalchemy import func, update
    client,headers,actor=dashboard
    grant(db,actor,'maintenance_dashboard',['export','read_orders'])
    original=transport._csv_stream
    def render_then_revoke(*args,**kwargs):
        response=original(*args,**kwargs)
        # A domain-parent change does not change the actor epoch or Grant.
        db.execute(update(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.is_active.is_(True)).values(
                is_active=False, version=MaintenanceSourceOrderAssignment.version+1,
                archived_at=func.now(), archived_by='stream-test'))
        db.commit()
        return response
    monkeypatch.setattr(transport,'_csv_stream',render_then_revoke)
    result=client.get('/api/maintenance/analytics/maintenance-dashboard/export',headers=headers,
        params={'kind':'orders','range':'all'})
    assert result.status_code==403,result.text
    assert result.json()['detail']['code']=='AUTHZ_ARTIFACT_STALE'


def test_export_rechecks_each_chunk_and_closes_file(db,dashboard,projects):
    import asyncio
    from io import BytesIO
    from fastapi import HTTPException
    from app.api import maintenance_dashboard as transport
    from app.services import maintenance_dashboard_access as protected
    _,_,actor=dashboard
    grant(db,actor,'maintenance_dashboard',['export','read_orders'])
    prepared=protected.prepare_export(db,projects[1],params={'kind':'orders','range':'all','format':'csv'})
    stream=BytesIO(b'firstsecond')
    response=transport._ClosingStreamingResponse(iter([b'first',b'second']),stream)
    transport._guard_export_stream(response,db.get_bind(),projects[1],prepared.provenance)
    async def consume():
        assert await anext(response.body_iterator)==b'first'
        row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,SysAuthorizationGrant.resource=='maintenance_dashboard'))
        row.actions=[];row.version+=1;db.commit()
        with pytest.raises(HTTPException):await anext(response.body_iterator)
    asyncio.run(consume())
    assert stream.closed
