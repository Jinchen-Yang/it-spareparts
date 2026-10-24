"""Return-import tasks: actual bytes, stable producers and complete live targets."""
from datetime import UTC, datetime, timedelta
from hashlib import sha256, sha1
from types import SimpleNamespace
import io
import zipfile
from pathlib import Path
import secrets

from sqlalchemy import select, or_, func, cast
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.orm import Session

from app.authz import runtime, service, provenance
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, relation_revision
from app.authz.errors import authz_error
from app.authz.principal import resume_task_identity
from app.models.authorization import SysAuthorizationArtifact
from app.models.system import SysImportBatch, SysRawFile
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_return_receipt_import as business
from app.services.maintenance_workflow_authorization import captured_provenance, require_complete

BASE='/api/maintenance/doc-imports/return-receipts/jobs'
SUBMIT='POST '+BASE
READ='GET '+BASE+'/{batch_id}'
RAW=READ+'/original'
RETRY='POST '+BASE+'/{batch_id}/retry'
CANCEL='POST '+BASE+'/{batch_id}/cancel'
APPLY='POST '+BASE+'/{batch_id}/apply'
RESOURCE='maintenance_return_import'
KIND='return_import_batch'
PLAN_KIND='return_import_plan'


def _batch_target(batch):
    return artifacts.ArtifactTarget(KIND,batch.batch_id,batch.report_json['generation'])


def _state(db,target):
    batch=_header(db,target.id)
    report=batch.report_json
    archive=db.get(SysImportBatch,report['archive_batch_id'])
    raw=db.scalar(select(SysRawFile).where(SysRawFile.batch_id==report['archive_batch_id'],
        SysRawFile.file_hash==batch.file_hash,SysRawFile.storage_path==report['storage_path']))
    if (batch.owner_user_id is None or report['generation']!=target.generation or archive is None or raw is None
            or archive.file_hash!=batch.file_hash or archive.file_type!='return_receipts'):
        raise artifacts.unavailable()
    digest=relation_revision({'id':batch.batch_id,'owner':batch.owner_user_id,'hash':batch.file_hash,
        'filename':batch.filename,'path':report['storage_path'],'archive':archive.id})
    return artifacts.ArtifactDomainState(target,ObjectRef(KIND,batch.batch_id),digest,batch.owner_user_id)


def _plan_state(db,target):
    try:bid,generation=target.id.rsplit(':',1);generation=int(generation)
    except (ValueError,TypeError):raise artifacts.unavailable()
    batch=business._load(db,bid)
    if target.generation!=1 or generation!=batch.report_json['generation'] or batch.owner_user_id is None:
        raise artifacts.unavailable()
    report=batch.report_json
    heads=list(db.execute(select(business.Head.row_id,business.Head.row_no,business.Head.raw_json)
        .where(business.Head.batch_id==bid).order_by(business.Head.row_id)))
    lines=list(db.execute(select(business.Line.row_id,business.Line.head_row_id,business.Line.row_no,business.Line.raw_json)
        .where(business.Line.batch_id==bid).order_by(business.Line.row_id)))
    digest=relation_revision({'batch':bid,'generation':generation,'owner':batch.owner_user_id,'file':batch.file_hash,
        'plan':{k:report.get(k) for k in ('rows','summary','excluded','possible_duplicates_count','plan_hash','expires_at')},
        'heads':[list(r) for r in heads],'lines':[list(r) for r in lines]})
    return artifacts.ArtifactDomainState(target,ObjectRef(KIND,bid),digest,batch.owner_user_id)


artifacts.register_artifact_domain(KIND,artifacts.ArtifactDomainAdapter(_state,None,(),private=True))
artifacts.register_artifact_domain(PLAN_KIND,artifacts.ArtifactDomainAdapter(_plan_state,None,(),private=True))


def _plan_target(batch):return artifacts.ArtifactTarget(PLAN_KIND,f"{batch.batch_id}:{batch.report_json['generation']}",1)


def _bytes(batch):
    path=Path(batch.report_json['storage_path'])
    try:
        if path.is_symlink():raise artifacts.unavailable()
        with path.open('rb') as source:data=source.read(business.MAX_PREVIEW_BYTES+1)
    except OSError:raise artifacts.unavailable() from None
    if len(data)>business.MAX_PREVIEW_BYTES or sha256(data).hexdigest()!=batch.file_hash:raise artifacts.unavailable()
    return data


