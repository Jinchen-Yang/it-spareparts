"""WBDD uploads and reconciliation over immutable input and live project scope."""
from sqlalchemy import select, func, Text
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, relation_revision
from app.authz.errors import authz_error
from app.models.maintenance import FMaintenanceOrder, FMaintenanceLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
from app.models.system import SysImportBatch
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_wbdd_import as business
from app.services.maintenance_workflow_authorization import require_complete

BASE='/api/maintenance/wbdd-imports'
UPLOAD='POST '+BASE
HEALTH='GET '+BASE+'/latest'
MISSING='GET '+BASE+'/latest/missing'
RECONCILE='POST '+BASE+'/{batch_id}/reconcile'
KIND='wbdd_import_batch'
SYNC_KIND='wbdd_reconciliation_receipt'


def _upload_context(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    principal,_=foundation.resolve_project_subjects(db,identity,[],read_snapshot=read_snapshot,locked_user=locked_user)
    selection={'input_sha256':params['input_sha256'],'doc_type':'wbdd'}
    revision=relation_revision({'actor':[principal.id,principal.authorization_epoch],'selection':selection})
    ref=foundation.slot_ref(UPLOAD,'context_0',revision,selection=selection)
    return create_context_batch(db,[foundation.resolved_context(db,entry_id=UPLOAD,branch='default',carrier=ref,
        slots={'context_0':(ref,)},subjects={'maintenance_wbdd_snapshot':(PolicySubject(ref.id,{}),)},revision=revision)])


foundation.register_entry_resolver(UPLOAD,_upload_context)


def _upload_state(db,target):
    if target.generation!=1 or not target.id.isdecimal():raise artifacts.unavailable()
    receipt=db.scalar(select(MaintenanceWbddImportReceipt).where(MaintenanceWbddImportReceipt.batch_id==int(target.id))
        .execution_options(populate_existing=True))
    batch=db.get(SysImportBatch,int(target.id),populate_existing=True)
    if receipt is None or receipt.owner_user_id is None or batch is None or batch.owner_user_id!=receipt.owner_user_id:
        raise artifacts.unavailable()
    digest=relation_revision({'owner':receipt.owner_user_id,'batch':batch.id,'file_hash':batch.file_hash,
        'receipt_file':receipt.file_hash,'layout':receipt.layout,'report':receipt.report_json,
        'snapshot':(batch.report_json or {}).get('_wbdd_source_snapshot'),
        'status_snapshot':(batch.report_json or {}).get('_maintenance_source_status_snapshot')})
    return artifacts.ArtifactDomainState(target,ObjectRef(KIND,target.id),digest,receipt.owner_user_id)


artifacts.register_artifact_domain(KIND,artifacts.ArtifactDomainAdapter(_upload_state,None,(),private=False))


class WbddUploadAuthorization:
    def __init__(self,db,identity,params,principal,precondition):
        self.db,self.transaction=db,db.get_transaction()
        self.identity,self.params,self.principal,self.precondition=identity,params,principal,precondition

    def finish(self,db,receipt,*,replayed):
        if db is not self.db or db.get_transaction() is not self.transaction:
            raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','上传事务已变化')
        actor=service.lock_actor(db,self.identity)
        context=_upload_context(db,self.identity,entry_id=UPLOAD,params=self.params,locked_user=actor)
        access=service.context_accesses(db,self.identity,context,locked_user=actor)
        require_complete(access);service.check_context_precondition(self.precondition,access,context.contexts[0].ref)
        target=artifacts.ArtifactTarget(KIND,str(receipt.batch_id),1)
        if replayed:artifacts.load_artifact_binding(db,target=target)
        else:
            state=_upload_state(db,target)
            artifacts.register_artifact_binding(db,self.identity,target=target,input_digest=state.input_digest,
                operation=artifacts.IdempotentOperation('wbdd_upload',receipt.idempotency_key),locked_user=actor)


def upload(db,identity,*,file_path,original_name,idempotency_key,authorization_view=False,precondition=None):
    from app.etl.pipeline import sha256_file
    business.precheck_wbdd_file(file_path)
    params={'input_sha256':sha256_file(file_path)}
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=UPLOAD,params=params) as (_,_,batch,access):
        ref=batch.contexts[0].ref
        if authorization_view:return service.context_resolution(access,ref)
        require_complete(access);service.check_context_precondition(precondition,access,ref)
        principal=access.principal
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        if not db.in_transaction():db.begin()
        permit=WbddUploadAuthorization(db,identity,params,principal,precondition)
        report,replayed=business.import_wbdd(db,file_path=file_path,original_name=original_name,
            operator=principal.username,idempotency_key=idempotency_key,_authorization=permit)
        db.commit()
        return {**report,'replayed':replayed}
    except BaseException:
        db.rollback();raise


