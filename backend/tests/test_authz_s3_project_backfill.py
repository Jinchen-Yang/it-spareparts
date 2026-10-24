"""Whole-target repair keeps exact input, empty-set admission and atomicity."""
import pytest
from sqlalchemy import event,select,update
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance_project import MaintenanceProject
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401
from tests.test_maintenance_business_type_backfill import _sales_order,_attach

BASE='/api/maintenance/projects/stable/business-type-backfill'


def grant(db,actor,actions):
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_project_backfill',
        actions=actions,scope='all_projects',field_permissions=[],version=1));db.commit()


def pre(payload):
    return {key:payload['authorization_context'][key] for key in ('catalog_version','authorization_epoch','context')}


def test_backfill_preview_and_apply_independent_explicit_actions_all_targets(db,canonical,projects):
    client,headers,ids=canonical;actor,_,_=projects
    _sales_order(db,'XSDD-S3-BACKFILL','整体维保');_attach(db,db.get(MaintenanceProject,ids['manager']),'XSDD-S3-BACKFILL');db.commit()
    assert client.get(BASE+'/preview',headers=headers).status_code==403
    grant(db,actor,['apply'])
    assert client.get(BASE+'/preview',headers=headers).status_code==403
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:view=client.post(BASE+'/apply?view=authorization',headers=headers,json={'reason':'按源事实确认'})
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert view.json()['locator']['object_type']=='operation_input'
    assert not any(q.lstrip().startswith(('insert ','update ','delete ')) for q in sql)
    result=client.post(BASE+'/apply',headers=headers,json={'reason':'按源事实确认','authorization_precondition':pre(view.json())})
    assert result.status_code==200,result.text
    assert result.json()['filled']==1 and result.json()['candidates']==4
    db.expire_all();project=db.get(MaintenanceProject,ids['manager'])
    assert project.business_type=='整体维保' and project.version==2


def test_backfill_plan_input_changes_stale_before_any_update(db,canonical,projects):
    from app.models.sales import FSalesOrder
    client,headers,ids=canonical;actor,_,_=projects
    sales=_sales_order(db,'XSDD-S3-BACKFILL2','整体维保');_attach(db,db.get(MaintenanceProject,ids['manager']),sales.order_no);db.commit()
    grant(db,actor,['apply','preview'])
    preview=client.get(BASE+'/preview',headers=headers)
    assert preview.status_code==200 and preview.json()['fillable']
    view=client.post(BASE+'/apply?view=authorization',headers=headers,json={'reason':'确认计划'})
    db.execute(update(FSalesOrder).where(FSalesOrder.id==sales.id).values(business_type='算力运维'));db.commit()
    result=client.post(BASE+'/apply',headers=headers,json={'reason':'确认计划','authorization_precondition':pre(view.json())})
    assert result.status_code==409,result.text
    assert db.get(MaintenanceProject,ids['manager']).business_type is None


def test_backfill_real_empty_set_still_requires_explicit_apply(db,canonical,projects):
    client,headers,ids=canonical;actor,_,_=projects
    db.execute(update(MaintenanceProject).values(business_type='已有值'));db.commit()
    assert client.post(BASE+'/apply',headers=headers,json={'reason':'真实空集'}).status_code==403
    grant(db,actor,['apply'])
    result=client.post(BASE+'/apply',headers=headers,json={'reason':'真实空集'})
    assert result.status_code==200,result.text
    assert result.json()['candidates']==0 and result.json()['filled']==0
