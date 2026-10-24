"""Cross-author late-lock and immutable receipt read checks with real PostgreSQL/PDP."""
import re

import pytest
from sqlalchemy import select,func,event
from sqlalchemy.orm import Session

from app.models.system import SysUser
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.services import maintenance_milestone_restore_access as restore
from tests.test_authz_s3_master_remaining import client,canonical,projects,cerbos_url  # noqa: F401
from tests.test_maintenance_collection_milestone_restore import _seed,_payload
from tests.test_authz_s3_demands import workflow  # noqa: F401


def test_restore_domain_locks_do_not_hold_actor_and_late_revocation_is_zero_write(db,client,projects,monkeypatch):
    from tests.test_authz_s3_master_remaining import grant
    http,headers=client;actor=projects[0]
    project,contract,milestones=_seed(db);body=_payload(contract,milestones)
    grant(db,actor,'maintenance_collection',['restore_plan'],['data_profit'])
    uid=actor.id;original=restore.RestoreAuthorization.authorize_locked
    def revoke(self,session):
        # This is after the actual project/contract/milestone locks. A second
        # real writer must not be blocked by an early actor SHARE lock.
        with Session(session.get_bind()) as writer:
            from sqlalchemy import text
            writer.execute(text("SET LOCAL lock_timeout='1500ms'"))
            user=writer.get(SysUser,uid,with_for_update=True);user.token_version+=1;writer.commit()
        return original(self,session)
    monkeypatch.setattr(restore.RestoreAuthorization,'authorize_locked',revoke)
    response=http.post(f'/api/maintenance/projects/stable/{project.project_id}/collection-milestones/restore',headers=headers,json=body)
    assert response.status_code==401,response.text
    assert db.scalar(select(func.count()).select_from(MaintenanceCollectionMilestone).where(MaintenanceCollectionMilestone.is_active.is_(True)))==0


def test_actual_demand_history_manifest_read_has_no_write_or_lock_sql(db,workflow):
    from tests.test_authz_s3_demands import command,body,req
    http,uid,pid,_=workflow
    result=command(http,'POST','/void-fast',body())
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:
        response=req(http,'GET',f"/delete-intents/{result['intent_id']}")
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert response.status_code==200,response.text
    assert response.json()['result']['source_order_ids']==['DEMAND-A']
    assert any('repeatable read' in sql and 'read only' in sql for sql in statements)
    assert any('sys_authorization_artifact' in sql for sql in statements)
    assert not any(re.match(r'\s*(insert|update|delete)\b',sql) or 'for update' in sql or 'for share' in sql for sql in statements)


from tests.test_authz_s3_batch_transfer import transfer  # noqa: F401,E402


def test_selected_transfer_authview_never_loads_unselected_full_plan(db,transfer,tmp_path):
    from uuid import uuid4
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from tests.test_authz_s3_batch_transfer_independent import _two_existing_projects
    from tests.test_authz_s3_batch_transfer import body
    from app.services import maintenance_batch_transfer_authorization as protected
    response,ids=_two_existing_projects(db,transfer,tmp_path)
    actor,grant=transfer[2],transfer[3]
    db.add(MaintenanceProjectUserAssignment(assignment_id=str(uuid4()),project_id=ids['A'],
        user_id=actor.id,responsibility_type='viewer',version=1,
        assigned_by='independent',assignment_reason='only selected A authorized'))
    grant.scope='assigned_projects';grant.actions=['apply'];grant.version+=1;db.commit()
    payload=body(response)
    payload['row_keys']=[row['row_key'] for row in response.json()['rows']
        if row['matched_project_id']==ids['A'] and row['row_status']=='ready']
    selected=[]
    def capture(_conn,cursor,statement,_params,_context,_many):
        if statement.lstrip().lower().startswith('select') and 'sys_import_batch' in statement.lower():
            selected.extend(column[0] for column in cursor.description or ())
    event.listen(db.get_bind(),'after_cursor_execute',capture)
    try:
        result=transfer[0].post(protected.BASE+'/apply',params={'view':'authorization'},
            headers=transfer[1],json=payload)
    finally:event.remove(db.get_bind(),'after_cursor_execute',capture)
    assert result.status_code==200,result.text
    assert selected
    assert not any(name.endswith('report_json') for name in selected),selected
    for _ in range(2):
        selected.clear()
        event.listen(db.get_bind(),'after_cursor_execute',capture)
        try:
            applied=transfer[0].post(protected.BASE+'/apply',headers=transfer[1],json=payload)
        finally:event.remove(db.get_bind(),'after_cursor_execute',capture)
        assert applied.status_code==200,applied.text
        assert applied.json()['applied']==1
        assert selected
        assert not any(name.endswith('report_json') for name in selected),selected


def test_transfer_preview_waits_for_project_before_holding_contract_lock(db,transfer,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from pathlib import Path
    from app.models.maintenance_project import MaintenanceProject,MaintenanceProjectContract
    from tests.test_authz_s3_batch_transfer_independent import _two_existing_projects,_preview_files
    from tests.test_maintenance_bulk_import import _ordinary_sales_workbook
    _,ids=_two_existing_projects(db,transfer,tmp_path)
    contract_id=db.scalar(select(MaintenanceProjectContract.project_contract_id).where(
        MaintenanceProjectContract.project_id==ids['A']))
    path=Path(_ordinary_sales_workbook(tmp_path,order_no='XSDD-INDEPENDENT-A',
        raw_order_id='INDEPENDENT-A',project_name='Independent selection A'))
    bind=db.get_bind();db.rollback()
    waiting=Event()
    def before(_conn,_cursor,statement,_params,_context,_many):
        sql=statement.lower()
        if 'for share' in sql and re.search(r'\bfrom maintenance_project\b',sql):waiting.set()
    event.listen(bind,'before_cursor_execute',before)
    lock_error=None
    try:
        with Session(bind) as writer,ThreadPoolExecutor(max_workers=1) as workers:
            writer.execute(select(MaintenanceProject.project_id).where(
                MaintenanceProject.project_id==ids['A']).with_for_update())
            task=workers.submit(_preview_files,transfer,[path])
            try:
                assert waiting.wait(8),'preview did not reach the actual project lock'
                # A normal project→contract domain writer must be able to finish;
                # the waiting preview must not already own the opposite lock.
                try:
                    writer.execute(select(MaintenanceProjectContract.project_contract_id).where(
                        MaintenanceProjectContract.project_contract_id==contract_id).with_for_update(nowait=True))
                except Exception as exc:lock_error=exc
            finally:writer.rollback()
            response=task.result(timeout=10)
            assert response.status_code==200,response.text
    finally:event.remove(bind,'before_cursor_execute',before)
    assert lock_error is None,f'contract held while preview waits on its parent: {lock_error}'
