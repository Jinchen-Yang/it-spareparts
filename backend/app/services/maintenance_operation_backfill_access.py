"""Whole-target repairs: current complete topology, domain locks, then actor.

These commands retain the existing global repair semantics. A restricted grant
must authorize every real target; it never silently turns a global repair into
a partial one. Discovery returns only binding metadata and opaque input hashes.
"""
from collections import defaultdict
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from app.authz import service
from app.authz.context import PolicySubject,create_context_batch,relation_revision
from app.authz.errors import authz_error
from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.maintenance import FProjectExpense,FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject,MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceSiteIssue as Issue,MaintenanceSiteIssueLine as Line,MaintenanceProjectExpenseAttribution as Expense
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as Assignment
from app.services import maintenance_authorization as foundation
from app.services import maintenance_project_operations as business

COST='POST /api/maintenance/projects/stable/site-issue-costs/backfill'
EXPENSE='POST /api/maintenance/projects/stable/expense-attribution/backfill'
RESOURCES={COST:'maintenance_cost_fill',EXPENSE:'maintenance_expense'}


def _digest(db,tables):
    # Static, server-owned table names. Only opaque digests cross the DB boundary.
    return {table:db.scalar(text(f"SELECT encode(sha256(convert_to(COALESCE(string_agg(d, '' ORDER BY d), ''),'UTF8')),'hex') FROM (SELECT encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') d FROM {table} t) fingerprints")) for table in tables}


def _targets(db,entry_id):
    if entry_id==COST:
        rows=list(db.execute(select(Line.issue_line_id,Issue.project_id).join(Issue,Issue.issue_id==Line.issue_id)
            .where(Issue.status_mapping_state=='mapped',Issue.normalized_status.in_(('confirmed','corrected')),Line.is_active.is_(True))
            .order_by(Line.issue_line_id)))
        projects={r.project_id for r in rows};unassigned=[]
        digests=_digest(db,('maintenance_site_issue','maintenance_site_issue_line'))
    else:
        mapping=defaultdict(set)
        def add(key,pid):
            if key:
                mapping[key].add(pid);mapping[key.removeprefix('XSDD-')].add(pid)
        for key,pid in db.execute(select(MaintenanceProjectContract.contract_no,MaintenanceProjectContract.project_id)):add(key,pid)
        for key,pid in db.execute(select(FMaintenanceOrder.linked_sales_order_no,Assignment.project_id)
                .join(Assignment,Assignment.source_order_id==FMaintenanceOrder.raw_order_id).where(Assignment.is_active.is_(True))):add(key,pid)
        rows=list(db.execute(select(FProjectExpense.raw_line_id,FProjectExpense.linked_sales_order_no).order_by(FProjectExpense.raw_line_id)))
        projects=set();unassigned=[]
        for raw_id,key in rows:
            parents=mapping.get(key or '',set());projects.update(parents)
            if not parents:unassigned.append(raw_id)
        projects.update(db.scalars(select(Expense.project_id).where(Expense.raw_expense_line_id.in_([r.raw_line_id for r in rows]))))
        digests=_digest(db,('f_project_expense','maintenance_project_expense_attribution','maintenance_project_contract','maintenance_source_order_assignment'))
    return sorted(projects),unassigned,rows,digests


def _batch(db,principal,entry_id,params):
    resource=RESOURCES[entry_id];projects,unassigned,rows,digests=_targets(db,entry_id)
    evidence=foundation.project_evidence(db,principal,projects)
    discovery=foundation.slot_ref(entry_id,'context_0',
        'maintenance_site_issue_cost_targets' if entry_id==COST else 'maintenance_expense_attribution_targets',selection=params)
    digest=relation_revision({'command':params,'rows':[tuple(r) for r in rows],
        'parents':projects,'unassigned':unassigned,'inputs':digests})
    carrier=foundation.slot_ref(entry_id,'context_1',digest,selection={'command_plan_sha256':digest})
    empty=not rows;branch='empty' if empty else 'default' if projects else 'unassigned'
    slots={'context_0':(discovery,),'context_1':(carrier,)}
    if projects:slots['context_projects']=tuple(foundation.slot_ref(entry_id,'context_projects',pid) for pid in projects)
    subjects=tuple(evidence[p].subject for p in projects)+tuple(PolicySubject(raw_id,{'is_manager':False,'is_viewer':False,'is_sales':False,'is_active':False}) for raw_id in unassigned)
    context=foundation.resolved_context(db,entry_id=entry_id,branch=branch,carrier=carrier,slots=slots,
        subjects={resource:subjects},revision=relation_revision({'input':digest,'actor':[principal.id,principal.authorization_epoch],
            'projects':[evidence[p].authorization_revision for p in projects]}),empty_target_set=empty)
    return create_context_batch(db,[context]),projects


def resolve(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    principal=foundation._named_principal(db,identity,read_snapshot=read_snapshot,locked_user=locked_user)
    return _batch(db,principal,entry_id,params)[0]

for _entry in RESOURCES:foundation.register_entry_resolver(_entry,resolve)


def _lock_domain(db,project_ids,entry_id):
    business.lock_workbook_states(db,project_ids=project_ids)
    list(db.scalars(select(MaintenanceProject).where(MaintenanceProject.project_id.in_(project_ids))
        .order_by(MaintenanceProject.project_id).with_for_update().execution_options(populate_existing=True)))
    if entry_id==COST:
        list(db.scalars(select(Issue).where(Issue.project_id.in_(project_ids)).order_by(Issue.issue_id)
            .with_for_update().execution_options(populate_existing=True)))
        list(db.scalars(select(Line).join(Issue,Issue.issue_id==Line.issue_id).where(Issue.project_id.in_(project_ids))
            .order_by(Line.issue_line_id).with_for_update(of=Line).execution_options(populate_existing=True)))
    else:
        list(db.scalars(select(MaintenanceProjectContract).where(MaintenanceProjectContract.project_id.in_(project_ids))
            .order_by(MaintenanceProjectContract.project_contract_id).with_for_update().execution_options(populate_existing=True)))
        # The existing primitive acquires attribution identities/rows and raw
        # rows next. No actor lock exists until that whole pipeline is flushed.


def execute(db,identity,*,entry_id,params,view=None,precondition=None):
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    resource=RESOURCES[entry_id]
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry_id,params=params) as (_,_,batch,accesses):
        original=batch.contexts[0];ref=original.ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        access=accesses.require(ref,resource,'backfill')
        service.check_context_precondition(foundation.decode_precondition(precondition),accesses,ref)
    try:
        db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
        before,projects=_batch(db,access.principal,entry_id,params)
        _lock_domain(db,projects,entry_id)
        stable,_=_batch(db,access.principal,entry_id,params)
        if stable.contexts[0].authorization_revision!=original.authorization_revision:
            raise authz_error(409,'AUTHZ_CONTEXT_CHANGED','完整回填目标或输入已变化，请重新确认')
        if entry_id==COST:result=business.backfill_site_issue_costs(db,operated_by=access.principal.username,force=params['force'])
        else:result=business.backfill_expense_attribution(db,operated_by=access.principal.username)
        db.flush()
        actor,_=foundation.lock_final_users(db,identity)
        current=resolve(db,identity,entry_id=entry_id,params=params,locked_user=actor)
        final=service.context_accesses(db,identity,current,locked_user=actor)
        final.require(current.contexts[0].ref,resource,'backfill')
        result={**result,'authorization_context':final.wire(current.contexts[0].ref)}
        db.commit();return result
    except Exception:db.rollback();raise
