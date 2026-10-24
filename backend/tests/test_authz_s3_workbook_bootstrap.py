"""Real denied requests must not materialize stored workbook/report contents."""
import pytest
from fastapi import HTTPException
from sqlalchemy import event,select
from app.models.authorization import SysAuthorizationGrant
from tests.test_authz_s3_checklist import setup,_create,cerbos_url  # noqa:F401
from tests.test_authz_s3_monthly_workbooks import setup as monthly_setup  # noqa:F401
from tests.test_authz_s3_project_workbooks import roundtrip  # noqa:F401


def _capture(db,call,table):
    columns=[]
    def observe(_conn,cursor,statement,_params,_context,_many):
        if statement.lstrip().lower().startswith('select') and table in statement.lower():
            columns.extend(column[0] for column in cursor.description or ())
    event.listen(db.get_bind(),'after_cursor_execute',observe)
    try:result=call()
    finally:event.remove(db.get_bind(),'after_cursor_execute',observe)
    assert columns,'expected actual stored carrier SQL'
    return result,columns


def test_checklist_denied_authview_does_not_load_saved_report(db,setup):
    uid,identity,pid,client,headers=setup
    bid=_create(db,identity,pid)
    grant=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==uid))
    grant.actions=['read_checklist'];grant.version+=1;db.commit()
    result,columns=_capture(db,lambda:client.post(
        f'/api/maintenance/acceptance-checklist/{bid}/apply?view=authorization',headers=headers),
        'maintenance_acceptance_checklist_batch')
    assert result.status_code==200,result.text
    assert result.json()['authorization_context']['resources']['maintenance_acceptance']['decisions']['checklist_apply']['effect']=='deny'
    assert not any(c.endswith('report_json') for c in columns),columns


def test_monthly_denied_apply_does_not_load_plan_or_issues(db,monthly_setup):
    from tests.test_authz_s3_monthly_workbooks import MONTH,AS_OF,HMAC_KEY,_set_plan
    from app.services.maintenance_manager_workbook_adapter import (
        read_monthly_workbook,validate_monthly_workbook,MaintenanceManagerWorkbookAdapter)
    uid,identity,_,cid,ctx,factory=monthly_setup
    artifact,_=read_monthly_workbook(factory,identity,MONTH,ctx=ctx,as_of=AS_OF,hmac_key=HMAC_KEY)
    _,values=validate_monthly_workbook(factory,identity,MONTH,_set_plan(artifact.content,cid),ctx=ctx,as_of=AS_OF,hmac_key=HMAC_KEY)
    grant=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==uid,
        SysAuthorizationGrant.resource=='maintenance_manager_workbook'))
    grant.actions=['download'];grant.version+=1;db.commit()
    adapter=MaintenanceManagerWorkbookAdapter(db,user_ctx=ctx,operator=identity.username,as_of=AS_OF,identity=identity)
    def denied():
        with pytest.raises(HTTPException) as error:adapter.apply(values['batch_id'],data_version=values['data_version'])
        assert error.value.status_code==403
    _,columns=_capture(db,denied,'maintenance_manager_upload_batch')
    db.rollback()
    assert not any(c.endswith(('plan_json','issues_json','result_json')) for c in columns),columns


def test_denied_error_artifact_never_loads_error_bytes_or_full_plan(db,roundtrip):
    from tests.test_authz_s3_project_workbooks import HMAC_KEY
    _,_,pid,gid,adapter=roundtrip
    _,_,bid=adapter.validate(pid,b'not an xlsx',hmac_key=HMAC_KEY);db.commit()
    grant=db.get(SysAuthorizationGrant,gid);grant.actions=['validate_v2'];grant.version+=1;db.commit()
    def denied():
        with pytest.raises(HTTPException) as error:adapter.load_error_workbook(bid)
        assert error.value.status_code==403
    _,columns=_capture(db,denied,'maintenance_project_workbook_validation')
    assert not any(c.endswith(('plan_json','issues_json','error_workbook')) for c in columns),columns

from tests.test_authz_s3_master_workbooks import master  # noqa:F401


def test_denied_master_replay_never_loads_complete_audit_result(db,master,monkeypatch):
    from tests.test_authz_s3_master_workbooks import grant
    from app.authz import runtime
    from app.config import get_settings
    uid,pid,client,url=master
    settings=get_settings()
    monkeypatch.setattr(settings,'authz_enabled_resources',settings.authz_enabled_resources+['maintenance_general_workbook_write'])
    runtime.activate_configured_resources(db.get_bind())
    fields=['data_purchase_cost','data_profit','data_customer']
    grant(db,uid,'maintenance_general_workbook',fields,'all_projects')
    write=SysAuthorizationGrant(user_id=uid,resource='maintenance_general_workbook_write',actions=['apply'],scope='all_projects',field_permissions=fields,version=1)
    db.add(write);db.commit()
    download=client.get(url);assert download.status_code==200,download.text
    apply_url=url.removesuffix('.xlsx')+'/apply'
    applied=client.post(apply_url,files={'file':('master.xlsx',download.content)})
    assert applied.status_code==200,applied.text
    write.actions=[];write.version+=1;db.commit()
    result,columns=_capture(db,lambda:client.post(apply_url,files={'file':('master.xlsx',download.content)}),'maintenance_project_operation_audit')
    assert result.status_code==403,result.text
    assert not any(c.endswith(('after_json','before_json')) for c in columns),columns