def _header(db,batch_id):
    B=business.Batch
    row=db.execute(select(B.batch_id,B.owner_user_id,B.file_hash,B.filename,
        B.report_json['protocol'].label('protocol'),B.report_json['generation'].label('generation'),
        B.report_json['storage_path'].label('storage_path'),
        B.report_json['archive_batch_id'].label('archive_batch_id'),
        func.jsonb_path_query_array(B.report_json,cast('$.rows[*].project_id',JSONPATH)).label('parents'),
        func.jsonb_path_query_array(B.report_json,cast('$.rows[*].before.project_id',JSONPATH)).label('old_parents'),
        func.jsonb_path_query_array(B.report_json,cast('$.rows[*].receipt_id',JSONPATH)).label('receipts'),
        func.jsonb_path_query_array(B.report_json,cast('$.rows[*].possible_duplicates[*].receipt_id',JSONPATH)).label('duplicates'))
        .where(B.batch_id==batch_id)).first()
    if row is None or row.protocol!=business.PROTOCOL:raise business.ImportError('not_found','返件导入任务不存在',404)
    return SimpleNamespace(batch_id=row.batch_id,owner_user_id=row.owner_user_id,file_hash=row.file_hash,
        filename=row.filename,report_json={'generation':row.generation,'storage_path':row.storage_path,'archive_batch_id':row.archive_batch_id},
        parents=set(row.parents+row.old_parents)-{None},receipt_ids=set(row.receipts+row.duplicates)-{None})


def _material(db,params):
    """Only input bytes and identity/relationship columns; no business plan SQL."""
    batch=_header(db,params['batch_id']) if 'batch_id' in params else None
    data=_bytes(batch) if batch is not None else params['data']
    parsed=business.parse_standard(data,_authorization_all_rows=True)
    names={h['raw']['项目名称'] for h in parsed['heads'] if h['raw']['项目名称']}
    P=business.Project
    projects=list(db.execute(select(P.project_id,P.project_code,P.display_name,P.version).where(P.is_active.is_(True),
        or_(P.project_code.in_(names),P.display_name.in_(names))))) if names else []
    name_map={name:[p for p in projects if name in (p.project_code,p.display_name)] for name in names}
    wbdds={business._clean(h['raw']['维保需求单'],business._WBDD_RE) for h in parsed['heads'] if h['raw']['维保需求单']}
    O=business.FMaintenanceOrder;A=business.Assignment;R=business.Receipt
    orders=list(db.execute(business.ledger.active_beta_maintenance_orders(select(O.id,O.raw_order_id,O.order_no)
        .where(O.order_no.in_(wbdds),O.data_status=='已生效'),O))) if wbdds else []
    links=list(db.execute(select(A.assignment_id,A.source_order_id,A.project_id,A.version).where(
        A.source_order_id.in_([o.raw_order_id for o in orders]),A.is_active.is_(True)).order_by(A.assignment_id))) if orders else []
    parents=set(batch.parents) if batch is not None else set()
    unresolved=not parsed['heads'];source_refs=set();head_ids=set()
    for head in parsed['heads']:
        raw=head['raw'];matches=name_map.get(raw['项目名称'],[])
        parents.update(p.project_id for p in matches)
        assigned=[]
        if raw['维保需求单']:
            number=business._clean(raw['维保需求单'],business._WBDD_RE)
            matching=[o for o in orders if o.order_no==number]
            assigned=[a for a in links if any(a.source_order_id==o.raw_order_id for o in matching)]
            parents.update(a.project_id for a in assigned)
            unresolved |= len(matching)!=1 or len(assigned)!=1
        elif len(matches)!=1:unresolved=True
        head_id=raw['数据ID(不可修改)'];head_ids.add(head_id)
        line_ids=[l['raw']['备件明细.数据ID(不可修改)'] for l in head['lines']]
        if raw['入库备件/整机']=='整机':line_ids.append('@machine')
        for line_id in line_ids:
            source_refs.add('rkd:native:'+business._hash({'source':'h3yun_rkd','head_id':head_id,'line_id':line_id}))
            source_refs.add('rkd:'+sha1(f"{raw['入库单号']}:{line_id}".encode()).hexdigest())
    filters=[R.source_ref.in_(source_refs),R.source_payload['identity']['head_id'].astext.in_(head_ids)]
    if batch is not None:filters.append(R.rkd_line_id.in_(batch.receipt_ids))
    receipts=list(db.execute(select(R.rkd_line_id,R.project_id,R.version).where(or_(*filters)).order_by(R.rkd_line_id)))
    parents.update(r.project_id for r in receipts if r.project_id)
    # Only the first standard sheet has a registered interpretation. Opaque
    # additional worksheets cannot prove a restricted project closure.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        unresolved |= sum(n.startswith('xl/worksheets/sheet') and n.endswith('.xml') for n in archive.namelist())>1
    metadata={'projects':[list(p) for p in projects],'orders':[list(o) for o in orders],
        'assignments':[list(a) for a in links],'receipts':[list(r) for r in receipts]}
    return batch,data,parsed,{'plan_hash':relation_revision(metadata)},sorted(parents),receipts,unresolved


