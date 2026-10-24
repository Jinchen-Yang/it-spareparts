"""Current source/project topology and stable whole-confirmation receipts."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import ObjectRef, create_context_batch, relation_revision
from app.authz.errors import authz_error
from app.models.system import SysImportBatch
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_authorization as foundation
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_source_cancel_cascade as business
from app.services.maintenance_workflow_authorization import require_complete

BASE='/api/maintenance/projects/{project_id}/source-cancellations'
LIST='GET '+BASE
DETAIL=LIST+'/{batch_id}/{source_order_id}'
STATUS=DETAIL+'/confirmation-status'
CONFIRM='POST '+BASE+'/{batch_id}/{source_order_id}/confirm'
KIND='source_cancel_receipt'


def _selection(params):
    return {'project_id':params['project_id'],'batch_id':str(params['batch_id']),
            'source_order_id':params['source_order_id']}


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None, _principal=None):
    pid=params['project_id']
    if _principal is None:
        principal, projects=foundation.resolve_project_subjects(db,identity,[pid],read_snapshot=read_snapshot,locked_user=locked_user)
    else:
        principal=_principal;projects=foundation.project_evidence(db,principal,[pid])
    revision={'project':projects[pid].authorization_revision,'actor':[principal.id,principal.authorization_epoch]}
    if entry_id==LIST:
        ref=foundation.slot_ref(entry_id,'context_0',pid)
    else:
        selection=_selection(params)
        row=db.execute(select(FMaintenanceOrder.id,FMaintenanceOrder.raw_order_id,FMaintenanceOrder.import_batch_id,FMaintenanceOrder.data_status).where(FMaintenanceOrder.raw_order_id==params['source_order_id'])).first()
        upload=db.execute(select(SysImportBatch.id,SysImportBatch.file_hash,SysImportBatch.status).where(SysImportBatch.id==params['batch_id'])).first()
        assignments=list(db.execute(select(MaintenanceSourceOrderAssignment.assignment_id,MaintenanceSourceOrderAssignment.project_id,
            MaintenanceSourceOrderAssignment.version,MaintenanceSourceOrderAssignment.is_active).where(
            MaintenanceSourceOrderAssignment.source_order_id==params['source_order_id']).order_by(MaintenanceSourceOrderAssignment.assignment_id)
            .execution_options(populate_existing=True)))
        active=[r for r in assignments if r.is_active]
        if row is None or upload is None or not any(r.project_id==pid for r in assignments):
            raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','来源取消对象不存在或不属于该项目')
        if active and (len(active)!=1 or active[0].project_id!=pid):
            raise authz_error(409,'AUTHZ_CONTEXT_STALE','来源单当前归属已变化')
        ref=foundation.slot_ref(entry_id,'context_0',relation_revision(selection),selection=selection)
        revision.update(source=[row.id,row.raw_order_id,row.import_batch_id,row.data_status],
            batch=[upload.id,upload.file_hash,upload.status],
            assignments=[[r.assignment_id,r.project_id,r.version,r.is_active] for r in assignments],
            command={k:params[k] for k in ('expected_digest','reason','idempotency_key') if k in params})
    return create_context_batch(db,[foundation.resolved_context(db,entry_id=entry_id,branch='default',
        carrier=ref,slots={'context_0':(ref,)},subjects={'maintenance_source_cancellation':(projects[pid].subject,)},
        revision=relation_revision(revision))])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    from app.authz.context import plain
    selection=plain(ref.selector)['selection']
    return _resolve(db,identity,entry_id=entry_id,params={**selection,'batch_id':int(selection['batch_id'])},
                    read_snapshot=read_snapshot,locked_user=locked_user)


for _entry in (LIST,DETAIL,STATUS,CONFIRM):
    foundation.register_entry_resolver(_entry,_resolve,reference_resolver=None if _entry==LIST else _reference)


def _key(actor_id, client_key):
    return business._digest([f'authz-user:{actor_id}',client_key])


def _receipt(db, target):
    try:
        batch_id,key=target.id.split(':',1)
        if str(int(batch_id))!=batch_id or len(key)!=64 or target.generation!=1:
            raise ValueError()
    except (ValueError,TypeError):
        raise artifacts.unavailable()
    batch=db.get(SysImportBatch,int(batch_id),populate_existing=True)
    record=((batch.report_json or {}).get(business.RESULTS_KEY) or {}).get(key) if batch else None
    if not isinstance(record,dict) or type(record.get('owner_user_id')) is not int:
        raise artifacts.unavailable()
    result=record.get('result')
    if not isinstance(result,dict) or result.get('batch_id')!=batch.id:
        raise artifacts.unavailable()
    selection={'project_id':record['project_id'],'batch_id':batch_id,'source_order_id':result['source_order_id']}
    ref=foundation.slot_ref(CONFIRM,'context_0',relation_revision(selection),selection=selection)
    return record,ref


def _state(db,target):
    record,ref=_receipt(db,target)
    return artifacts.ArtifactDomainState(target,ref,relation_revision(record),record['owner_user_id'])


artifacts.register_artifact_domain(KIND,artifacts.ArtifactDomainAdapter(_state,None,(),private=False))


def _target(batch_id, actor_id, client_key):
    return artifacts.ArtifactTarget(KIND,f'{batch_id}:{_key(actor_id,client_key)}',1)


def read(db,identity,*,entry_id,params,authorization_view=False,precondition=None):
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry_id,params=params) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses,ref)
        require_complete(accesses);service.check_context_precondition(precondition,accesses,ref)
        if entry_id==LIST:
            result=business.list_candidates(snapshot,project_id=params['project_id'],page=params['page'],page_size=params['page_size'])
        elif entry_id==DETAIL:
            result=business.candidate_detail(snapshot,**{k:params[k] for k in ('project_id','batch_id','source_order_id')})
        else:
            result=business.confirmation_status(snapshot,**{k:params[k] for k in ('project_id','batch_id','source_order_id','idempotency_key')},
                operated_by=f'authz-user:{accesses.principal.id}')
            if result['result'] is not None:
                target=_target(params['batch_id'],accesses.principal.id,params['idempotency_key'])
                artifacts.load_artifact_binding(snapshot,target=target)
        return {**result,'authorization_context':accesses.wire(ref)}


class SourceCancellationAuthorization:
    def __init__(self,db,identity,params,principal,precondition):
        self.db,self.identity,self.params,self.principal,self.precondition=db,identity,params,principal,precondition
        self.transaction=db.get_transaction();self.captured=None

    @property
    def operator_identity(self):
        return f'authz-user:{self.principal.id}'

    def capture(self,db):
        if db is not self.db or db.get_transaction() is not self.transaction:
            raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','来源取消事务已变化')
        self.captured=_resolve(db,self.identity,entry_id=CONFIRM,params=self.params,_principal=self.principal)

    def finish(self,db,*,replayed):
        if self.captured is None or db is not self.db or db.get_transaction() is not self.transaction:
            raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','来源取消缺少实际完整命令')
        actor=service.lock_actor(db,self.identity)
        accesses=service.context_accesses(db,self.identity,self.captured,locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(self.precondition,accesses,self.captured.contexts[0].ref)
        target=_target(self.params['batch_id'],actor.id,self.params['idempotency_key'])
        if replayed:
            artifacts.load_artifact_binding(db,target=target)
        else:
            state=_state(db,target)
            artifacts.register_artifact_binding(db,self.identity,target=target,input_digest=state.input_digest,locked_user=actor)


def confirm(db,identity,*,params,authorization_view=False,precondition=None):
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=CONFIRM,params=params) as (_,_,batch,accesses):
        ref=batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses,ref)
        require_complete(accesses);service.check_context_precondition(precondition,accesses,ref)
        principal=accesses.principal
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        if not db.in_transaction():
            db.begin()
        authorization=SourceCancellationAuthorization(db,identity,params,principal,precondition)
        result=business.confirm_cancellation(db,**params,operated_by=principal.username,reauthorize=None,
                                            _authorization=authorization)
        authorization.finish(db,replayed=result['replayed'])
        db.commit()
        return result
    except BaseException:
        db.rollback();raise
