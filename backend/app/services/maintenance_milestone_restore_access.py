"""Restore exact existing milestone facts under the independent collection grant."""
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.authz import service
from app.authz.context import relation_revision,create_context_batch
from app.authz.errors import authz_error
from app.models.maintenance_manager import MaintenanceCollectionMilestone as Milestone
from app.models.maintenance_project import MaintenanceProjectContract as Contract
from app.services import maintenance_authorization as foundation
from app.services import maintenance_collection_milestone_restore as business

ENTRY='POST /api/maintenance/projects/stable/{project_id}/collection-milestones/restore'
RESOURCE='maintenance_collection'


def resolve(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    principal=foundation._named_principal(db,identity,read_snapshot=read_snapshot,locked_user=locked_user)
    pid=params['project_id'];evidence=foundation.project_evidence(db,principal,[pid])[pid]
    items=json.loads(json.dumps(params['items'],default=str));ids=[x['entity_id'] for x in items]
    milestones=list(db.execute(select(Milestone.milestone_id,Milestone.project_id,Milestone.project_contract_id,
        Milestone.version,Milestone.is_active).where(Milestone.milestone_id.in_(ids)).order_by(Milestone.milestone_id)))
    if {r.milestone_id for r in milestones}!=set(ids) or any(r.project_id!=pid for r in milestones):
        raise authz_error(403,'AUTHZ_FORBIDDEN','回款计划不属于实际项目')
    contracts=list(db.execute(select(Contract.project_contract_id,Contract.project_id,Contract.version,Contract.effective_to)
        .where(Contract.project_contract_id.in_([r.project_contract_id for r in milestones])).order_by(Contract.project_contract_id)))
    if any(r.project_id!=pid for r in contracts):raise authz_error(403,'AUTHZ_FORBIDDEN','回款合同不属于实际项目')
    ref=foundation.slot_ref(ENTRY,'context_0',pid,selection={'reason':params['reason'],'items_plan_sha256':relation_revision(items)})
    slots={'context_0':(ref,),
        'context_milestones':tuple(foundation.slot_ref(ENTRY,'context_milestones',x) for x in sorted(ids)),
        'context_contracts':tuple(foundation.slot_ref(ENTRY,'context_contracts',r.project_contract_id) for r in contracts)}
    context=foundation.resolved_context(db,entry_id=ENTRY,branch='default',carrier=ref,slots=slots,
        subjects={RESOURCE:(evidence.subject,)},revision=relation_revision({'project':evidence.authorization_revision,
            'milestones':[tuple(r) for r in milestones],'contracts':json.loads(json.dumps([tuple(r) for r in contracts],default=str)),
            'carrier':ref.to_dict()}))
    return create_context_batch(db,[context])
foundation.register_entry_resolver(ENTRY,resolve)


class RestoreAuthorization:
    def __init__(self,identity,params,revision,precondition):
        self.identity=identity;self.params=params;self.revision=revision;self.precondition=precondition;self.wire=None
    def authorize_locked(self,db):
        actor,_=foundation.lock_final_users(db,self.identity)
        batch=resolve(db,self.identity,entry_id=ENTRY,params=self.params,locked_user=actor);ref=batch.contexts[0].ref
        accesses=service.context_accesses(db,self.identity,batch,locked_user=actor);accesses.require(ref,RESOURCE,'restore_plan')
        service.check_context_precondition(foundation.decode_precondition(self.precondition),accesses,ref)
        if batch.contexts[0].authorization_revision!=self.revision:raise authz_error(409,'AUTHZ_CONTEXT_CHANGED','回款计划实际版本已变化')
        self.wire=accesses.wire(ref)


def execute(db,identity,*,project_id,command,view=None,precondition=None):
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    params={'project_id':project_id,**command}
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=ENTRY,params=params) as (_,_,batch,accesses):
        context=batch.contexts[0];ref=context.ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        access=accesses.require(ref,RESOURCE,'restore_plan')
        service.check_context_precondition(foundation.decode_precondition(precondition),accesses,ref)
    guard=RestoreAuthorization(identity,params,context.authorization_revision,precondition)
    try:
        result=business.restore_collection_milestones(db,project_id=project_id,
            specs=[business.MilestoneRestoreSpec(**item) for item in command['items']],reason=command['reason'],
            operated_by=access.principal.username,_authorization=guard)
        if guard.wire is None:raise RuntimeError('restore omitted locked authorization')
        result={**result,'authorization_context':guard.wire};db.commit();return result
    except Exception:db.rollback();raise
