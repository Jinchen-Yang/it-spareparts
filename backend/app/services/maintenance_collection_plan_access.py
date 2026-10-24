"""Whole uploaded collection plans: stable ownership, input binding and live grants.

Original uploads have an immutable domain sidecar, not generated-content lineage.
Readers obtain their own complete raw-download contract in the same snapshot as
the actual file hash check. Apply keeps the original whole-plan transaction.
"""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Text, cast, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import JSONPATH

from app.authz import service
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.authz.provenance import AccessCollector
from app.models.maintenance_manager import MaintenanceCollectionPlanImportBatch as Batch
from app.models.maintenance_manager import MaintenanceCollectionPlanSourceBinding as Binding
from app.models.maintenance_manager import MaintenanceCollectionMilestone as Milestone
from app.models.maintenance_project import MaintenanceProject as Project, MaintenanceProjectContract as Contract
from app.schemas.maintenance_collection_reminders import ApplyBinding, ApplyResponse, BindingOptionsResponse, PreviewResponse
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as authorization
from app.services import maintenance_collection_plan_imports as imports

PREVIEW='POST /api/maintenance/collection-plan-imports/preview'
APPLY='POST /api/maintenance/collection-plan-imports/{batch_id}/apply'
OPTIONS='GET /api/maintenance/collection-plan-imports/{batch_id}/binding-options'
RAW='GET /api/maintenance/collection-plan-imports/{batch_id}/source-file'
WRITE='maintenance_collection_plan_import'
READ='maintenance_collection_plan_import_read'
KIND='collection_plan_import_batch'


def _metadata(db,batch_id):
    row=db.execute(select(Batch.batch_id,Batch.owner_user_id,Batch.file_sha256,Batch.file_size,
        Batch.storage_key,Batch.operation_key,Batch.contract_version,Batch.version,Batch.status,
        Batch.data_version,func.encode(func.sha256(func.convert_to(cast(Batch.plan_json,Text),'UTF8')),'hex').label('plan_digest'),
        func.jsonb_path_query_array(Batch.plan_json,cast('$.orders[*].binding',JSONPATH)).label('bindings'),
        func.jsonb_path_query_array(Batch.plan_json,cast('$.orders[*].external_order_no',JSONPATH)).label('order_numbers'))
        .where(Batch.batch_id==batch_id)).one_or_none()
    if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','导入批次不存在')
    return dict(row._mapping)


def _input_digest(meta):
    return relation_revision({k:meta[k] for k in ('batch_id','owner_user_id','file_sha256','file_size',
        'storage_key','operation_key','contract_version','plan_digest')})


def _state(db,target):
    meta=_metadata(db,target.id)
    if target.generation!=1:raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(target,ObjectRef(KIND,target.id,{}),_input_digest(meta),meta['owner_user_id'])


artifacts.register_artifact_domain(KIND,artifacts.ArtifactDomainAdapter(_state,None,(),private=False))


def _topology(db,bindings):
    pids=sorted({b['project_id'] for b in bindings if b.get('project_id')})
    cids=sorted({b['project_contract_id'] for b in bindings if b.get('project_contract_id')})
    projects=[tuple(row) for row in db.execute(select(Project.project_id,Project.version,Project.is_active)
        .where(Project.project_id.in_(pids)).order_by(Project.project_id))]
    contracts=[tuple(row) for row in db.execute(select(Contract.project_contract_id,Contract.project_id,
        Contract.version,Contract.effective_from,Contract.effective_to).where(Contract.project_contract_id.in_(cids))
        .order_by(Contract.project_contract_id))]
    milestones=[tuple(row) for row in db.execute(select(Milestone.milestone_id,Milestone.project_id,
        Milestone.project_contract_id,Milestone.version,Milestone.is_active)
        .where(Milestone.project_contract_id.in_(cids)).order_by(Milestone.milestone_id))]
    return json.loads(json.dumps({'projects':projects,'contracts':contracts,'milestones':milestones},default=str))


def _preview_material(db,identity,params):
    # Client bytes are parsed only to discover complete current binding metadata.
    content=params['content'];filename=params['filename'];key=params['idempotency_key']
    if not 8<=len(key)<=128:raise imports.CollectionPlanImportInvalid('Idempotency-Key 必须为 8–128 字符')
    try:
        parsed=imports.parse_project_manager_collection_xls(content,filename=filename)
        orders=[r.external_order_no for r in parsed.rows]
    except imports.CollectionPlanContractError:
        orders=[]  # The error upload itself is retained only after the real preview grant.
    rows=[dict(row._mapping) for row in db.execute(select(Binding.binding_id,Binding.external_order_no,
        Binding.project_id,Binding.project_contract_id,Binding.version).where(
            Binding.source_system==imports._SOURCE_SYSTEM,Binding.external_order_no.in_(orders))
        .order_by(Binding.binding_id))]
    command={'file_sha256':sha256(content).hexdigest(),'filename':filename,'idempotency_key':key,
        'contract_version':imports.CONTRACT_VERSION,'as_of':str(params['as_of'])}
    return command,rows,_topology(db,rows)