def _allowed_projects(db,access):
    # The predicate is the PDP's actual SQL plan, never inferred from a scope name.
    from sqlalchemy.sql.elements import True_
    if isinstance(access.predicate,True_):return None
    return set(db.scalars(select(MaintenanceProject.project_id).where(foundation.project_condition(access))))


def read(db,identity,*,entry_id,authorization_view=False,precondition=None):
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry_id,params={}) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if authorization_view:return service.context_resolution(accesses,ref)
        require_complete(accesses);service.check_context_precondition(precondition,accesses,ref)
        if entry_id==HEALTH:result=business.latest_health(snapshot)
        else:
            access=accesses.require(ref,'maintenance_wbdd_reconciliation','read_missing')
            result=business.latest_missing(snapshot,allowed_project_ids=_allowed_projects(snapshot,access))
        return {**result,'authorization_context':accesses.wire(ref)}


def _targets(db,params):
    source_ids=set(params['source_order_ids']);line_ids=set(params['line_ids'])
    if not source_ids and not line_ids:raise authz_error(422,'AUTHZ_INVALID_REQUEST','必须选择真实来源或明细')
    lines=list(db.execute(select(FMaintenanceLine.id,FMaintenanceLine.order_id,FMaintenanceLine.raw_line_id,FMaintenanceLine.is_active).where(FMaintenanceLine.id.in_(line_ids)).execution_options(populate_existing=True)))
    if {line.id for line in lines}!=line_ids:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','明细不存在')
    orders=list(db.execute(select(FMaintenanceOrder.id,FMaintenanceOrder.raw_order_id,FMaintenanceOrder.import_batch_id,FMaintenanceOrder.data_status).where((FMaintenanceOrder.raw_order_id.in_(source_ids))|
        FMaintenanceOrder.id.in_([line.order_id for line in lines])).execution_options(populate_existing=True)))
    if not source_ids<={order.raw_order_id for order in orders}:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','来源单不存在')
    assignments=list(db.execute(select(MaintenanceSourceOrderAssignment.assignment_id,MaintenanceSourceOrderAssignment.source_order_id,
        MaintenanceSourceOrderAssignment.project_id,MaintenanceSourceOrderAssignment.version).where(
        MaintenanceSourceOrderAssignment.source_order_id.in_([order.raw_order_id for order in orders]),
        MaintenanceSourceOrderAssignment.is_active.is_(True)).order_by(MaintenanceSourceOrderAssignment.assignment_id)
        .execution_options(populate_existing=True)))
    return orders,lines,assignments


