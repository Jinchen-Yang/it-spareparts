"""Generic source import contracts: verified input, scoped history, and current publication.

HTTP and workers call this service; it never imports an API module. Pure ETL
selection/transform remains in app.etl and has no transport authority.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import csv
import io
import json
import logging
import os
import tempfile
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, func, text, exists, Text
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from app.authz import domain_contexts as contexts, runtime, service
from app.authz.catalog import CATALOG_VERSION
from app.authz.context import PolicySubject, canonical_json, create_context_batch, relation_revision, read_snapshot
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.authz.import_contract import *
from app.authz.import_context_registry import source_branch
from app.authz.principal import load_principal, resume_task_identity
from app.authz.provenance import AccessCollector
from app.config import MAX_IMPORT_FILES, MAX_UPLOAD_MB
from app.etl import pipeline, precheck as content_precheck, reader, sheet_selection
from app.etl.reader import ReaderError
from app.etl.transform import SOFT_ERROR_TYPES
from app.models.system import SysImportBatch, SysImportError, SysImportJob, SysImportJobItem, SysRawFile
from app.services.authorization_downloads import stream_materialized_bytes, attachment_headers

_LOG = logging.getLogger(__name__)


def digest(value):
    return sha256(canonical_json(value).encode()).hexdigest()


def fail(status, code, message, *, commit_state='none', **extra):
    exc=authz_error(status,code,message)
    exc.detail.update(commit_state=commit_state,**extra)
    return exc


@dataclass(frozen=True)
class InputFile:
    path: str
    filename: str
    mime_type: str
    content_sha256: str
    content_size: int
    source_domain: str | None
    selection: tuple
    error_code: str | None = None

    def manifest(self):
        return {'filename':self.filename,'mime_type':self.mime_type,'sha256':self.content_sha256,
                'size':self.content_size,'source_domain':self.source_domain,
                'selection':[list(s) for s in self.selection],'parser_version':PARSER_VERSION}


def inspect_input(path, filename, mime_type):
    source=None;selected=();error=None
    try:
        reader.reject_roundtrip_workbook(path)
        sheets=reader.inspect_workbook(path,load_data=False)
        selection=sheet_selection.select_workbook_sheets(sheets)
        selected=tuple((s.sheet_name,s.file_type) for s in selection.selected)
        types={s.file_type for s in selection.selected}
        if types=={'expense'}:source='expense'
        elif len(types)==1 and next(iter(types)) in SOURCES:source=next(iter(types))
    except ReaderError as exc:
        error=exc.code
    return InputFile(path,filename,mime_type,pipeline.sha256_file(path),os.path.getsize(path),source,selected,error)


@contextmanager
def uploaded_inputs(files):
    if not files or len(files)>MAX_IMPORT_FILES:
        raise fail(422,'IMPORT_CONTENT_INVALID','上传文件数量无效')
    saved=[];values=[]
    try:
        for upload in files:
            name=os.path.basename(upload.filename or 'upload.xlsx')
            if not name.lower().endswith('.xlsx'):
                raise fail(422,'IMPORT_CONTENT_INVALID','仅支持 .xlsx 文件')
            fd,path=tempfile.mkstemp(suffix='.xlsx');saved.append(path)
            size=0
            with os.fdopen(fd,'wb') as output:
                while chunk:=upload.file.read(1024*1024):
                    size+=len(chunk)
                    if size>MAX_UPLOAD_MB*1024*1024:raise fail(413,'IMPORT_CONTENT_INVALID','上传文件超过大小限制')
                    output.write(chunk)
            yield_value=inspect_input(path,name,upload.content_type or 'application/octet-stream')
            values.append(yield_value)
        yield tuple(values)
    finally:
        for path in saved:
            if os.path.exists(path):os.remove(path)


def canonical(db, *, inputs=None, entry_id=None):
    if entry_id==VOID:resources=('import_expense_void',)
    elif inputs is not None:
        sources={f.source_domain for f in inputs if f.source_domain}
        resources=tuple('import_'+s for s in sources) or tuple('import_'+s for s in SOURCES)
    else:resources=tuple('import_'+s for s in SOURCES)
    states=runtime.runtime_states(db)
    return any(states[r].runtime_state!='legacy' for r in resources)


def _input_params(inputs, mode, preview_tokens=()):
    if mode not in ('skip','upsert'):raise fail(422,'IMPORT_CONTENT_INVALID','导入模式无效')
    return {'inputs':tuple(inputs),'mode':mode,'preview_tokens':tuple(preview_tokens)}


def _valid_id(value):
    if type(value) is not int or not 0<value<2**53:raise fail(422,'AUTHZ_INVALID_REQUEST','编号无效')
    return value


def _batch_header(db, batch_id):
    row=db.execute(select(SysImportBatch.id,SysImportBatch.owner_user_id,
        SysImportBatch.authorization_protocol,SysImportBatch.source_domain,SysImportBatch.file_hash,
        func.md5(func.coalesce(SysImportBatch.report_json.cast(Text),'null')).label('report_digest'))
        .where(SysImportBatch.id==_valid_id(batch_id))).mappings().one_or_none()
    if row is None:raise fail(404,'AUTHZ_OBJECT_NOT_FOUND','导入批次不存在')
    if row['authorization_protocol']!=PROTOCOL or row['source_domain'] not in SOURCES or row['owner_user_id'] is None:
        raise fail(409,'IMPORT_ORIGIN_UNVERIFIED','历史批次来源无法核验')
    errors=db.scalar(select(func.md5(func.coalesce(func.string_agg(
        func.concat_ws('|',SysImportError.id,SysImportError.row_no,SysImportError.error_type,
            SysImportError.error_detail), aggregate_order_by('|',SysImportError.id)),'')))
        .where(SysImportError.batch_id==batch_id))
    return {**row,'errors_digest':errors}


def _job_header(db, job_id):
    row=db.execute(select(SysImportJob.id,SysImportJob.owner_user_id,SysImportJob.authorization_protocol,
        SysImportJob.enqueued_epoch,SysImportJob.authorization_generation,SysImportJob.manifest_digest)
        .where(SysImportJob.id==_valid_id(job_id))).mappings().one_or_none()
    if row is None:raise fail(404,'AUTHZ_OBJECT_NOT_FOUND','导入作业不存在')
    sources=tuple(db.scalars(select(SysImportJobItem.source_domain).where(SysImportJobItem.job_id==job_id).distinct()))
    if (row['authorization_protocol']!=PROTOCOL or row['owner_user_id'] is None or not sources
            or set(sources)-set(SOURCES) or not row['manifest_digest']):
        raise fail(409,'IMPORT_ORIGIN_UNVERIFIED','历史作业来源无法核验')
    return dict(row),sources


def _resolve(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    actor=contexts._named_principal(db,identity,read_snapshot=read_snapshot,locked_user=locked_user)
    selection={};state={};sources=();actual_branch='default'
    if entry_id in (PRECHECK,UPLOAD,UPLOAD_BATCH,VOID):
        inputs=params['inputs'];sources=tuple(sorted({f.source_domain for f in inputs if f.source_domain}))
        if entry_id in (UPLOAD,UPLOAD_BATCH) and any(f.source_domain is None for f in inputs):
            raise fail(422,'IMPORT_CONTENT_INVALID','无法识别实际导入来源')
        command={'entry_id':entry_id,'actor_id':actor.id,'mode':params['mode'],
            'files':[f.manifest() for f in inputs],'preview_tokens':list(params['preview_tokens'])}
        selection={'command_sha256':digest(command)};object_id=selection['command_sha256']
        if entry_id!=VOID:
            actual_branch=source_branch(sources,None if entry_id==PRECHECK else params['mode']) if sources else 'unrecognized_input'
    elif entry_id in (JOBS,BATCHES):
        object_id='generic_import_jobs' if entry_id==JOBS else 'generic_import_batches'
    elif entry_id==JOB:
        state,sources=_job_header(db,params['job_id']);object_id=str(state['id']);actual_branch=source_branch(sources)
    elif entry_id in (BATCH,ERRORS):
        state=_batch_header(db,params['batch_id']);object_id=str(state['id']);actual_branch=state['source_domain']
    elif entry_id==WORKER:
        item=db.execute(select(SysImportJobItem.id,SysImportJobItem.job_id,SysImportJobItem.source_domain,
            SysImportJobItem.mode,SysImportJobItem.content_sha256,SysImportJobItem.content_size,
            SysImportJobItem.input_manifest).where(SysImportJobItem.id==params['item_id'])).mappings().one_or_none()
        if item is None:raise fail(404,'AUTHZ_OBJECT_NOT_FOUND','导入文件任务不存在')
        job,_=_job_header(db,item['job_id'])
        if job['owner_user_id']!=actor.id or job['enqueued_epoch']!=actor.authorization_epoch:
            raise fail(401,'AUTHN_SESSION_INVALID','后台任务主体已变化')
        state={**dict(item),'job':job};object_id=item['id'];actual_branch=source_branch((item['source_domain'],),item['mode'])
    else:raise ValueError('Unknown import entry')
    if branch is not None and branch!=actual_branch:raise fail(409,'AUTHZ_CONTEXT_STALE','导入来源分支已变化')
    ref=contexts.slot_ref(entry_id,'context_0',object_id,selection=selection)
    rule=ENTRY_CONTRACTS[entry_id]['branches'][actual_branch]
    subjects={r:(PolicySubject(object_id,{}),) for r in rule['requirements']}
    ctx=contexts.resolved_context(db,entry_id=entry_id,branch=actual_branch,carrier=ref,
        slots={'context_0':(ref,)},subjects=subjects,
        revision=relation_revision({'entry':entry_id,'branch':actual_branch,'state':state,
            'selection':selection,'actor_id':actor.id,'epoch':actor.authorization_epoch}))
    return create_context_batch(db,[ctx])


def _reference(db,identity,ref,*,entry_id,branch,read_snapshot=None,locked_user=None):
    if entry_id in (BATCH,ERRORS):params={'batch_id':int(ref.id)}
    elif entry_id==JOB:params={'job_id':int(ref.id)}
    elif entry_id==WORKER:params={'item_id':ref.id}
    elif entry_id in (JOBS,BATCHES):params={}
    else:raise fail(403,'AUTHZ_ARTIFACT_STALE','临时上传不能恢复为历史授权')
    return _resolve(db,identity,entry_id=entry_id,params=params,branch=branch,
        read_snapshot=read_snapshot,locked_user=locked_user)


def register():
    for entry in (*HTTP_ENTRIES,WORKER):
        contexts.register_entry_resolver(entry,_resolve,reference_resolver=_reference)


def _admit(accesses,context):
    rule=ENTRY_CONTRACTS[context.entry_id]['branches'][context.branch]
    pairs=[(r,a) for r,aa in context.requirements.items() for a in aa]
    if rule.get('admission_rule'):
        if not any(accesses.get(context.ref,r,a) for r,a in pairs):
            raise fail(403,'AUTHZ_FORBIDDEN','没有此导入操作权限')
    else:
        for resource,action in pairs:accesses.require(context.ref,resource,action)


def _envelope(accesses,ref):
    return {'schema_version':1,'authorization_mode':'unified_modules','catalog_version':CATALOG_VERSION,
            'authorization_context':accesses.wire(ref)}


def _snapshot(factory,identity,entry_id,params,*,precondition=None):
    register()
    return contexts.read_context_snapshot(factory,identity,entry_id=entry_id,params=params)


def _finish_access(accesses,batch,precondition,view):
    if view not in (None,'authorization'):raise fail(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    context=batch.contexts[0]
    # A view may show deny decisions; formal data must satisfy actual admission.
    if view is None:_admit(accesses,context)
    service.check_context_precondition(contexts.decode_precondition(precondition),accesses,context.ref)
    return context.ref


def _status_sources(accesses,ref):
    requirements=accesses._context(ref).requirements
    return [s for s in SOURCES if 'read_status' in requirements.get('import_'+s,()) and accesses.get(ref,'import_'+s,'read_status')]


def _metadata(row):
    keys=('filename','file_type','status','uploaded_at','uploaded_by','rows_total','rows_inserted',
          'rows_skipped','rows_error','rows_inactive')
    return {k:row[k].isoformat() if isinstance(row[k],datetime) else row[k] for k in keys}


def _batch_columns():
    return [getattr(SysImportBatch,k) for k in ('id','source_domain','filename','file_type','status',
        'uploaded_at','uploaded_by','rows_total','rows_inserted','rows_skipped','rows_error','rows_inactive')]


def _job_projection(db,job_id,sources):
    # Scope is in SQL before metadata selection; hidden filenames/results never load.
    columns=[getattr(SysImportJobItem,k) for k in ('id','ordinal','source_domain','filename','status','batch_id','error_code')]
    rows=db.execute(select(*columns).where(SysImportJobItem.job_id==job_id,
        SysImportJobItem.source_domain.in_(sources)).order_by(SysImportJobItem.id)).mappings().all()
    hidden=db.scalar(select(exists().where(SysImportJobItem.job_id==job_id,
        SysImportJobItem.source_domain.not_in(sources))))
    head=db.execute(select(SysImportJob.id,SysImportJob.created_at,SysImportJob.mode,
        *([] if hidden else [SysImportJob.finished_at])).where(SysImportJob.id==job_id)).mappings().one()
    failed=sum(r['status'] in ('failed','authorization_failed') for r in rows)
    done=sum(r['status'] in ('success','partial') for r in rows)
    running=any(r['status'] in ('queued','processing') for r in rows)
    status='processing' if running else 'done' if done==len(rows) else 'failed' if failed==len(rows) else 'partial'
    items=[]
    for row in rows:
        if row['error_code'] not in JOB_ITEM_STATE_CODES.get(row['status'],()):
            raise fail(409,'IMPORT_ORIGIN_UNVERIFIED','导入任务状态无法核验')
        items.append({'item_id':row['id'],'input_index':None if hidden else row['ordinal'],
            'source_domain':row['source_domain'],'filename':row['filename'],'state':row['status'],
            'batch_id':row['batch_id'],'code':row['error_code']})
    return {'job_id':job_id,'created_at':head['created_at'].isoformat(),
        'finished_at':None if hidden or head['finished_at'] is None else head['finished_at'].isoformat(),
        'mode':head['mode'],'partial':bool(hidden),'restricted':bool(hidden),
        'status':None if hidden else status,'total_files':None if hidden else len(rows),
        'done_files':None if hidden else done,'error_files':None if hidden else failed,'note':None,
        'visible_status':status,'visible_total_files':len(rows),'visible_done_files':done,
        'visible_error_files':failed,'items':items}


def read(factory,identity,entry_id,params=None,*,view=None,precondition=None,before_publish=None):
    with _snapshot(factory,identity,entry_id,params or {}) as (db,proof,batch,accesses):
        ref=_finish_access(accesses,batch,precondition,view)
        if view:return service.context_resolution(accesses,ref)
        result=_envelope(accesses,ref)
        sources=_status_sources(accesses,ref)
        if entry_id==BATCHES:
            rows=db.execute(select(*_batch_columns()).where(SysImportBatch.authorization_protocol==PROTOCOL,
                SysImportBatch.owner_user_id.is_not(None),SysImportBatch.source_domain.in_(sources))
                .order_by(SysImportBatch.uploaded_at.desc(),SysImportBatch.id.desc()).limit(100)).mappings().all()
            result.update(items=[{'batch_id':r['id'],'source_domain':r['source_domain'],'metadata':_metadata(r)} for r in rows],
                returned_count=len(rows),total=None,limit=100,restricted=len(sources)<len(SOURCES))
        elif entry_id in (JOBS,JOB):
            if entry_id==JOB:ids=[params['job_id']]
            else:
                ids=list(db.scalars(select(SysImportJob.id).where(SysImportJob.authorization_protocol==PROTOCOL,
                    SysImportJob.owner_user_id.is_not(None),SysImportJob.manifest_digest.is_not(None),
                    exists().where(SysImportJobItem.job_id==SysImportJob.id,SysImportJobItem.source_domain.in_(sources)))
                    .order_by(SysImportJob.created_at.desc(),SysImportJob.id.desc()).limit(50)))
            projections=[_job_projection(db,j,sources) for j in ids]
            if entry_id==JOB:result['job']=projections[0]
            else:result.update(items=projections,returned_count=len(projections),total=None,limit=50,restricted=len(sources)<len(SOURCES))
        elif entry_id==BATCH:
            source=batch.contexts[0].branch;resource='import_'+source
            metadata=None;report=None;errors=[];count=None
            if accesses.get(ref,resource,'read_status'):
                metadata=_metadata(db.execute(select(*_batch_columns()).where(SysImportBatch.id==params['batch_id'])).mappings().one())
            if accesses.get(ref,resource,'read_report'):
                raw=db.scalar(select(SysImportBatch.report_json).where(SysImportBatch.id==params['batch_id']))
                report={k:v for k,v in (raw or {}).items() if not k.startswith('_')}
                count=db.scalar(select(func.count()).select_from(SysImportError).where(SysImportError.batch_id==params['batch_id']))
                rows=db.execute(select(SysImportError.row_no,SysImportError.error_type,SysImportError.error_detail)
                    .where(SysImportError.batch_id==params['batch_id']).order_by(SysImportError.id).limit(500))
                errors=[{'row_no':r.row_no,'nature':'提示' if r.error_type in SOFT_ERROR_TYPES else '错误',
                    'error_type':r.error_type,'detail':r.error_detail} for r in rows]
            result.update(batch_id=params['batch_id'],source_domain=source,metadata=metadata,report=report,
                errors=errors,issue_count=count,block_access={
                    'status':accesses.wire(ref)['resources'][resource]['decisions']['read_status'],
                    'report':accesses.wire(ref)['resources'][resource]['decisions']['read_report']})
        elif entry_id==ERRORS:
            output=io.StringIO(newline='');writer=csv.writer(output);writer.writerow(('行号','性质','问题类型','问题明细'))
            for row in db.execute(select(SysImportError.row_no,SysImportError.error_type,SysImportError.error_detail)
                .where(SysImportError.batch_id==params['batch_id']).order_by(SysImportError.id)):
                writer.writerow([_csv_cell(v) for v in (row.row_no,'提示' if row.error_type in SOFT_ERROR_TYPES else '错误',row.error_type,row.error_detail)])
            content=output.getvalue().encode('utf-8-sig');bind=db.get_bind()
            collector=AccessCollector(accesses.principal.id,accesses.principal.authorization_epoch,CATALOG_VERSION)
            collector.record(accesses.require(ref,'import_'+batch.contexts[0].branch,'export_errors'))
        else:raise ValueError('Unknown history reader')
    if entry_id==ERRORS:
        if before_publish is not None:before_publish()
        return stream_materialized_bytes(content,bind,identity,collector.snapshot(),media_type='text/csv; charset=utf-8',
            headers=attachment_headers(f'import-batch-{params["batch_id"]}-issues.csv'))
    return result

def _csv_cell(value):
    return "'"+value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value


def precheck(factory,identity,inputs,mode,*,view=None,precondition=None):
    params=_input_params(inputs,mode)
    with _snapshot(factory,identity,PRECHECK,params) as (db,proof,batch,accesses):
        ref=_finish_access(accesses,batch,precondition,view)
        if view:return service.context_resolution(accesses,ref)
        budget=content_precheck.new_upsert_budget();pairs=[]
        for f in inputs:
            try:content=content_precheck.inspect_file(f.path,f.filename,mode=mode,budget=budget)
            except ReaderError as exc:content=content_precheck.failed_file_result(f.filename,exc.code,str(exc))
            pairs.append((content,f.content_sha256))
        # Duplicate lookup is a separate actual history capability; its context is
        # a real list, never a borrowed input action or an implied precheck field.
        visible=[]
        if any(f.source_domain for f in inputs):
            history=_resolve(db,identity,entry_id=BATCHES,params={},read_snapshot=proof)
            hs=service.context_accesses(db,identity,history,read_snapshot=proof)
            h_ref=history.contexts[0].ref
            visible=_status_sources(hs,h_ref)
        hashes=[f.content_sha256 for f in inputs if f.source_domain in visible]
        matches={r.file_hash:r.id for r in db.execute(select(SysImportBatch.file_hash,SysImportBatch.id)
            .where(SysImportBatch.file_hash.in_(hashes),SysImportBatch.source_domain.in_(visible),
                SysImportBatch.authorization_protocol==PROTOCOL,SysImportBatch.owner_user_id.is_not(None),SysImportBatch.status=='success'))} if hashes else {}
        content_precheck.apply_exact_success_matches(pairs,matches,mode)
        return {**_envelope(accesses,ref),'content':content_precheck.response([p[0] for p in pairs],mode),
            'inputs':[{'file_index':i,'input_sha256':f.content_sha256,'source_domain':f.source_domain} for i,f in enumerate(inputs)]}


def _hmac_key():
    from app.config import get_settings
    value=get_settings().secret_key.encode()
    if len(value)<16:raise fail(503,'IMPORT_STORAGE_UNAVAILABLE','预演签名配置无效')
    return value


def void_preview(factory,identity,inputs,mode,*,view=None,precondition=None):
    from app.etl import expense_void, mapping
    from app.models.maintenance import FProjectExpense as E
    from app.services import import_void_preview
    from sqlalchemy import or_
    import time
    params=_input_params(inputs,mode)
    with _snapshot(factory,identity,VOID,params) as (db,proof,batch,accesses):
        ref=_finish_access(accesses,batch,precondition,view)
        if view:return service.context_resolution(accesses,ref)
        grant=accesses.require(ref,'import_expense_void','preview')
        allow_amount='data_purchase_cost' in grant.field_permissions
        previews=[]
        for uploaded in inputs:
            base={'filename':uploaded.filename,'file_hash':uploaded.content_sha256,'mode':mode}
            try:
                loaded=pipeline.load_workbook(uploaded.path);pipeline.require_clean_workbook(loaded)
                payload={'status':'not_applicable','file_type':loaded.file_type}
                if mode=='upsert' and loaded.expense_sheets:
                    transformed=pipeline.transform_workbook(loaded).result
                    plan=expense_void.plan_inputs(transformed,mode=mode)
                    payload.update(rows_incoming=len(plan.incoming_ids),dropped_no_contract=plan.dropped_no_contract,
                        contracts=sorted(plan.contracts),anchored=plan.anchored)
                    blocking=expense_void.blocking_errors(transformed)
                    if blocking:payload.update(status='will_be_rejected',blocking_errors=len(blocking),blocking_error_types=sorted(expense_void.iter_error_types(blocking)))
                    elif plan.suppressed_reason:payload.update(status='suppressed',reason=plan.suppressed_reason)
                    elif expense_void.is_armed(plan):
                        conditions=[]
                        if plan.incoming_ids:conditions.append(E.raw_line_id.in_(plan.incoming_ids))
                        contracts=expense_void.scope_contracts(plan)
                        if contracts:conditions.append(E.linked_sales_order_no.in_(contracts))
                        commitment=func.encode(func.sha256(func.convert_to(func.coalesce(E.amount.cast(Text),''),'UTF8')),'hex').label('amount_commitment')
                        columns=[E.raw_line_id,E.data_status,E.linked_sales_order_no,E.bxd_no,E.line_no,
                            E.expense_date,E.person,E.reason,commitment]
                        if allow_amount:columns.append(E.amount)
                        existing={row.raw_line_id:row for row in db.execute(select(*columns).where(or_(*conditions)))} if conditions else {}
                        decision=expense_void.classify(existing,plan)
                        rows=expense_void.void_rows(decision,existing)
                        if not allow_amount:
                            for row in rows:row['amount']=None
                        amount=format(expense_void.void_amount(decision,existing),'f') if allow_amount else None
                        contract=next(iter(plan.contracts))
                        payload.update(status='ready',contract=contract,void={'rows':len(rows),'amount':amount,
                            'already_void_rows':len(decision.already_void_ids)})
                        if len(rows)>pipeline.VOID_PREVIEW_ROW_CAP:
                            payload.update(status='too_large',row_cap=pipeline.VOID_PREVIEW_ROW_CAP)
                        else:
                            fp=expense_void.fingerprint_v2(decision,plan,existing)
                            payload.update(void_rows=rows,preview_token=import_void_preview.issue(file_hash=uploaded.content_sha256,
                                mode=mode,fingerprint=fp,contract=contract,issued_at=int(time.time()),hmac_key=_hmac_key(),version=2),
                                expires_in_seconds=import_void_preview.TOKEN_TTL_SECONDS)
            except ReaderError as exc:payload={'status':'unreadable','code':exc.code,'error':str(exc)}
            previews.append({**base,**payload})
        return {**_envelope(accesses,ref),'fingerprint_version':2,'previews':previews}


def _verify_tokens(inputs,mode,tokens):
    from app.services import import_void_preview
    import time
    hashes={f.content_sha256 for f in inputs};result={}
    try:
        for token in tokens:
            if not token:continue
            payload=import_void_preview.verify(token,hmac_key=_hmac_key(),now=int(time.time()),mode=mode,version=2)
            if payload['file_hash'] not in hashes or not payload['fingerprint'].startswith('v2:'):
                raise ValueError('changed input')
            result[payload['file_hash']]=payload['fingerprint']
    except (ValueError,KeyError,TypeError):
        raise fail(409,'IMPORT_VOID_PREVIEW_STALE','预演已失效，请重新预演') from None
    if mode=='upsert' and len(result)>1:
        raise fail(422,'IMPORT_CONTENT_INVALID','修复模式一次只能导入一个需要作废预演的文件')
    return result


def _current_authorize(db,identity,entry,params,precondition=None):
    actor=service.lock_actor(db,identity)
    batch=_resolve(db,identity,entry_id=entry,params=params,locked_user=actor)
    accesses=service.context_accesses(db,identity,batch,locked_user=actor)
    ref=_finish_access(accesses,batch,precondition,None)
    return accesses,ref,actor


def _refresh(bind,source):
    from app.services import profit,inventory,master_data
    failed=False
    with Session(bind) as db:
        operations=[]
        if source in ('purchase','sales'):operations.append(profit.recompute)
        if source in ('purchase','inventory'):operations.append(inventory.backfill_costs)
        operations.append(master_data.refresh)
        for operation in operations:
            try:operation(db)
            except Exception:
                _LOG.exception('Derived import refresh failed');db.rollback();failed=True
    return 'failed' if failed else 'complete'


def _execute_file(db,identity,uploaded,mode,*,entry=UPLOAD,params,precondition=None,job_id=None,item_id=None):
    """Per-file atomic source write. Domain work finishes before the actor SHARE."""
    from app.etl import loader,expense_void
    from app.services import maintenance_cost
    from sqlalchemy.exc import DataError,IntegrityError
    from app.config import get_settings
    failure=None;batch=None
    # Capture a genuine permit under a separate repeatable-read observation;
    # the write transaction has not acquired any actor lock yet.
    with _snapshot(lambda:Session(db.get_bind()),identity,entry,params) as (snapshot,proof,checked,allowed):
        checked_ref=_finish_access(allowed,checked,precondition,None)
        action_access=allowed.require(checked_ref,'import_'+uploaded.source_domain,'submit_'+mode)
    from app.services.source_import_permit import SourceImportPermit
    if not db.in_transaction():db.begin()
    permit=SourceImportPermit(db,action_access,source=uploaded.source_domain,
        content_sha256=uploaded.content_sha256,mode=mode)
    expected=_verify_tokens((uploaded,),mode,params.get('preview_tokens',())).get(uploaded.content_sha256)
    try:
        # A request may not replace the file after authorization resolution.
        if inspect_input(uploaded.path,uploaded.filename,uploaded.mime_type).manifest()!=uploaded.manifest():
            raise fail(409,'AUTHZ_CONTEXT_STALE','上传内容已变化')
        try:
            batch=pipeline.run_import(db,uploaded.path,uploaded.filename,uploaded_by=identity.username,
                mode=mode,import_job_id=job_id,auto_assign_maintenance_projects=True,
                expected_void_fingerprint=expected,require_void_preview=True,_authorization=permit)
            if uploaded.source_domain in ('purchase','sales','maintenance'):
                maintenance_cost.recompute(db,commit=False)
        except (ReaderError,expense_void.VoidPlanDrift) as exc:
            # This is the established failed-receipt transaction, never a partial
            # successful source write. ETL raises integrity conflicts separately.
            batch=permit.created_batch
            code=('IMPORT_VOID_PREVIEW_STALE' if isinstance(exc,expense_void.VoidPlanDrift) else
                'IMPORT_VOID_PREVIEW_REQUIRED' if exc.code=='void_preview_required' else 'IMPORT_CONTENT_INVALID')
            failure=(409 if code=='IMPORT_VOID_PREVIEW_STALE' else 422,code,'文件未通过导入校验')
        permit.check(db)
        if pipeline.sha256_file(uploaded.path)!=uploaded.content_sha256:
            raise fail(409,'AUTHZ_CONTEXT_STALE','执行期间上传内容已变化')
        if job_id is not None:_verify_job_plan(db,job_id)
        accesses,ref,actor=_current_authorize(db,identity,entry,params,precondition)
        if batch is None:raise fail(500,'IMPORT_EXECUTION_FAILED','未生成导入回执')
        batch.owner_user_id=actor.id;batch.authorization_protocol=PROTOCOL;batch.source_domain=uploaded.source_domain
        if item_id:
            item=db.get(SysImportJobItem,item_id)
            item.batch_id=batch.id;item.status='failed' if failure else batch.status
            item.error_code=failure[1] if failure else None;item.finished_at=datetime.now(timezone.utc)
        db.flush();bid=batch.id;status=batch.status;envelope=_envelope(accesses,ref)
        db.commit()
        if failure:raise fail(*failure,commit_state='receipt_only',batch_id=bid)
        return {**envelope,'batch_id':bid,'source_domain':uploaded.source_domain,'status':status,
                'committed':True,'derived_refresh':_refresh(db.get_bind(),uploaded.source_domain)}
    except pipeline.DuplicateFileError:
        db.rollback();raise fail(409,'IMPORT_DUPLICATE','该文件已成功导入') from None
    except (loader.ImportConcurrencyConflict,maintenance_cost.MaintenanceCostRecomputeBusy,
            maintenance_cost.WorkbookInvalidationConflictError):
        db.rollback();raise fail(409,'IMPORT_CONFLICT','导入期间数据发生变化，本次已回滚') from None
    except (loader.ImportIntegrityError,DataError,IntegrityError):
        db.rollback();raise fail(422,'IMPORT_CONTENT_INVALID','文件未通过完整性校验，本次已回滚') from None
    except pipeline.ArchiveError:
        db.rollback();raise fail(503,'IMPORT_STORAGE_UNAVAILABLE','导入存储暂不可用') from None
    except BaseException as exc:
        db.rollback()
        if isinstance(exc,HTTPException):raise
        _LOG.exception('Canonical import execution failed')
        raise fail(500,'IMPORT_EXECUTION_FAILED','导入处理失败') from None
    # Hash-addressed archives are shared. A rollback never unlinks a published
    # hash path: another import can be waiting on the same advisory lock. The
    # existing archive GC owns orphan cleanup after its age/reference checks.


def submit(db,identity,inputs,mode,*,preview_tokens=(),view=None,precondition=None,batch_upload=False):
    entry=UPLOAD_BATCH if batch_upload else UPLOAD
    params=_input_params(inputs,mode,preview_tokens)
    with _snapshot(lambda:Session(db.get_bind()),identity,entry,params) as (snapshot,proof,contexts_batch,accesses):
        ref=_finish_access(accesses,contexts_batch,precondition,view)
        if view:return service.context_resolution(accesses,ref)
        principal=accesses.principal
    _verify_tokens(inputs,mode,preview_tokens)
    runtime.mark_artifact_protocol_used(db.get_bind(),protocol=PROTOCOL)
    if not batch_upload:
        return _execute_file(db,identity,inputs[0],mode,params=params,precondition=precondition)
    import shutil
    queued=[]
    try:
        for f in inputs:
            fd,path=tempfile.mkstemp(prefix='canonical-import-',suffix='.xlsx');os.close(fd)
            shutil.copyfile(f.path,path);queued.append((f,path))
        job=SysImportJob(created_by=principal.username,mode=mode,total_files=len(inputs),status='processing',
            authorization_protocol=PROTOCOL,enqueued_epoch=principal.authorization_epoch,authorization_generation=1)
        db.add(job);db.flush();manifest=[]
        for i,(f,path) in enumerate(queued):
            item_id=uuid4().hex
            stored={'input':f.manifest(),'path':path,'preview_tokens':list(preview_tokens)}
            row=SysImportJobItem(id=item_id,job_id=job.id,ordinal=i,source_domain=f.source_domain,
                filename=f.filename,content_sha256=f.content_sha256,content_size=f.content_size,mode=mode,
                parser_version=PARSER_VERSION,input_manifest=stored,status='queued')
            db.add(row)
        db.flush()
        manifest=_job_manifest(db,job.id)
        job.manifest_digest=digest(manifest)
        current,ref,actor=_current_authorize(db,identity,entry,params,precondition)
        job.owner_user_id=actor.id;db.flush();jid=job.id
        response={**_envelope(current,ref),'job_id':jid,'status':'processing','accepted_files':len(inputs),'committed':True}
        db.commit()
    except BaseException:
        db.rollback()
        for f,path in queued:
            if os.path.exists(path):os.remove(path)
        raise
    enqueue_job(db.get_bind(),jid)
    return response


def enqueue_job(bind,job_id):
    import threading
    threading.Thread(target=process_job,args=(bind,job_id),daemon=True,name=f'canonical-import-{job_id}').start()


def _job_manifest(db,job_id):
    columns=[getattr(SysImportJobItem,k) for k in ('id','ordinal','source_domain','filename','content_sha256','content_size','mode','parser_version','input_manifest')]
    return [dict(r) for r in db.execute(select(*columns).where(SysImportJobItem.job_id==job_id).order_by(SysImportJobItem.ordinal)).mappings()]


def _verify_job_plan(db,job_id):
    head,_=_job_header(db,job_id)
    if digest(_job_manifest(db,job_id))!=head['manifest_digest']:
        raise fail(409,'AUTHZ_ARTIFACT_STALE','完整导入任务计划已变化')
    return head


def _lock_job_plan(db,job_id):
    # Preserve the source pipeline's global -> domain -> final actor order.
    # Acquiring item rows first would invert a concurrent source import's job FK.
    from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
    db.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': DATA_CHANGE_ADVISORY_LOCK_KEY})
    # All canonical workers acquire the same parent then sorted item locks.
    # This also prevents a future job editor from changing a later item after
    # our final digest check while the current file is committing.
    db.execute(select(SysImportJob.id).where(SysImportJob.id==job_id).with_for_update()).all()
    db.execute(select(SysImportJobItem.id).where(SysImportJobItem.job_id==job_id)
        .order_by(SysImportJobItem.id).with_for_update()).all()


def process_job(bind,job_id):
    """Rehydrate stable owner for every item. Earlier file commits remain intact."""
    with Session(bind) as db:
        head,_=_job_header(db,job_id)
        ids=list(db.scalars(select(SysImportJobItem.id).where(
            SysImportJobItem.job_id==job_id).order_by(SysImportJobItem.ordinal)))
    for item_id in ids:
        stored=None
        with Session(bind) as db:
            try:
                # Stabilize the complete immutable plan for this file, not just
                # the current item. No user lock precedes these domain locks.
                _lock_job_plan(db,job_id)
                current_head=_verify_job_plan(db,job_id)
                identity=resume_task_identity(db,current_head['owner_user_id'],current_head['enqueued_epoch'])
                row=db.get(SysImportJobItem,item_id)
                if row is None or row.status!='queued':continue
                stored=row.input_manifest
                m=stored['input'];uploaded=inspect_input(stored['path'],m['filename'],m['mime_type'])
                if uploaded.manifest()!=m:raise fail(409,'AUTHZ_ARTIFACT_STALE','任务输入已变化')
                params={'item_id':item_id,'preview_tokens':tuple(stored['preview_tokens'])}
                with _snapshot(lambda:Session(bind),identity,WORKER,params) as (snapshot,proof,batch,accesses):
                    _admit(accesses,batch.contexts[0])
                row=db.scalar(select(SysImportJobItem).where(SysImportJobItem.id==item_id)
                    .with_for_update().execution_options(populate_existing=True))
                if row.status!='queued':db.rollback();continue
                row.status='processing';db.flush()
                _execute_file(db,identity,uploaded,row.mode,entry=WORKER,params=params,job_id=job_id,item_id=item_id)
            except HTTPException as exc:
                db.rollback();code=exc.detail.get('code') if isinstance(exc.detail,dict) else None
                if code not in JOB_ITEM_CODES:code='IMPORT_EXECUTION_FAILED'
                row=db.scalar(select(SysImportJobItem).where(SysImportJobItem.id==item_id).with_for_update())
                if row is not None and row.status not in ('success','partial'):
                    row.status='authorization_failed' if code in JOB_ITEM_CODES[7:] else 'failed'
                    row.error_code=code;row.finished_at=datetime.now(timezone.utc);db.commit()
            except Exception:
                db.rollback();_LOG.exception('Import worker failed: %s',job_id)
                row=db.get(SysImportJobItem,item_id)
                if row is not None:
                    row.status='failed';row.error_code='IMPORT_EXECUTION_FAILED';row.finished_at=datetime.now(timezone.utc);db.commit()
            finally:
                if stored and os.path.exists(stored['path']):os.remove(stored['path'])
    with Session(bind) as db:
        job=db.scalar(select(SysImportJob).where(SysImportJob.id==job_id).with_for_update())
        states=list(db.scalars(select(SysImportJobItem.status).where(SysImportJobItem.job_id==job_id)))
        job.done_files=sum(s in ('success','partial') for s in states)
        job.error_files=sum(s in ('failed','authorization_failed') for s in states)
        job.status='done' if job.done_files==len(states) else 'failed' if job.error_files==len(states) else 'partial'
        job.finished_at=datetime.now(timezone.utc);job.note=None;db.commit()