def resolve(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    principal=authorization._named_principal(db,identity,read_snapshot=read_snapshot,locked_user=locked_user)
    if entry_id==PREVIEW:
        command,bindings,topology=_preview_material(db,identity,params)
        digest=relation_revision(command)
        selection={'file_sha256':command['file_sha256'],'command_plan_sha256':digest}
        ref=authorization.slot_ref(entry_id,'context_0',relation_revision({'actor':principal.id,'command':command}),selection=selection)
        resource=WRITE;owner=principal.id;state={'command':command,'bindings':bindings,'topology':topology}
    else:
        meta=_metadata(db,params['batch_id']);owner=meta['owner_user_id']
        schema=ENTRY_CONTRACTS[entry_id]['slots']['context_0']['selector_schema']
        selection={key:params[key] for key in schema['properties'] if key in params}
        ref=authorization.slot_ref(entry_id,'context_0',meta['batch_id'],selection=selection)
        resource=WRITE if entry_id==APPLY else READ
        bindings=[*(meta['bindings'] or []),*params.get('bindings',[])]
        live=[dict(r._mapping) for r in db.execute(select(Binding.binding_id,Binding.project_id,
            Binding.project_contract_id,Binding.version).where(Binding.source_system==imports._SOURCE_SYSTEM,
                Binding.external_order_no.in_(meta['order_numbers'] or [])).order_by(Binding.binding_id))]
        if entry_id==APPLY:bindings.extend(live)
        state={'batch':meta,'topology':_topology(db,bindings),'current_bindings':live if entry_id==APPLY else []}
    slots={'context_0':(ref,)}
    if entry_id==APPLY:
        if not state['topology']['projects'] or not state['topology']['contracts']:
            raise imports.CollectionPlanImportInvalid('应用需要完整实际项目和合同绑定')
        slots['context_projects']=tuple(authorization.slot_ref(entry_id,'context_projects',row[0]) for row in state['topology']['projects'])
        slots['context_contracts']=tuple(authorization.slot_ref(entry_id,'context_contracts',row[0]) for row in state['topology']['contracts'])
    context=authorization.resolved_context(db,entry_id=entry_id,branch='default',carrier=ref,
        slots=slots,subjects={resource:(PolicySubject(ref.id,{'is_owner':owner==principal.id}),)},
        revision=relation_revision({'state':state,'actor':[principal.id,principal.authorization_epoch],
            'carrier':ref.to_dict()}))
    return create_context_batch(db,[context])


def _reference(db,identity,ref,*,entry_id,branch,actions_by_resource=None,read_snapshot=None,locked_user=None):
    if entry_id==PREVIEW:raise authz_error(403,'AUTHZ_ARTIFACT_UNAVAILABLE','上传意图不可恢复为持久来源')
    _sidecar(db,ref.id)
    return resolve(db,identity,entry_id=entry_id,params={'batch_id':ref.id,**dict(ref.selector.get('selection',{}))},
        read_snapshot=read_snapshot,locked_user=locked_user)


for _entry in (PREVIEW,APPLY,OPTIONS,RAW):
    authorization.register_entry_resolver(_entry,resolve,reference_resolver=_reference)


def _view(view):
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')


def _sidecar(db,batch_id):
    return artifacts.load_artifact_binding(db,target=artifacts.ArtifactTarget(KIND,batch_id,1))


def _lock_preview(db,identity,params,owner_id):
    # Serialize only this owner's idempotent creation; then collect the complete
    # existing domain set before the final actor SHARE. No account lock upgrade.
    imports.lock_collection_plan_bindings(db)
    db.execute(text('select pg_advisory_xact_lock(hashtextextended(:key,0))'),
        {'key':'collection-plan:'+str(owner_id)+':'+params['idempotency_key']})
    _,bindings,_=_preview_material(db,identity,params)
    pids=sorted({r['project_id'] for r in bindings});cids=sorted({r['project_contract_id'] for r in bindings})
    for model,column,ids in ((Project,Project.project_id,pids),(Contract,Contract.project_contract_id,cids)):
        list(db.scalars(select(model).where(column.in_(ids)).order_by(column).with_for_update(read=True)
            .execution_options(populate_existing=True)))
    list(db.scalars(select(Binding).where(Binding.binding_id.in_([r['binding_id'] for r in bindings]))
        .order_by(Binding.binding_id).with_for_update(read=True).execution_options(populate_existing=True)))
    list(db.scalars(select(Milestone).where(Milestone.project_contract_id.in_(cids))
        .order_by(Milestone.milestone_id).with_for_update(read=True).execution_options(populate_existing=True)))


def preview(db,identity,*,content,filename,idempotency_key,legacy_context,as_of,view=None,precondition=None):
    _view(view);params={'content':content,'filename':filename,'idempotency_key':idempotency_key,'as_of':as_of}
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=PREVIEW,params=params) as (_,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        initial=accesses.require(ref,WRITE,'preview')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
    artifacts.begin_artifact_protocol(db.get_bind());new_storage=[]
    try:
        _lock_preview(db,identity,params,initial.principal.id)
        actor,_=authorization.lock_final_users(db,identity)
        batch=resolve(db,identity,entry_id=PREVIEW,params=params,locked_user=actor)
        accesses=service.context_accesses(db,identity,batch,locked_user=actor);ref=batch.contexts[0].ref
        access=accesses.require(ref,WRITE,'preview')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        old=db.scalar(select(Batch).where(Batch.owner_user_id==actor.id,
            Batch.operation_key==imports._operation_key(actor.id,idempotency_key)))
        if old is not None:_sidecar(db,old.batch_id)
        failure=None
        try:
            result=imports.preview_collection_plan_import(db,content=content,filename=filename,
                idempotency_key=idempotency_key,owner_user_id=actor.id,operator=actor.username,
                user_ctx=service.field_context(access,legacy_context),as_of=as_of,_authorized_user=actor,_created_storage=new_storage.append)
        except imports.CollectionPlanImportInvalid as exc:
            failure=exc;result=None
        stored=db.scalar(select(Batch).where(Batch.owner_user_id==actor.id,
            Batch.operation_key==imports._operation_key(actor.id,idempotency_key)))
        if stored is None:raise failure or RuntimeError('preview omitted its actual batch')
        if old is None:
            target=artifacts.ArtifactTarget(KIND,stored.batch_id,1)
            artifacts.register_artifact_binding(db,identity,target=target,input_digest=_state(db,target).input_digest,
                operation=artifacts.IdempotentOperation('collection_plan_preview',stored.operation_key),locked_user=actor)
        db.commit()
        if failure:raise failure
        return {**PreviewResponse(**result).model_dump(mode='json'),'authorization_context':accesses.wire(ref)}
    except Exception:
        db.rollback()
        for key in new_storage:imports._delete_unreferenced_evidence(db,storage_key=key)
        raise


def read(db,identity,*,entry_id,params,legacy_context,view=None,precondition=None):
    _view(view)
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry_id,params=params) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        access=accesses.require(ref,READ,'raw_download' if entry_id==RAW else 'read')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        _sidecar(snapshot,ref.id)
        if entry_id==OPTIONS:
            data=imports.search_collection_binding_options(snapshot,batch_id=ref.id,q_text=params['q'],
                page=params['page'],page_size=params['page_size'],user_ctx=legacy_context,authorization_access=access)
            return {**BindingOptionsResponse(**data).model_dump(mode='json'),'authorization_context':accesses.wire(ref)}
        meta=_metadata(snapshot,ref.id)
        try:
            with (imports._storage_dir()/meta['storage_key']).open('rb') as source:
                content=source.read(8 * 1024 * 1024 + 1)
        except OSError:raise artifacts.unavailable() from None
        if len(content)!=meta['file_size'] or sha256(content).hexdigest()!=meta['file_sha256']:raise artifacts.unavailable()
        collector=AccessCollector(access.principal.id,access.authorization_epoch,access.catalog_version)
        collector.record(access)
        return content,collector.snapshot()