def _sync_context(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None,_principal=None):
    if branch == 'discovery':
        principal,_=foundation.resolve_project_subjects(db,identity,[],read_snapshot=read_snapshot,locked_user=locked_user)
        ref=foundation.slot_ref(RECONCILE,'context_discovery','wbdd_latest_missing',
            selection={k:params[k] for k in ('source_order_ids','line_ids')})
        return create_context_batch(db,[foundation.resolved_context(db,entry_id=RECONCILE,branch='discovery',
            carrier=ref,slots={'context_discovery':(ref,)},subjects={'maintenance_wbdd_reconciliation':()},
            revision=foundation.project_collection_revision(db,principal,ref))])
    orders,lines,assignments=_targets(db,params)
    if _principal is None:
        principal,_=foundation.resolve_project_subjects(db,identity,[],read_snapshot=read_snapshot,locked_user=locked_user)
    else:principal=_principal
    from app.services.maintenance_wbdd_reconciliation import _digest
    replay_key=_digest([f'authz-user:{principal.id}',params['idempotency_key']])
    report=SysImportBatch.report_json
    prior=report['_wbdd_sync_results'][replay_key]
    # Bootstrap returns relationship keys and opaque digests only; the full
    # saved result remains inside PostgreSQL until current scope is authorized.
    batch=db.execute(select(SysImportBatch.id,SysImportBatch.file_type,
        _sql_digest(report['_wbdd_source_snapshot']).label('snapshot_digest'),
        func.jsonb_typeof(prior).label('prior_type'),
        prior['authorization_parent_ids'].label('parent_ids'),
        prior['authorization_source_projects'].label('source_projects'))
        .where(SysImportBatch.id==params['batch_id'])).first()
    if batch is None or batch.file_type!='maintenance':raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','来源批次不存在')
    historical_parents=set()
    historical_sources={}
    if batch.prior_type is not None:
        if (batch.prior_type!='object' or type(batch.parent_ids) is not list
                or any(type(pid) is not str for pid in batch.parent_ids)
                or type(batch.source_projects) is not dict
                or any(type(source) is not str or type(parents) is not list
                    or any(type(pid) is not str for pid in parents)
                    for source,parents in batch.source_projects.items())):
            raise artifacts.unavailable()
        artifacts.load_artifact_binding(db,target=artifacts.ArtifactTarget(SYNC_KIND,str(batch.id)+':'+replay_key,1))
        historical_parents=set(batch.parent_ids)
        historical_sources=batch.source_projects
    parent_ids=sorted({a.project_id for a in assignments}|historical_parents)
    projects=foundation.project_evidence(db,principal,parent_ids)
    ref=foundation.slot_ref(RECONCILE,'context_0',str(batch.id),selection={k:params[k] for k in ('source_order_ids','line_ids')})
    slots={'context_0':(ref,)}
    if parent_ids:slots['context_1']=tuple(foundation.slot_ref(RECONCILE,'context_1',pid) for pid in parent_ids)
    subjects=[projects[pid].subject for pid in parent_ids]
    assigned={a.source_order_id for a in assignments}
    subjects.extend(PolicySubject(order.raw_order_id,{'is_manager':False,'is_viewer':False,'is_sales':False,'is_active':False})
                    for order in orders if order.raw_order_id not in assigned and not historical_sources.get(order.raw_order_id))
    revision=relation_revision({'command':params,'actor':[principal.id,principal.authorization_epoch],
        'snapshot':batch.snapshot_digest,
        'projects':{pid:value.authorization_revision for pid,value in projects.items()},
        'assignments':[[a.assignment_id,a.source_order_id,a.project_id,a.version] for a in assignments],
        'orders':[[o.id,o.raw_order_id,o.import_batch_id,o.data_status] for o in orders],
        'lines':[[line.id,line.order_id,line.raw_line_id,line.is_active] for line in lines]})
    return create_context_batch(db,[foundation.resolved_context(db,entry_id=RECONCILE,branch='default' if parent_ids else 'unassigned',
        carrier=ref,slots=slots,subjects={'maintenance_wbdd_reconciliation':tuple(subjects)},revision=revision)])


foundation.register_entry_resolver(RECONCILE,_sync_context)


def _sql_digest(value):
    return func.encode(func.sha256(func.convert_to(value.cast(Text),'UTF8')),'hex')


def _sync_state(db,target):
    try:bid,key=target.id.split(':',1);batch_id=int(bid)
    except (ValueError,TypeError):raise artifacts.unavailable()
    record=SysImportBatch.report_json['_wbdd_sync_results'][key]
    state=db.execute(select(func.jsonb_typeof(record).label('record_type'),
        record['owner_user_id'].label('owner_id'),_sql_digest(record).label('digest'))
        .where(SysImportBatch.id==batch_id)).first()
    if target.generation!=1 or state is None or state.record_type!='object' or type(state.owner_id) is not int:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(target,ObjectRef(KIND,bid),state.digest,state.owner_id)


