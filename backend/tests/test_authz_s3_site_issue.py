"""Real issue HTTP/PDP: independent grants, whole writes and late revocation."""
from datetime import date
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select,func,update,event
from app.api import maintenance_project_operations as api
from app.authz.catalog import get_resource_definition
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceSiteIssue as Issue,MaintenanceSiteIssueLine as Line,MaintenanceSiteIssueCommand as Command
from app.services import maintenance_site_issue_access as protected
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401
from tests.test_authz_s3_finance import _pre
from tests.test_manual_site_issue_api import _part,_create_body
from tests.test_site_issue_v2_api import _delivery_source


@pytest.fixture
def issues(db,canonical,projects):
    actor,identity,ids=projects
    part=_part(db,'S3-SITE-ISSUE');db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api');app.include_router(api.site_issue_router,prefix='/api')
    app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),canonical[1],actor,identity,ids,part


def grant(db,actor,actions,fields=('data_purchase_cost',)):
    row=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,
        SysAuthorizationGrant.resource==protected.RESOURCE))
    if row:row.actions=list(actions);row.field_permissions=list(fields);row.version+=1
    else:
        row=SysAuthorizationGrant(user_id=actor.id,resource=protected.RESOURCE,actions=list(actions),
            field_permissions=list(fields),scope='assigned_or_sales_projects',version=1);db.add(row)
    db.commit();return row


def create(db,issues):
    client,headers,actor,_,ids,part=issues
    path=f"/api/maintenance/site-issues/projects/{ids['manager']}/manual"
    body=_create_body([part],key='s3-manual-create')
    response=client.post(path,headers=headers,json=body)
    assert response.status_code==201,response.text
    return response.json()


