"""Reminder reads and commands bind current projects and isolate financial fields."""
import json
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project import MaintenanceProjectContract
from app.schemas.maintenance_collection_reminders import DirectoryResponse, ProjectDetailResponse, FollowUpResponse
from app.services import maintenance_authorization as authorization
from app.services import maintenance_collection_reminders as reminders

DIRECTORY='POST /api/maintenance/collection-reminders/search'
PROJECT='GET /api/maintenance/projects/stable/{project_id}/collection-milestones'
FOLLOWUP='POST /api/maintenance/collection-milestones/{milestone_id}/follow-ups'
RESOURCE='maintenance_collection_followup'


def milestone_context(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    row=db.execute(select(MaintenanceCollectionMilestone.milestone_id,
        MaintenanceCollectionMilestone.project_id,MaintenanceCollectionMilestone.project_contract_id,
        MaintenanceCollectionMilestone.version,MaintenanceCollectionMilestone.is_active).where(
            MaintenanceCollectionMilestone.milestone_id==params['milestone_id'])).one_or_none()
    if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','回款节点不存在')
    contract=db.execute(select(MaintenanceProjectContract.project_contract_id,
        MaintenanceProjectContract.project_id,MaintenanceProjectContract.version,
        MaintenanceProjectContract.effective_to).where(
            MaintenanceProjectContract.project_contract_id==row.project_contract_id)).one_or_none()
    if contract is None or contract.project_id!=row.project_id:
        raise authz_error(409,'AUTHZ_CONTEXT_CHANGED','节点合同归属已变化')
    _,projects=authorization.resolve_project_subjects(db,identity,[row.project_id],
        read_snapshot=read_snapshot,locked_user=locked_user)
    schema=ENTRY_CONTRACTS[entry_id]['slots']['context_0']['selector_schema']
    selection=json.loads(json.dumps({k:params[k] for k in schema['properties'] if k in params},default=str))
    ref=authorization.slot_ref(entry_id,'context_0',row.milestone_id,selection=selection)
    context=authorization.resolved_context(db,entry_id=entry_id,branch='default',carrier=ref,
        slots={'context_0':(ref,)},subjects={RESOURCE:(projects[row.project_id].subject,)},
        revision=relation_revision({'project':projects[row.project_id].authorization_revision,
            'milestone':dict(row._mapping),'contract':dict(contract._mapping),'carrier':ref.to_dict()}))
    return create_context_batch(db,[context])


authorization.register_entry_resolver(FOLLOWUP,milestone_context)


def read(db,identity,*,entry_id,params,legacy_context,as_of,view=None,precondition=None):
    if entry_id not in {DIRECTORY,PROJECT}:raise ValueError('unknown reminder read')
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=entry_id,params=params) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        resource='maintenance_collection_followup_directory' if entry_id==DIRECTORY else RESOURCE
        action='search' if entry_id==DIRECTORY else 'read'
        access=accesses.require(ref,resource,action)
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        fields=service.field_context(access,legacy_context)
        if entry_id==DIRECTORY:
            data=reminders.search_collection_reminders(snapshot,as_of=as_of,user_ctx=fields,
                q_text=params.get('q'),owner_scope=params.get('owner_scope'),
                reminder_state=params.get('reminder_state'),page=params.get('page',1),
                page_size=params.get('page_size',24),authorization_condition=authorization.project_condition(access))
            payload=DirectoryResponse(**data).model_dump(mode='json')
        else:
            data=reminders.get_project_collection_milestones(snapshot,project_id=params['project_id'],
                as_of=as_of,user_ctx=fields,authorization_access=access)
            if data is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','项目不存在或已归档')
            payload=ProjectDetailResponse(**data).model_dump(mode='json')
        return {**payload,'authorization_context':accesses.wire(ref)}


def record(db,identity,*,milestone_id,command,legacy_context,as_of,view=None,precondition=None):
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    params={'milestone_id':milestone_id,**command}
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=FOLLOWUP,params=params) as (_,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        accesses.require(ref,RESOURCE,'record')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
    captured={}
    def authorize():
        actor,_=authorization.lock_final_users(db,identity)
        batch=authorization.resolve_entry_context(db,identity,entry_id=FOLLOWUP,params=params,locked_user=actor)
        accesses=service.context_accesses(db,identity,batch,locked_user=actor);ref=batch.contexts[0].ref
        access=accesses.require(ref,RESOURCE,'record')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        captured['authorization_context']=accesses.wire(ref)
        return actor,service.field_context(access,legacy_context)
    try:
        data=reminders.follow_up_collection_milestone(db,milestone_id=milestone_id,**command,
            operator=identity.username,user_ctx=legacy_context,as_of=as_of,_authorize=authorize)
        if data is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','回款节点不存在')
        if not captured:raise RuntimeError('reminder primitive missed final authorization')
        payload={**FollowUpResponse(**data).model_dump(mode='json'),**captured}
        db.commit();return payload
    except IntegrityError as exc:
        db.rollback();raise authz_error(409,'DOMAIN_CONFLICT','回款跟进版本或幂等键冲突') from exc
    except Exception:
        db.rollback();raise


EVIDENCE_READ='GET /api/maintenance/collection-milestones/{milestone_id}/evidence'
EVIDENCE_UPLOAD='POST /api/maintenance/collection-milestones/{milestone_id}/evidence'
for _entry in (EVIDENCE_READ,EVIDENCE_UPLOAD):
    authorization.register_entry_resolver(_entry,milestone_context)


def evidence_read(db,identity,*,milestone_id,view=None,precondition=None):
    from app.services import maintenance_collection_evidence as evidence
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=EVIDENCE_READ,params={'milestone_id':milestone_id}) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        accesses.require(ref,RESOURCE,'evidence_read')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        return {'milestone_id':milestone_id,'rows':evidence.list_evidence(snapshot,milestone_id),
            'authorization_context':accesses.wire(ref)}