def _context(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None,_principal=None):
    principal=_principal or foundation.resolve_project_subjects(db,identity,[],read_snapshot=read_snapshot,locked_user=locked_user)[0]
    if 'batch_id' in params:
        owner=db.scalar(select(business.Batch.owner_user_id).where(business.Batch.batch_id==params['batch_id']))
        if owner is None or owner!=principal.id:
            raise authz_error(403,'AUTHZ_FORBIDDEN','任务仅允许实际上传者操作；旧任务请重新上传')
    batch,data,parsed,plan,parent_ids,receipts,unresolved=_material(db,params)
    projects=foundation.project_evidence(db,principal,parent_ids)
    if batch is not None and (batch.owner_user_id is None or batch.owner_user_id!=principal.id):
        raise authz_error(403,'AUTHZ_FORBIDDEN','任务仅允许实际上传者操作；旧任务请重新上传')
    persisted=entry_id==SUBMIT and batch is not None
    slot='context_receipt' if persisted else 'context_0'
    if batch is None:
        selection={'input_sha256':sha256(data).hexdigest(),'doc_type':'return_receipt'}
        identifier=relation_revision({'actor':principal.id,'purpose':'return_import.enqueue','input':selection})
        ref=foundation.slot_ref(entry_id,slot,identifier,selection=selection)
    else:ref=foundation.slot_ref(entry_id,slot,batch.batch_id)
    actual_branch=('persisted_batch' if parent_ids else 'persisted_no_targets') if persisted else ('default' if parent_ids else 'no_targets')
    if branch is not None and branch!=actual_branch:
        raise authz_error(409,'AUTHZ_CONTEXT_STALE','任务的实际目标集合已变化')
    slots={slot:(ref,)}
    if parent_ids:slots['context_1']=tuple(foundation.slot_ref(entry_id,'context_1',pid) for pid in parent_ids)
    subjects=[projects[pid].subject for pid in parent_ids]
    if unresolved:subjects.append(PolicySubject(ref.id,{'is_manager':False,'is_viewer':False,'is_sales':False,'is_active':False}))
    revision=relation_revision({'actor':[principal.id,principal.authorization_epoch],
        'file':sha256(data).hexdigest(),'generation':batch.report_json['generation'] if batch is not None else None,
        'projects':{pid:p.authorization_revision for pid,p in projects.items()},
        'plan':plan['plan_hash'],'receipts':[[r.rkd_line_id,r.project_id,r.version] for r in receipts],
        'command':params.get('command')})
    return create_context_batch(db,[foundation.resolved_context(db,entry_id=entry_id,branch=actual_branch,carrier=ref,
        slots=slots,subjects={RESOURCE:tuple(subjects)},revision=revision)])


def _reference(db,identity,ref,*,entry_id,branch,read_snapshot=None,locked_user=None):
    if entry_id==RAW:
        actual=_header(db,ref.id)
        artifacts.load_artifact_binding(db,target=_batch_target(actual))
    return _context(db,identity,entry_id=entry_id,params={'batch_id':ref.id},branch=branch,
        read_snapshot=read_snapshot,locked_user=locked_user)


for _entry in (SUBMIT,READ,RAW,RETRY,CANCEL,APPLY):
    foundation.register_entry_resolver(_entry,_context,reference_resolver=_reference)


def _check(db,identity,entry,params,*,actor=None,principal=None,precondition=None):
    batch=_context(db,identity,entry_id=entry,params=params,locked_user=actor,_principal=principal)
    accesses=service.context_accesses(db,identity,batch,locked_user=actor)
    require_complete(accesses)
    service.check_context_precondition(precondition,accesses,batch.contexts[0].ref)
    return batch,accesses