artifacts.register_artifact_domain(SYNC_KIND,artifacts.ArtifactDomainAdapter(_sync_state,None,(),private=False))


class WbddReconciliationAuthorization:
    def __init__(self,db,identity,params,principal,precondition):
        self.db,self.transaction=db,db.get_transaction()
        self.identity,self.params,self.principal,self.precondition=identity,params,principal,precondition
        self.captured=None

    @property
    def operator_identity(self):return f'authz-user:{self.principal.id}'

    def capture(self,db):
        if db is not self.db or db.get_transaction() is not self.transaction:raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','核对事务已变化')
        self.captured=_sync_context(db,self.identity,entry_id=RECONCILE,params=self.params,_principal=self.principal)
        orders,_,assignments=_targets(db,self.params)
        self.captured_sources=frozenset(row.raw_order_id for row in orders)
        self.captured_source_parents={source:sorted({a.project_id for a in assignments if a.source_order_id==source})
            for source in self.captured_sources}
        self.captured_parents=frozenset(ref.id for ref in self.captured.contexts[0].slots.get('context_1',()))

    def check_demand_side_effect(self,db,source_order_ids):
        ids=set(source_order_ids)
        if (db is not self.db or db.get_transaction() is not self.transaction or self.captured is None
                or not ids or not ids <= self.captured_sources):
            raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','核对作废来源不在原完整选择内')
        parents=set(db.scalars(select(MaintenanceSourceOrderAssignment.project_id).where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(ids),MaintenanceSourceOrderAssignment.is_active.is_(True))))
        if not parents <= self.captured_parents:
            raise authz_error(409,'AUTHZ_CONTEXT_STALE','核对作废来源的实际归属已变化')

    def finish(self,db,result):
        if self.captured is None or db is not self.db or db.get_transaction() is not self.transaction:
            raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','核对缺少完整锁内对象')
        actor=service.lock_actor(db,self.identity)
        access=service.context_accesses(db,self.identity,self.captured,locked_user=actor)
        require_complete(access);service.check_context_precondition(self.precondition,access,self.captured.contexts[0].ref)
        from app.services.maintenance_wbdd_reconciliation import _digest
        target=artifacts.ArtifactTarget(SYNC_KIND,str(self.params['batch_id'])+':'+_digest([self.operator_identity,self.params['idempotency_key']]),1)
        if result['replayed']:artifacts.load_artifact_binding(db,target=target)
        else:
            state=_sync_state(db,target)
            artifacts.register_artifact_binding(db,self.identity,target=target,input_digest=state.input_digest,locked_user=actor)


def reconcile(db,identity,*,params,authorization_view=False,precondition=None):
    from app.services import maintenance_wbdd_sync as sync
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=RECONCILE,params=params,
            branch='discovery') as (snapshot,proof,discovery,query_accesses):
        require_complete(query_accesses)
        allowed_projects=_allowed_projects(snapshot,query_accesses.require(discovery.contexts[0].ref,
            'maintenance_wbdd_reconciliation','reconcile'))
        batch=_sync_context(snapshot,identity,entry_id=RECONCILE,params=params,read_snapshot=proof)
        access=service.context_accesses(snapshot,identity,batch,read_snapshot=proof)
        ref=batch.contexts[0].ref
        if authorization_view:return service.context_resolution(access,ref)
        require_complete(access);service.check_context_precondition(precondition,access,ref);principal=access.principal
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        if not db.in_transaction():db.begin()
        permit=WbddReconciliationAuthorization(db,identity,params,principal,precondition)
        result=sync.apply_reconciliation(db,**params,operated_by=principal.username,
            allowed_project_ids=allowed_projects,_authorization=permit)
        permit.finish(db,result);db.commit();return result
    except BaseException:
        db.rollback();raise