def _evidence_locks(db,milestone_id):
    from app.models.maintenance_project import MaintenanceProject
    from app.models.maintenance_collection_evidence import MaintenanceCollectionEvidence
    from app.models.maintenance_manager import BusinessFile
    from app.services import maintenance_project_operations as operations
    row=db.execute(select(MaintenanceCollectionMilestone.project_id,MaintenanceCollectionMilestone.project_contract_id)
        .where(MaintenanceCollectionMilestone.milestone_id==milestone_id)).one_or_none()
    if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','回款节点不存在')
    operations.get_or_create_workbook_state(db,project_id=row.project_id,lock=True)
    project=db.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id==row.project_id)
        .with_for_update().execution_options(populate_existing=True))
    contract=db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_contract_id==row.project_contract_id)
        .with_for_update().execution_options(populate_existing=True))
    milestone=db.scalar(select(MaintenanceCollectionMilestone).where(
        MaintenanceCollectionMilestone.milestone_id==milestone_id)
        .with_for_update().execution_options(populate_existing=True))
    if (project is None or contract is None or milestone is None or
        not project.is_active or not milestone.is_active or milestone.project_id!=row.project_id or
        contract.project_id!=row.project_id or milestone.project_contract_id!=row.project_contract_id):
        raise authz_error(409,'AUTHZ_CONTEXT_CHANGED','回款节点实际归属或状态已变化')
    rows=list(db.scalars(select(MaintenanceCollectionEvidence).where(
        MaintenanceCollectionEvidence.milestone_id==milestone_id).order_by(MaintenanceCollectionEvidence.evidence_id)
        .with_for_update().execution_options(populate_existing=True)))
    if rows:list(db.scalars(select(BusinessFile).where(BusinessFile.file_id.in_([r.file_id for r in rows]))
        .order_by(BusinessFile.file_id).with_for_update().execution_options(populate_existing=True)))


def evidence_upload(db,identity,*,milestone_id,content,filename,mime_type,legacy_context,as_of,
                    view=None,precondition=None):
    from hashlib import sha256
    from app.services import maintenance_collection_evidence as evidence
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    filename,_,mime_type=evidence.validate_attachment(filename=filename,mime_type=mime_type,content=content)
    params={'milestone_id':milestone_id,'filename':filename,'mime_type':mime_type,'file_sha256':sha256(content).hexdigest()}
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=EVIDENCE_UPLOAD,params=params) as (_,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        accesses.require(ref,RESOURCE,'evidence_upload')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
    captured={}
    def authorize():
        actor,_=authorization.lock_final_users(db,identity)
        batch=authorization.resolve_entry_context(db,identity,entry_id=EVIDENCE_UPLOAD,params=params,locked_user=actor)
        accesses=service.context_accesses(db,identity,batch,locked_user=actor);ref=batch.contexts[0].ref
        access=accesses.require(ref,RESOURCE,'evidence_upload')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        captured['authorization_context']=accesses.wire(ref)
        return actor,service.field_context(access,legacy_context)
    try:
        _evidence_locks(db,milestone_id)
        actor,fields=authorize()
        payload=evidence.save_evidence(db,milestone_id=milestone_id,operator=actor.username,
            filename=filename,mime_type=mime_type,content=content,owner_user_id=actor.id)
        if payload['owner_user_id'] is None:
            raise authz_error(403,'AUTHZ_ARTIFACT_UNAVAILABLE','历史凭证缺少稳定来源，请重新提交独立凭证')
        evidence.write_evidence_files(file_id=payload['file_id'],object_key=payload['object_key'],content=content,
            meta={**{key:payload[key] for key in ('file_id','milestone_id','original_filename','mime_type',
                'size_bytes','md5','sha256','uploaded_by','uploaded_at')},'owner_user_id':payload['owner_user_id'],
                'content_origin':'user_uploaded_original','storage':'local'})
        closed=evidence.try_close_milestone_after_upload(db,milestone_id=milestone_id,
            evidence_id=payload['evidence_id'],operator=actor.username,user_ctx=fields,as_of=as_of,_authorize=authorize)
        result={**payload,'closed':bool(closed['closed']),
            'close_reason':None if closed['closed'] else closed.get('reason'),**captured}
        db.commit();return result
    except Exception:
        db.rollback();raise