def _lock_domain(db,params):
    from app.services import maintenance_project_operations as operations, project_names
    business.ledger.lock_receipt_context(db)
    business._lock(db,'return-receipt-import-apply')
    material=_material(db,params)
    project_names.lock_display_name_identities(db,[h['raw']['项目名称'] for h in material[2]['heads'] if h['raw']['项目名称']])
    material=_material(db,params)
    parents=material[4]
    operations.lock_workbook_states(db,project_ids=parents)
    list(db.scalars(select(business.Project).where(business.Project.project_id.in_(parents))
        .order_by(business.Project.project_id).with_for_update().execution_options(populate_existing=True)))
    list(db.scalars(select(business.Receipt).where(business.Receipt.rkd_line_id.in_([r.rkd_line_id for r in material[5]]))
        .order_by(business.Receipt.rkd_line_id).with_for_update().execution_options(populate_existing=True)))
    if material[0] is not None:business._load(db,material[0].batch_id,lock=True)
    after=_material(db,params)
    if after[4]!=parents or {r.rkd_line_id for r in after[5]}!={r.rkd_line_id for r in material[5]}:
        raise authz_error(409,'AUTHZ_CONTEXT_STALE','锁定期间任务目标已变化')
    return after


def _assert_plan(db,batch):
    if batch.report_json.get('plan_hash') is not None:
        artifacts.load_artifact_binding(db,target=_plan_target(batch))
    elif batch.report_json.get('rows'):
        raise artifacts.unavailable()


class ReturnImportAuthorization:
    def __init__(self,db,identity,entry,params,principal,precondition):
        self.db,self.transaction=db,db.get_transaction()
        self.identity,self.entry,self.params,self.principal,self.precondition=identity,entry,params,principal,precondition
        self.captured=None

    def check(self,db,operation,digest):
        expected={SUBMIT:'enqueue',APPLY:'apply',RETRY:'retry',CANCEL:'cancel'}[self.entry]
        if (db is not self.db or db.get_transaction() is not self.transaction or operation!=expected
                or digest is not None and self.params.get('data') is not None and digest!=sha256(self.params['data']).hexdigest()):
            raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','导入许可与当前事务或输入不一致')
        return self.principal.id

    def finish_enqueue(self,db,batch,*,replayed):
        self.check(db,'enqueue',batch.file_hash)
        actor=service.lock_actor(db,self.identity)
        _check(db,self.identity,SUBMIT,self.params,actor=actor,precondition=self.precondition)
        if replayed:
            artifacts.load_artifact_binding(db,target=_batch_target(batch));_assert_plan(db,batch)
            _check(db,self.identity,SUBMIT,{'batch_id':batch.batch_id},actor=actor)
        else:
            _,access=_check(db,self.identity,SUBMIT,{'batch_id':batch.batch_id},actor=actor)
            target=_batch_target(batch)
            artifacts.register_artifact_binding(db,self.identity,target=target,input_digest=_state(db,target).input_digest,
                operation=artifacts.IdempotentOperation('return_import_enqueue',batch.idempotency_key),
                provenance=captured_provenance(access),locked_user=actor)

    def capture(self,db):
        self.check(db,'apply',None)
        self.captured=_context(db,self.identity,entry_id=APPLY,params=self.params,_principal=self.principal)
        _assert_plan(db,business._load(db,self.params['batch_id']))

    def finish_apply(self,db):
        self.check(db,'apply',None)
        if self.captured is None:raise artifacts.unavailable()
        actor=service.lock_actor(db,self.identity)
        access=service.context_accesses(db,self.identity,self.captured,locked_user=actor)
        require_complete(access);service.check_context_precondition(self.precondition,access,self.captured.contexts[0].ref)


def submit(db,identity,*,data,filename,key,authorization_view=False,precondition=None):
    params={'data':data}
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=SUBMIT,params=params) as (_,_,context,access):
        if authorization_view:return service.context_resolution(access,context.contexts[0].ref)
        require_complete(access);service.check_context_precondition(precondition,access,context.contexts[0].ref)
        principal=access.principal
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        _lock_domain(db,params)
        permit=ReturnImportAuthorization(db,identity,SUBMIT,params,principal,precondition)
        batch,created=business.enqueue(db,data,filename,principal.username,key,_authorization=permit)
        result={'batch_id':batch.batch_id,'status':batch.report_json['status']}
        generation=batch.report_json['generation'];db.commit()
        return result,created,generation
    except BaseException:db.rollback();raise


