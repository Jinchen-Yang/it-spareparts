"""Explicit global diagnostic contracts, isolated from legacy role decisions."""
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.errors import authz_error
from app.business_time import business_today
from app.services import maintenance_authorization as authorization
from app.services import maintenance_match_audit
from app.services import maintenance_expense_reconcile


def _read(db,identity,*,entry,resource,params,view,precondition,project):
    if view not in (None,'authorization'):
        raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知的授权视图')
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=entry,params=params) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':
            return service.context_resolution(accesses, ref)
        accesses.require(ref,resource,'read')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        return {**project(snapshot),'authorization_context':accesses.wire(ref)}


def read_business_date(db,identity,*,view=None,precondition=None):
    return _read(db,identity,entry='GET /api/maintenance/as-of',resource='maintenance_business_date',
        params={},view=view,precondition=precondition,project=lambda snapshot:{'as_of':business_today().isoformat()})


def read_match_audit(db,identity,*,sample_limit=5,view=None,precondition=None):
    return _read(db,identity,entry='GET /api/maintenance/match-audit',resource='maintenance_audit',
        params={'sample_limit':sample_limit},view=view,precondition=precondition,
        project=lambda snapshot:maintenance_match_audit.build_report(snapshot,sample_limit=sample_limit))


def read_expense_reconcile(db,identity,*,limit=500,offset=0,view=None,precondition=None):
    def payload(snapshot):
        rows=maintenance_expense_reconcile.expense_reconcile_rows(snapshot,limit=limit,offset=offset)
        return {'rows':rows,'limit':limit,'offset':offset,**{status:sum(row['status']==status for row in rows)
            for status in ('matched','mismatch','unresolved','ledger_only','bxd_only','formal_only')}}
    return _read(db,identity,entry='GET /api/maintenance/reconcile/expenses',
        resource='maintenance_expense_reconcile',params={},view=view,precondition=precondition,project=payload)
