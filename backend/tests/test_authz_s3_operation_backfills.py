"""Whole global repairs: exact targets, empty admission and atomic late revocation."""
from datetime import date
from decimal import Decimal
import json
import pytest
from sqlalchemy import select,func,update,text
from sqlalchemy.orm import Session
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceSiteIssue as Issue,MaintenanceProjectExpenseAttribution as Expense
from app.models.system import SysImportBatch,SysUser
from app.services import maintenance_operation_backfill_access as protected
from tests.test_authz_s3_site_issue import issues,grant as issue_grant,create
from tests.test_authz_s3_projects import canonical
from tests.test_authz_s3_project_foundation import projects
from tests.authz_pdp_helpers import cerbos_url
from tests.test_authz_s3_finance import _pre
from app.api import maintenance_project_operations as api


def grant(db,actor,resource,scope='all_projects'):
    db.add(SysAuthorizationGrant(user_id=actor.id,resource=resource,actions=['backfill'],scope=scope,field_permissions=[],version=1));db.commit()


def client(issues):
    c=issues[0];c.app.include_router(api.operations_beta_router,prefix='/api');return c


def raw(db,ids,*,unassigned=False):
    batch=SysImportBatch(filename='backfill-test.xls',file_type='maintenance',file_hash='8'*64);db.add(batch);db.flush()
    if not unassigned:
        db.add(MaintenanceProjectContract(project_contract_id='backfill-contract',project_id=ids['manager'],contract_no='XSDD-BACKFILL',
            contract_id='backfill-contract',contract_amount=Decimal('100'),amount_inc_tax=Decimal('113'),status_mapping_state='mapped',status_mapping_version='test',included_in_total=True,effective_from=date(2026,1,1),source='test'))
    row=FProjectExpense(raw_line_id='backfill#1',bxd_no='BXD-20261023-0001',line_no=1,data_status='已结束',expense_date=date(2026,10,23),
        linked_sales_order_no='XSDD-NONE' if unassigned else 'XSDD-BACKFILL',amount=Decimal('10'),amount_ex_tax=Decimal('10'),
        amount_inc_tax=Decimal('11.30'),import_batch_id=batch.id)
    db.add(row);db.commit();return row


@pytest.mark.parametrize('entry,resource',[(protected.COST,'maintenance_cost_fill'),(protected.EXPENSE,'maintenance_expense')])
def test_true_empty_requires_explicit_grant(db,issues,entry,resource):
    c=client(issues);headers=issues[1];actor=issues[2];path=entry.split(' ',1)[1]
    assert c.post(path,headers=headers).status_code==403
    grant(db,actor,resource)
    view=c.post(path,headers=headers,params={'view':'authorization'});assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    result=c.post(path,headers=headers,params={'authorization_precondition':json.dumps(_pre(view.json()))})
    assert result.status_code==200,result.text
    assert result.json()['total']==0


def test_cost_requires_all_real_targets(db,issues):
    c=client(issues);headers,actor,identity,ids,part=issues[1:]
    issue_grant(db,actor,['create_manual']);made=create(db,issues)
    grant(db,actor,'maintenance_cost_fill','assigned_or_sales_projects')
    path=protected.COST.split(' ',1)[1];view=c.post(path,headers=headers,params={'view':'authorization'})
    assert view.status_code==200,view.text
    db.execute(update(Issue).where(Issue.issue_id==made['issue_id']).values(project_id=ids['other'],version=2));db.commit()
    denied=c.post(path,headers=headers);assert denied.status_code==403,denied.text
    db.execute(update(Issue).where(Issue.issue_id==made['issue_id']).values(project_id=ids['manager'],version=3));db.commit()
    result=c.post(path,headers=headers);assert result.status_code==200,result.text
    assert result.json()['total']==1


def test_expense_no_parent_requires_all_and_input_precondition(db,issues):
    c=client(issues);headers,actor,identity,ids,part=issues[1:];row=raw(db,ids,unassigned=True)
    grant(db,actor,'maintenance_expense','assigned_or_sales_projects');path=protected.EXPENSE.split(' ',1)[1]
    denied=c.post(path,headers=headers);assert denied.status_code==403,denied.text
    db.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor.id,SysAuthorizationGrant.resource=='maintenance_expense').values(scope='all_projects',version=2));db.commit()
    view=c.post(path,headers=headers,params={'view':'authorization'});assert view.status_code==200,view.text
    db.execute(update(FProjectExpense).where(FProjectExpense.id==row.id).values(reason='new input'));db.commit()
    stale=c.post(path,headers=headers,params={'authorization_precondition':json.dumps(_pre(view.json()))});assert stale.status_code==409,stale.text
    result=c.post(path,headers=headers);assert result.status_code==200,result.text
    assert result.json()['total']==1 and result.json()['skipped_no_contract']==1


def test_expense_late_revocation_rolls_back_real_attribution(db,issues,monkeypatch):
    c=client(issues);headers,actor,identity,ids,part=issues[1:];raw(db,ids)
    grant(db,actor,'maintenance_expense');real=protected.business.backfill_expense_attribution
    def revoke(session,**kwargs):
        result=real(session,**kwargs);session.flush()
        assert session.scalar(select(func.count()).select_from(Expense))==1
        with Session(session.get_bind()) as writer:
            writer.execute(text("SET LOCAL lock_timeout='1500ms'"))
            writer.execute(update(SysUser).where(SysUser.id==actor.id).values(token_version=actor.token_version+1));writer.commit()
        return result
    monkeypatch.setattr(protected.business,'backfill_expense_attribution',revoke)
    result=c.post(protected.EXPENSE.split(' ',1)[1],headers=headers)
    assert result.status_code==401,result.text
    assert db.scalar(select(func.count()).select_from(Expense))==0