def apply(db,identity,*,batch_id,command,legacy_context,as_of,view=None,precondition=None):
    _view(view);params={'batch_id':batch_id,**command}
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=APPLY,params=params) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        access=accesses.require(ref,WRITE,'apply');principal=access.principal
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        _sidecar(snapshot,batch_id)
    captured={}
    def authorize(db,stored,bindings,*,project_ids,contract_ids):
        actual=list(db.execute(select(Binding.project_id,Binding.project_contract_id).where(
            Binding.source_system==imports._SOURCE_SYSTEM,Binding.external_order_no.in_([b.external_order_no for b in bindings]))))
        if any(row.project_id not in project_ids or row.project_contract_id not in contract_ids for row in actual):
            raise authz_error(409,'AUTHZ_CONTEXT_CHANGED','绑定实际归属在加锁期间已变化')
        actor,_=authorization.lock_final_users(db,identity)
        batch=resolve(db,identity,entry_id=APPLY,params=params,locked_user=actor)
        accesses=service.context_accesses(db,identity,batch,locked_user=actor);ref=batch.contexts[0].ref
        accesses.require(ref,WRITE,'apply')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        _sidecar(db,batch_id);captured['authorization_context']=accesses.wire(ref)
        return actor
    try:
        result=imports.apply_collection_plan_import(db,batch_id=batch_id,**{k:v for k,v in command.items() if k!='bindings'},
            bindings=[ApplyBinding(**v) for v in command['bindings']],owner_user_id=principal.id,operator=principal.username,
            user_ctx=legacy_context,as_of=as_of,_authorization=SimpleNamespace(principal=principal,authorize=authorize))
        if not captured:raise RuntimeError('apply omitted final authorization')
        result={**ApplyResponse(**result).model_dump(mode='json'),**captured};db.commit();return result
    except Exception:db.rollback();raise