def read(db,identity,*,entry,batch_id,offset=0,limit=100,authorization_view=False,precondition=None):
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry,params={'batch_id':batch_id}) as (snapshot,_,context,access):
        ref=context.contexts[0].ref
        if authorization_view:return service.context_resolution(access,ref)
        require_complete(access);service.check_context_precondition(precondition,access,ref)
        batch=business._load(snapshot,batch_id)
        artifacts.load_artifact_binding(snapshot,target=_batch_target(batch))
        if entry==RAW:return _bytes(batch),Path(batch.filename).name,captured_provenance(access)
        _assert_plan(snapshot,batch)
        return {**business.public_job(snapshot,batch,offset=offset,limit=limit),'authorization_context':access.wire(ref)}


def command(db,identity,*,entry,batch_id,command=None,authorization_view=False,precondition=None):
    params={'batch_id':batch_id}
    # Apply's JSON is bound by the business token/hash; the registered object
    # selector remains closed. Precondition observes actual current topology.
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry,params=params) as (_,_,context,access):
        if authorization_view:return service.context_resolution(access,context.contexts[0].ref)
        require_complete(access);service.check_context_precondition(precondition,access,context.contexts[0].ref)
        principal=access.principal
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        _lock_domain(db,params)
        batch=business._load(db,batch_id)
        binding=artifacts.load_artifact_binding(db,target=_batch_target(batch));_assert_plan(db,batch)
        permit=ReturnImportAuthorization(db,identity,entry,params,principal,precondition)
        if entry==APPLY:
            result=business.apply(db,batch_id,principal.username,**command,_authorization=permit)
        else:
            actor=service.lock_actor(db,identity)
            _check(db,identity,entry,params,actor=actor,precondition=precondition)
            batch=business.control(db,batch_id,'retry' if entry==RETRY else 'cancel',_authorization=permit)
            business._state(batch,rows=[],summary=None,excluded={},plan_hash=None,possible_duplicates_count=0)
            db.flush()
            if entry==RETRY:
                _,access=_check(db,identity,RETRY,params,actor=actor)
                lineage=captured_provenance(access)
            else:lineage=None
            artifacts.restart_artifact_task(db,identity,binding=binding,expected_generation=binding.target.generation,
                provenance=lineage,locked_user=actor)
            result={'batch_id':batch.batch_id,'status':batch.report_json['status'],'generation':batch.report_json['generation']}
        db.commit();return result
    except BaseException:db.rollback();raise


def _task_identity(db,batch_id,generation):
    binding=artifacts.load_artifact_binding(db,target=artifacts.ArtifactTarget(KIND,batch_id,generation))
    if binding.provenance is None:raise artifacts.unavailable()
    identity=resume_task_identity(db,binding.subject.owner_user_id,binding.subject.enqueued_epoch)
    return identity,binding


def run_job(batch_id,generation,*,session_factory):
    """No worker failure can create a new write transaction after revocation."""
    try:
        with session_factory() as db:
            identity,binding=_task_identity(db,batch_id,generation)
            # Validate all dependencies in one independent RR observation before
            # loading bytes; this read holds no domain or actor locks afterwards.
            provenance.validate_provenance(db,identity,artifacts.plain(binding.provenance))
            batch=business._load(db,batch_id)
            if batch.report_json['generation']!=generation or batch.report_json['status']!='queued':return
            data=_bytes(batch)
            db.rollback()
        parsed=business.parse_standard(data)
        with session_factory() as db:
            material=_lock_domain(db,{'batch_id':batch_id})
            batch=business._load(db,batch_id)
            if batch.report_json['generation']!=generation or batch.report_json['status']!='queued':return
            identity,binding=_task_identity(db,batch_id,generation)
            # Stage raw input and plan under the complete domain lock set. No
            # externally visible state exists before final live actor/PDP check.
            business._save_raw(db,batch,parsed)
            plan=business.build_plan(db,batch)
            business._state(batch,**plan,excluded=parsed['excluded'],status='ready',
                preview_token=secrets.token_urlsafe(32),expires_at=(datetime.now(UTC)+timedelta(hours=2)).isoformat())
            batch.issue_rows=plan['summary']['blocking_errors'];db.flush()
            actor=service.lock_actor(db,identity)
            provenance.validate_provenance(db,identity,artifacts.plain(binding.provenance),locked_user=actor)
            target=_plan_target(batch)
            artifacts.register_artifact_binding(db,identity,target=target,input_digest=_plan_state(db,target).input_digest,locked_user=actor)
            db.commit()
    except Exception:
        # The original queued generation is retained for an explicitly
        # authorized cancel/retry. Do not publish a failed report as a new actor.
        return