def test_manual_zero_write_view_independent_actions_scope_and_read(db,issues):
    client,headers,actor,_,ids,part=issues
    path=f"/api/maintenance/site-issues/projects/{ids['manager']}/manual";body=_create_body([part],key='s3-manual-create')
    assert client.post(path,headers=headers,json=body).status_code==403
    grant(db,actor,['create_manual'])
    view=client.post(path+'?view=authorization',headers=headers,json=body)
    assert view.status_code==201,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(Issue))==0
    response=client.post(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert response.status_code==201,response.text
    assert response.headers['cache-control']=='no-store'
    assert client.post(path.replace(ids['manager'],ids['other']),headers=headers,json={**body,'idempotency_key':'foreign-create'}).status_code==403
    search='/api/maintenance/site-issues/search';params={'project_id':ids['manager']}
    assert client.post(search,headers=headers,json=params).status_code==403
    grant(db,actor,['read_search'])
    read=client.post(search,headers=headers,json=params);assert read.status_code==200,read.text
    assert read.json()['total']==1
    view=client.post(search+'?view=authorization',headers=headers,json=params)
    formal=client.post(search,headers=headers,json={**params,'authorization_precondition':_pre(view.json())})
    assert formal.status_code==200,formal.text
    assert formal.json()['authorization_context']['context']==view.json()['authorization_context']['context']


def test_candidate_reads_do_not_require_cost_or_other_issue_actions(db,issues):
    client,headers,actor,_,ids,_=issues
    grant(db,actor,['read_candidates','read_demand_candidates'],fields=[])
    paths=[('post',f"/api/maintenance/site-issues/projects/{ids['manager']}/candidates/search",{}),
        ('get',f"/api/maintenance/site-issues/projects/{ids['manager']}/demand-candidates",None)]
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:
        for method,path,body in paths:
            response=getattr(client,method)(path,headers=headers,**({'json':body} if body is not None else {}))
            assert response.status_code==200,response.text
            assert response.json()['total']==0
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert not any('.cost_amount' in q or '.unit_cost' in q or '.contract_amount' in q for q in sql)


def test_manual_preview_update_line_header_and_void_each_actual_command(db,issues):
    client,headers,actor,_,ids,part=issues
    grant(db,actor,['preview_manual','create_manual','update_manual','correct_line','correct_header','void'])
    manual_body=_create_body([part],key='s3-manual-preview')
    preview_body={k:v for k,v in manual_body.items() if k not in {'idempotency_key','reason'}}
    preview=client.post('/api/maintenance/site-issues/manual/preview',headers=headers,params={'project_id':ids['manager']},json=preview_body)
    assert preview.status_code==200,preview.text
    assert db.scalar(select(func.count()).select_from(Issue))==0
    created=create(db,issues);issue_id=created['issue_id'];line_id=created['lines'][0]['issue_line_id']
    patch={'project_id':ids['manager'],'version':created['version'],'idempotency_key':'s3-manual-update','receiver':'changed','reason':'explicit patch'}
    updated=client.patch('/api/maintenance/site-issues/manual/'+issue_id,headers=headers,json=patch)
    assert updated.status_code==200,updated.text
    db.expire_all();issue=db.get(Issue,issue_id);line=db.get(Line,line_id)
    linebody={'expected_issue_version':issue.version,'expected_line_version':line.version,'changes':{'no_return':True},'reason':'line proof','idempotency_key':'s3-correct-line'}
    linepath=f'/api/maintenance/site-issues/{issue_id}/lines/{line_id}'
    view=client.patch(linepath+'?view=authorization',headers=headers,json=linebody)
    assert view.status_code==200,view.text
    assert view.json()['locator']=={'object_type':'site_issue','object_id':issue_id}
    changed=client.patch(linepath,headers=headers,json={**linebody,'authorization_precondition':_pre(view.json())})
    assert changed.status_code==200,changed.text
    replay=client.patch(linepath,headers=headers,json=linebody)
    assert replay.status_code==200,replay.text
    assert replay.json()['idempotent_replay']
    db.expire_all();issue=db.get(Issue,issue_id)
    header={'expected_issue_version':issue.version,'changes':{'receiver':'header receiver'},'reason':'whole header','idempotency_key':'s3-header-key'}
    corrected=client.patch(f'/api/maintenance/site-issues/{issue_id}/header',headers=headers,json=header)
    assert corrected.status_code==200,corrected.text
    db.expire_all();issue=db.get(Issue,issue_id)
    void={'project_id':ids['manager'],'version':issue.version,'idempotency_key':'s3-void-issue','reason':'void actual issue'}
    result=client.post(f'/api/maintenance/site-issues/{issue_id}/void',headers=headers,json=void)
    assert result.status_code==200,result.text
    assert result.json()['workflow_status']=='void'


def test_v2_delivery_draft_preview_confirm_update(db,issues):
    client,headers,actor,_,ids,_=issues
    source=_delivery_source(db,project=db.get(MaintenanceProject,ids['manager']),delivery_line_id='s3-delivery-line')
    grant(db,actor,['create','preview','confirm','update'])
    body={'idempotency_key':'s3-delivery-draft','issue_date':'2026-08-09','receiver':'receiver','issued_by':'issuer',
        'site_location':'site','lines':[{'delivery_line_id':source.delivery_line_id,'quantity':'2'}],'reason':'explicit source'}
    created=client.post(f"/api/maintenance/site-issues/projects/{ids['manager']}",headers=headers,json=body)
    assert created.status_code==201,created.text
    issue_id=created.json()['issue_id'];version=created.json()['version']
    preview=client.post(f'/api/maintenance/site-issues/{issue_id}/preview',headers=headers,json={'project_id':ids['manager'],'version':version})
    assert preview.status_code==200,preview.text
    updated=client.patch(f'/api/maintenance/site-issues/{issue_id}',headers=headers,json={'project_id':ids['manager'],
        'version':version,'idempotency_key':'s3-draft-update','receiver':'new receiver','reason':'draft patch'})
    assert updated.status_code==200,updated.text
    confirm=client.post(f'/api/maintenance/site-issues/{issue_id}/confirm',headers=headers,json={'project_id':ids['manager'],
        'version':updated.json()['version'],'idempotency_key':'s3-draft-confirm','reason':'confirm all facts'})
    assert confirm.status_code==200,confirm.text
    assert confirm.json()['workflow_status']=='confirmed'


def test_direct_issue_create_and_status_keep_existing_business_rules(db,issues):
    client,headers,actor,_,ids,part=issues;grant(db,actor,['create','update_status'])
    body={'issue_no':'S3-DIRECT','issue_date':'2026-10-23','raw_status':'已确认','status_mapping_state':'mapped',
        'normalized_status':'confirmed','status_mapping_version':'test-v1','reason':'direct original contract',
        'lines':[{'issue_line_id':'s3-direct-line','line_no':1,'part_id':part.id,'pn':part.pn_std,'quantity':'1'}]}
    created=client.post(f"/api/maintenance/projects/stable/{ids['manager']}/site-issues",headers=headers,json=body)
    assert created.status_code==201,created.text
    issue=created.json()
    result=client.patch(f"/api/maintenance/projects/stable/site-issues/{issue['issue_id']}/status",headers=headers,json={
        'version':issue['version'],'raw_status':'作废','normalized_status':'void','status_mapping_version':'test-v2','reason':'void old fact'})
    assert result.status_code==200,result.text
    assert result.json()['normalized_status']=='void'


def test_cost_dependency_denies_before_fact_sql_and_foreign_line_cannot_alias(db,issues):
    client,headers,actor,_,ids,_=issues
    grant(db,actor,['create_manual','correct_line'])
    made=create(db,issues); issue_id=made['issue_id']; line_id=made['lines'][0]['issue_line_id']
    body={'expected_issue_version':made['version'],'expected_line_version':1,
        'changes':{'no_return':True},'reason':'foreign child','idempotency_key':'foreign-child-key'}
    assert client.patch(f'/api/maintenance/site-issues/does-not-exist/lines/{line_id}',headers=headers,json=body).status_code==404
    grant(db,actor,['read_search'],fields=[])
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:response=client.post('/api/maintenance/site-issues/search',headers=headers,json={'project_id':ids['manager']})
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==403,response.text
    assert not any('.cost_amount' in q or '.unit_cost' in q for q in sql)


def test_late_epoch_revocation_rolls_back_all_issue_and_receipt_effects(db,issues,monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    from app.models.system import SysUser
    client,headers,actor,_,ids,part=issues;grant(db,actor,['create_manual'])
    action,real=protected.WRITES[protected.MANUAL_CREATE]
    def revoke_after_business(session,**kwargs):
        result=real(session,**kwargs)
        session.flush()
        with Session(session.get_bind()) as writer:
            writer.execute(text("SET LOCAL lock_timeout='1500ms'"))
            writer.execute(update(SysUser).where(SysUser.id==actor.id).values(token_version=actor.token_version+1))
            writer.commit()
        return result
    monkeypatch.setitem(protected.WRITES,protected.MANUAL_CREATE,(action,revoke_after_business))
    result=client.post(f"/api/maintenance/site-issues/projects/{ids['manager']}/manual",headers=headers,
        json=_create_body([part],key='late-revoke-manual'))
    assert result.status_code==401,result.text
    assert db.scalar(select(func.count()).select_from(Issue))==0
    assert db.scalar(select(func.count()).select_from(Line))==0
    assert db.scalar(select(func.count()).select_from(Command))==0


def test_changed_payload_and_domain_generation_invalidate_precondition(db,issues):
    client,headers,actor,_,ids,part=issues;grant(db,actor,['create_manual'])
    path=f"/api/maintenance/site-issues/projects/{ids['manager']}/manual";body=_create_body([part],key='stale-view-manual')
    view=client.post(path+'?view=authorization',headers=headers,json=body)
    changed=client.post(path,headers=headers,json={**body,'receiver':'another receiver','authorization_precondition':_pre(view.json())})
    assert changed.status_code==409,changed.text
    db.execute(update(MaintenanceProject).where(MaintenanceProject.project_id==ids['manager']).values(version=2));db.commit()
    stale=client.post(path,headers=headers,json={**body,'authorization_precondition':_pre(view.json())})
    assert stale.status_code==409,stale.text
    assert db.scalar(select(func.count()).select_from(Issue))==0
