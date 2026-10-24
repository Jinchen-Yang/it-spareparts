"""Explicit whole-target business-type repair; no collection-only write allow."""
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from app.authz import service
from app.authz.context import create_context_batch,relation_revision
from app.authz.errors import authz_error
from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.maintenance_project import MaintenanceProject
from app.services import maintenance_authorization as authorization
from app.services import maintenance_business_type_backfill as backfill
from app.services import maintenance_project_operations as operations
from app.services import maintenance_project_catalog as catalog

PREVIEW='GET /api/maintenance/projects/stable/business-type-backfill/preview'
APPLY='POST /api/maintenance/projects/stable/business-type-backfill/apply'
RESOURCE='maintenance_project_backfill'


def _plan(db):
    ids=backfill.candidate_project_ids(db)
    return ids,backfill.derive_by_project(db,ids)


def _resolve(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    ids,derived=_plan(db)
    principal,evidence=authorization.resolve_project_subjects(db,identity,ids,
        read_snapshot=read_snapshot,locked_user=locked_user)
    plan={'reason':params['reason'],'projects':{pid:derived.get(pid,{}) for pid in ids}}
    digest=relation_revision(plan)
    carrier=authorization.slot_ref(APPLY,'context_1',digest,selection={'command_plan_sha256':digest})
    discovery=authorization.slot_ref(APPLY,'context_0','maintenance_business_type_targets',selection={'reason':params['reason']})
    slots={'context_0':(discovery,),'context_1':(carrier,)}
    if ids:slots['targets']=tuple(authorization.slot_ref(APPLY,'targets',pid) for pid in ids)
    context=authorization.resolved_context(db,entry_id=APPLY,branch='default' if ids else 'empty',
        carrier=carrier,slots=slots,subjects={RESOURCE:tuple(evidence[pid].subject for pid in ids)},
        revision=relation_revision({'carrier':carrier.to_dict(),
            'projects':{pid:evidence[pid].authorization_revision for pid in ids},
            'actor':[principal.id,principal.authorization_epoch,principal.salesperson_name]}),empty_target_set=not ids)
    return create_context_batch(db,[context])


authorization.register_entry_resolver(APPLY,_resolve)


def preview(db,identity,*,view=None,precondition=None):
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=PREVIEW,params={}) as (snapshot,proof,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        access=accesses.require(ref,RESOURCE,'preview')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        return {**backfill.preview(snapshot,project_condition=authorization.project_condition(access)),
                'authorization_context':accesses.wire(ref)}


def apply(db,identity,*,reason,view=None,precondition=None):
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    params={'reason':reason}
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=APPLY,params=params) as (_,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        accesses.require(ref,RESOURCE,'apply')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
    try:
        # This is the same outer lock used by source assignment/import writers.
        db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
        ids,_=_plan(db)
        states=operations.lock_workbook_states(db,project_ids=ids)
        rows={row.project_id:row for row in db.scalars(select(MaintenanceProject)
            .where(MaintenanceProject.project_id.in_(ids)).order_by(MaintenanceProject.project_id)
            .with_for_update().execution_options(populate_existing=True))}
        if backfill.candidate_project_ids(db)!=ids:
            raise authz_error(409,'AUTHZ_CONTEXT_STALE','回填候选已变化，请重新确认')
        actor,_=authorization.lock_final_users(db,identity)
        batch=authorization.resolve_entry_context(db,identity,entry_id=APPLY,params=params,locked_user=actor)
        accesses=service.context_accesses(db,identity,batch,locked_user=actor);ref=batch.contexts[0].ref
        accesses.require(ref,RESOURCE,'apply')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        plan=backfill.preview(db)
        filled=0
        for item in plan['fillable']:
            project=rows[item['project_id']]
            catalog.update_project(db,project_id=project.project_id,version=project.version,
                updates={'business_type':item['business_type']},
                reason=f"{reason}｜按销售订单业务类型回填，证据：{'、'.join(item['evidence_order_nos'][:5])}",
                operated_by=actor.username,_prelocked=(project,states[project.project_id]))
            filled+=1
        result={'candidates':plan['candidates'],'filled':filled,'conflicts':len(plan['conflicts']),
                'underivable':len(plan['underivable']),'authorization_context':accesses.wire(ref)}
        db.commit();return result
    except Exception:
        db.rollback();raise
