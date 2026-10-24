"""Actual multi-file plans and current all-target transfer contracts."""
from hashlib import sha256
import json
from pathlib import Path
import os
import tempfile

from sqlalchemy import select, func, Text, Integer, cast, true, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, load_only

from app.authz import service
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, read_snapshot, relation_revision
from app.authz.errors import authz_error
from app.authz.provenance import AccessCollector
from app.models.system import SysImportBatch, SysRawFile
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceCollectionReceipt
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_bulk_import as business
from app.services.maintenance_workflow_authorization import require_complete

BASE = '/api/maintenance/project-batch-transfer'
PREVIEW, APPLY, RULING = ('POST '+BASE+'/'+name for name in ('preview','apply','receipt-rulings'))
OPTIONS, DOWNLOAD = 'GET '+BASE+'/options', 'POST '+BASE+'/download'
RESOURCE, EXPORT = 'maintenance_project_transfer', 'maintenance_project_transfer_export'
KIND, RECEIPT = 'project_transfer_batch', 'project_transfer_apply'
_SEAL = object()
_INPUT_KEYS = ('protocol_version','payload_hash','data_version','token_hash','expires_at','plans','row_map','public','archives','previewed_at','scope_by_row')


def _json(value):
    return json.loads(json.dumps(value, default=str))


def _row(db, batch_id):
    row = db.scalar(batch_projection(select(SysImportBatch).where(SysImportBatch.id==int(batch_id)))
        .execution_options(populate_existing=True))
    if row is None or row.file_type != business.TRANSFER_BATCH_TYPE or row.owner_user_id is None:
        raise artifacts.unavailable()
    return row


def batch_projection(statement):
    return statement.options(load_only(SysImportBatch.id,SysImportBatch.owner_user_id,
        SysImportBatch.file_type,SysImportBatch.status,SysImportBatch.uploaded_by,
        SysImportBatch.rows_total,SysImportBatch.file_hash,SysImportBatch.filename,raiseload=True))


def _selected_report(db,batch_id,selected):
    """Project only selected plans in SQL; keep the complete immutable JSON in DB."""
    if not selected or len(selected)!=len(set(selected)):
        raise business.BulkImportInvalid('必须选择至少一行，且 row_keys 不能重复')
    report=SysImportBatch.report_json
    keys=('token_hash','payload_hash','data_version','expires_at','archives',
          'selected_row_keys','selection_hash','applied_at','applied_by')
    values=db.execute(select(*(report[key].label(key) for key in keys),SysImportBatch.status)
        .where(SysImportBatch.id==batch_id)).one()
    projected={key:getattr(values,key) for key in keys}
    if values.status=='success':
        if sorted(projected['selected_row_keys'] or [])!=sorted(selected):
            raise business.BulkImportConflict('该批次已应用，row_keys 与原应用选择不一致')
        projected['result']=db.scalar(select(report['result']).where(SysImportBatch.id==batch_id))
    mapping=func.jsonb_each(report['row_map']).table_valued('key','value').alias('selected_transfer_map')
    mapped=cast(mapping.c.value,JSONB)
    wrapper=report['plans'].op('->',return_type=JSONB)(cast(mapped['plan_index'].astext,Integer))
    operation=wrapper['plan']['operations'].op('->',return_type=JSONB)(cast(mapped['operation_index'].astext,Integer))
    state=wrapper['plan']['contract_states'].op('->',return_type=JSONB)(operation['project_contract_id'].astext)
    statement=select(mapping.c.key,mapped['plan_index'].astext.label('plan_index'),
        wrapper['file_id'].label('file_id'),wrapper['form_type'].label('form_type'),
        wrapper['plan']['file_hash'].label('file_hash'),operation.label('operation'),state.label('contract_state'))\
        .select_from(SysImportBatch).join(mapping,true()).where(SysImportBatch.id==batch_id,mapping.c.key.in_(selected))
    plans=[]; row_map={}; indexes={}
    for row in db.execute(statement):
        if row.plan_index not in indexes:
            indexes[row.plan_index]=len(plans)
            plans.append({'file_id':row.file_id,'form_type':row.form_type,
                'plan':{'file_hash':row.file_hash,'operations':[],'contract_states':{}}})
        index=indexes[row.plan_index];plan=plans[index]['plan']
        row_map[row.key]={'plan_index':index,'operation_index':len(plan['operations'])}
        plan['operations'].append(row.operation)
        if row.contract_state is not None:
            plan['contract_states'][row.operation['project_contract_id']]=row.contract_state
    if set(row_map)!=set(selected):
        raise business.BulkImportInvalid('包含不可提交、未知或已阻断的 row_key')
    public=func.jsonb_array_elements(report['public']['rows']).table_valued('value').alias('selected_transfer_public')
    value=cast(public.c.value,JSONB)
    rows=list(db.scalars(select(value).select_from(SysImportBatch).join(public,true())
        .where(SysImportBatch.id==batch_id,value['row_key'].astext.in_(selected))))
    if {row['row_key'] for row in rows}!=set(selected):raise artifacts.unavailable()
    return {**projected,'plans':plans,'row_map':row_map,'public':{'rows':rows}}


def _merge_report(db,row,patch):
    db.execute(update(SysImportBatch).where(SysImportBatch.id==row.id)
        .values(report_json=SysImportBatch.report_json.op('||')(cast(_json(patch),JSONB)))
        .execution_options(synchronize_session=False))
    db.expire(row,['report_json'])


def _domain_metadata(db,batch_id):
    report=SysImportBatch.report_json
    immutable=func.jsonb_build_object(*[value for key in _INPUT_KEYS for value in (key,report[key])])
    input_doc=func.jsonb_build_object('id',SysImportBatch.id,'owner',SysImportBatch.owner_user_id,
        'hash',SysImportBatch.file_hash,'filename',SysImportBatch.filename,'input',immutable)
    receipt_doc=func.jsonb_build_object('input',input_doc,'result',report['result'],
        'selected',report['selected_row_keys'],'selection_hash',report['selection_hash'],
        'apply_owner',report['apply_owner_user_id'],'applied_by',report['applied_by'],'applied_at',report['applied_at'])
    def digest(value):return func.encode(func.sha256(func.convert_to(value.cast(Text),'UTF8')),'hex')
    row=db.execute(select(SysImportBatch.id,SysImportBatch.owner_user_id,SysImportBatch.file_type,
        SysImportBatch.status,digest(input_doc).label('input_digest'),digest(receipt_doc).label('receipt_digest'),
        report['apply_owner_user_id'].label('apply_owner'))
        .where(SysImportBatch.id==int(batch_id))).first()
    if row is None or row.file_type!=business.TRANSFER_BATCH_TYPE or row.owner_user_id is None:
        raise artifacts.unavailable()
    return row


def _input_digest(db,row):
    return _domain_metadata(db,row.id).input_digest


def _state(db, target):
    row=_domain_metadata(db,target.id)
    if target.generation!=1:raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(target,ObjectRef(KIND,str(row.id)),row.input_digest,row.owner_user_id)


def _receipt_state(db,target):
    row=_domain_metadata(db,target.id)
    if target.generation!=1 or row.status!='success' or type(row.apply_owner) is not int:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(target,ObjectRef(KIND,str(row.id)),row.receipt_digest,row.apply_owner)


artifacts.register_artifact_domain(KIND, artifacts.ArtifactDomainAdapter(_state, None, (), private=False))
artifacts.register_artifact_domain(RECEIPT, artifacts.ArtifactDomainAdapter(_receipt_state, None, (), private=False))


def _verify(db, row, *, receipt=False):
    artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget(KIND, str(row.id), 1))
    if receipt:
        artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget(RECEIPT, str(row.id), 1))
    archives=db.scalar(select(SysImportBatch.report_json['archives']).where(SysImportBatch.id==row.id)) or []
    for archive in archives:
        business.pipeline.verify_archive(archive['storage_path'], archive['file_hash'])


def _targets(db, material):
    projects, contracts = set(), set()
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('project_id','matched_project_id') and item:
                    projects.add(str(item))
                elif key in ('project_contract_id','matched_contract_id') and item:
                    contracts.add(str(item))
                walk(item)
        elif isinstance(value, (tuple,list)):
            for item in value:
                walk(item)
    walk(material)
    current = [list(row) for row in db.execute(select(MaintenanceProjectContract.project_contract_id,
        MaintenanceProjectContract.project_id, MaintenanceProjectContract.version)
        .where(MaintenanceProjectContract.project_contract_id.in_(sorted(contracts)))
        .order_by(MaintenanceProjectContract.project_contract_id))]
    projects.update(row[1] for row in current)
    return sorted(projects), current


def _discover_preview(db, files):
    """Resolve upload identities using only keys and relationship columns."""
    from app.models.maintenance import FMaintenanceOrder
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from app.models.sales import FSalesOrder
    norms, raw_ids, hashes = set(), set(), []
    for filename, content in files:
        if not filename.lower().endswith('.xlsx'):
            raise business.BulkImportInvalid('只接受 .xlsx 文件')
        _, sheet = business._detect(content)
        hashes.append(sha256(content).hexdigest())
        for _, values in sheet.rows:
            norms.add(business.normalize_order_no(business._value(sheet,values,'order_no')))
            raw_id=business._text(business._value(sheet,values,'raw_order_id'))
            if raw_id:raw_ids.add(raw_id)
    sales=[list(row) for row in db.execute(select(FSalesOrder.id,FSalesOrder.raw_order_id,FSalesOrder.order_no).where(FSalesOrder.raw_order_id.in_(raw_ids)))]
    norms.update(business.normalize_order_no(row[2]) for row in sales)
    contracts=[list(row) for row in db.execute(select(MaintenanceProjectContract.project_contract_id,
        MaintenanceProjectContract.project_id,MaintenanceProjectContract.contract_no))
        if business.normalize_order_no(row.contract_no) in norms]
    assignments=[list(row) for row in db.execute(select(FMaintenanceOrder.linked_sales_order_no,
        MaintenanceSourceOrderAssignment.project_id).join(MaintenanceSourceOrderAssignment,
        MaintenanceSourceOrderAssignment.source_order_id==FMaintenanceOrder.raw_order_id))
        if business.normalize_order_no(row.linked_sales_order_no) in norms]
    codes=[list(row) for row in db.execute(select(MaintenanceProject.project_id,MaintenanceProject.project_code))
        if business.normalize_order_no(row.project_code) in norms]
    projects=sorted({row[1] for row in contracts}|{row[1] for row in assignments}|{row[0] for row in codes})
    resolved={business.normalize_order_no(row[2]) for row in contracts}|{
        business.normalize_order_no(row[0]) for row in assignments}|{
        business.normalize_order_no(row[1]) for row in codes}
    return {'targets':[{'project_id':pid} for pid in projects], 'hashes':hashes,
        'unresolved':bool(norms-resolved) or not norms, 'contracts':contracts,
        'assignments':assignments,'codes':codes,'sales':sales, 'plans':[]}


def _prepare_authorized_preview(db,identity,params,*,proof):
    from sqlalchemy import event
    from sqlalchemy.orm import load_only
    from app.models.sales import FSalesOrder
    discovery=_discover_preview(db,params['files'])
    admission=_context(db,identity,entry_id=PREVIEW,params=params,read_snapshot=proof,material=discovery)
    require_complete(service.context_accesses(db,identity,admission,read_snapshot=proof))
    allowed_projects={row['project_id'] for row in discovery['targets']}
    allowed_contracts={row[0] for row in discovery['contracts']}
    def project_reads(state):
        # The existing planner stays authoritative. Limit its ORM facts to the
        # actual input targets already authorized in this same RR snapshot.
        if not state.is_select:return
        for description in state.statement.column_descriptions:
            entity=description.get('expr')
            if entity is FSalesOrder:
                state.statement=state.statement.options(load_only(FSalesOrder.id,FSalesOrder.raw_order_id,
                    FSalesOrder.order_no,FSalesOrder.data_status,FSalesOrder.amount_ex_tax,
                    FSalesOrder.tax_rate,FSalesOrder.order_date,raiseload=True))
            elif entity is MaintenanceProjectContract:
                state.statement=state.statement.where(MaintenanceProjectContract.project_contract_id.in_(allowed_contracts))
            elif entity is MaintenanceProject:
                state.statement=state.statement.where(MaintenanceProject.project_id.in_(allowed_projects))
    event.listen(db,'do_orm_execute',project_reads)
    try:
        prepared=business.prepare_transfer(db,params['files'],operated_by='authorization-plan')
    finally:event.remove(db,'do_orm_execute',project_reads)
    material={'plans':prepared['plans'],'public':prepared['public_payload'],
        'payload_hash':prepared['payload_hash'],'data_version':prepared['data_version'],'hashes':prepared['hashes'],
        'discovery':discovery}
    return material,prepared


def _material(db, entry_id, params):
    if entry_id == PREVIEW:
        prepared = business.prepare_transfer(db, params['files'], operated_by='authorization-plan')
        return {'plans':prepared['plans'], 'public':prepared['public_payload'], 'payload_hash':prepared['payload_hash'],
                'data_version':prepared['data_version'], 'hashes':prepared['hashes']}
    if entry_id == APPLY:
        row = _row(db, business._batch_id_from_transfer_token(params['preview_token'])); _verify(db, row, receipt=row.status=='success')
        selected = params['row_keys']
        report = _selected_report(db,row.id,selected)
        rows = {item['row_key']:item for item in report.get('public',{}).get('rows',[])}
        mapping = report.get('row_map',{})
        if len(selected) != len(set(selected)) or any(key not in mapping for key in selected):
            raise business.BulkImportInvalid('包含不可提交、未知或重复的 row_key')
        plans = report.get('plans',[])
        operations = [plans[mapping[key]['plan_index']]['plan']['operations'][mapping[key]['operation_index']] for key in selected]
        return {'batch_id':row.id, 'owner':row.owner_user_id, 'input':_input_digest(db,row),
            'status':row.status, 'operations':operations, 'rows':[rows[key] for key in selected],
            'result':report.get('result') if row.status=='success' else None}
    if entry_id == RULING:
        norm = business.normalize_order_no(params['contract_no'])
        row = db.scalar(select(MaintenanceCollectionReceipt).where(MaintenanceCollectionReceipt.contract_no==norm,
            MaintenanceCollectionReceipt.receipt_no==params['receipt_no'], MaintenanceCollectionReceipt.is_active.is_(True)))
        if row is None:
            raise business.BulkImportNotFound('生效收款单台账行不存在')
        return {'receipt':business._receipt_dict(row), 'receipt_id':row.id,
            'project_contract_id':row.project_contract_id,
            'project_id':db.scalar(select(MaintenanceProjectContract.project_id).where(
                MaintenanceProjectContract.project_contract_id==row.project_contract_id))}
    raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','未知传输命令')


def _admit_apply(db,identity,batch_id,params,proof):
    meta=_domain_metadata(db,batch_id)
    artifacts.load_artifact_binding(db,target=artifacts.ArtifactTarget(KIND,str(batch_id),1))
    scope=db.scalar(select(SysImportBatch.report_json['scope_by_row']).where(SysImportBatch.id==batch_id))
    selected=params['row_keys']
    if not isinstance(scope,dict) or not selected or any(key not in scope for key in selected):
        raise artifacts.unavailable()
    targets=[target for key in selected for target in scope[key]]
    admission=_context(db,identity,entry_id=APPLY,params=params,read_snapshot=proof,
        material={'batch_id':batch_id,'owner':meta.owner_user_id,'targets':targets,'operations':[],
            'unresolved':any(item.get('unresolved') for item in targets)})
    require_complete(service.context_accesses(db,identity,admission,read_snapshot=proof))


def _context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None,
             principal=None, material=None):
    if branch not in (None,'default'):
        raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','未知传输分支')
    principal = principal or foundation._named_principal(db,identity,read_snapshot=read_snapshot,locked_user=locked_user)
    if material is None and entry_id==PREVIEW:
        material,_=_prepare_authorized_preview(db,identity,params,proof=read_snapshot)
    if material is None and entry_id==APPLY and read_snapshot is not None:
        batch_id=business._batch_id_from_transfer_token(params['preview_token'])
        _admit_apply(db,identity,batch_id,params,read_snapshot)
    if material is None and entry_id==RULING and read_snapshot is not None:
        metadata=db.execute(select(MaintenanceCollectionReceipt.id,
            MaintenanceCollectionReceipt.project_contract_id,MaintenanceProjectContract.project_id)
            .join(MaintenanceProjectContract,MaintenanceProjectContract.project_contract_id==MaintenanceCollectionReceipt.project_contract_id)
            .where(MaintenanceCollectionReceipt.contract_no==business.normalize_order_no(params['contract_no']),
                MaintenanceCollectionReceipt.receipt_no==params['receipt_no'],MaintenanceCollectionReceipt.is_active.is_(True))).first()
        if metadata is None:raise business.BulkImportNotFound('生效收款单台账行不存在')
        admission=_context(db,identity,entry_id=entry_id,params=params,read_snapshot=read_snapshot,
            material={'receipt_id':metadata.id,'project_contract_id':metadata.project_contract_id,'project_id':metadata.project_id})
        require_complete(service.context_accesses(db,identity,admission,read_snapshot=read_snapshot))
    material = material if material is not None else _material(db,entry_id,params)
    project_ids, contracts = _targets(db,material)
    evidence = foundation.project_evidence(db,principal,project_ids)
    digest = relation_revision(_json(material))
    if entry_id == PREVIEW:
        selection={'file_sha256':business._batch_file_hash(material['hashes']), 'command_plan_sha256':digest}
        object_id=relation_revision({'actor':principal.id,'entry':entry_id,'input':digest})
    else:
        selection=_json(params)
        object_id=str(material['batch_id'] if entry_id==APPLY else material['receipt_id'])
    ref=foundation.slot_ref(entry_id,'context_0',object_id,selection=selection)
    subjects=[evidence[pid].subject for pid in project_ids]
    operations = (material.get('operations',[]) if entry_id==APPLY else
        [item for wrapper in material.get('plans',[]) for item in wrapper['plan'].get('operations',[])])
    # Actual unmatched/new-project input is globally scoped. It cannot be made
    # vacuously owned by omitting a missing project ID from the target set.
    if material.get('unresolved') or not subjects or any(not item.get('project_id') or item.get('action')=='create_project' for item in operations):
        subjects.append(PolicySubject(ref.id,dict.fromkeys(('is_manager','is_viewer','is_sales','is_active'),False)))
    context=foundation.resolved_context(db,entry_id=entry_id,branch='default',carrier=ref,
        slots={'context_0':(ref,)},subjects={RESOURCE:tuple(subjects)},
        revision=relation_revision({'input':digest,'selection':ref.to_dict(),'contracts':contracts,
            'projects':{pid:e.authorization_revision for pid,e in evidence.items()},
            'actor':[principal.id,principal.authorization_epoch]}))
    return create_context_batch(db,[context])


for entry in (PREVIEW,APPLY,RULING):
    foundation.register_entry_resolver(entry,_context)


def read(db,identity,*,entry_id,params,legacy_context,view=None,precondition=None):
    if view not in (None,'authorization'):
        raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    from app.services import maintenance_transfer_options as options
    from app.services import maintenance_project_export as exporter
    from app.services.authorization_downloads import stream_materialized_bytes
    with foundation.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry_id,params=params) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        access=accesses.require(ref,EXPORT,'read_options' if entry_id==OPTIONS else 'download')
        service.check_context_precondition(foundation.decode_precondition(precondition),accesses,ref)
        fields=service.field_context(access,legacy_context)
        if entry_id==OPTIONS:
            return {**options._options_payload(fields,can_import=False),'authorization_context':accesses.wire(ref)}
        forms,field_options,_,_=options._download_options(fields)
        allowed_forms={item['key'] for item in forms};by_key={item['key']:item for item in field_options}
        if (not set(params['forms'])<=allowed_forms or not set(params['fields'])<=set(by_key)
                or any(not set(params['forms']).intersection(by_key[key]['form_keys']) for key in params['fields'])):
            raise authz_error(403,'AUTHZ_FORBIDDEN','所选表单或字段不在当前导出范围')
        content,count=exporter.build_project_export(snapshot,user_ctx=fields,field_keys=params['fields'],
            q_text=params.get('q'),lifecycle=params.get('lifecycle','all'),card_status=params.get('card_status'),
            sort=params.get('sort','name'),business_type=params.get('business_type','all'),
            allowed_project_ids=foundation.project_query(access,include_unassigned_when_all=True),strict_projection=True)
        collector=AccessCollector(access.principal.id,access.authorization_epoch,access.catalog_version)
        collector.record(access);provenance=collector.snapshot()
    return stream_materialized_bytes(content,db.get_bind(),identity,provenance,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition':'attachment; filename="maintenance-projects.xlsx"','X-Export-Row-Count':str(count),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


def _authorization_relationships(db,project_ids):
    from app.models.maintenance_project import MaintenanceProjectUserAssignment as Assignment
    return relation_revision(_json({
        'projects':[list(row) for row in db.execute(select(MaintenanceProject.project_id,MaintenanceProject.is_active,
            MaintenanceProject.salesperson,MaintenanceProject.salesperson_override_active)
            .where(MaintenanceProject.project_id.in_(project_ids)).order_by(MaintenanceProject.project_id))],
        'assignments':[list(row) for row in db.execute(select(Assignment.assignment_id,Assignment.project_id,
            Assignment.user_id,Assignment.responsibility_type,Assignment.version,Assignment.archived_at)
            .where(Assignment.project_id.in_(project_ids)).order_by(Assignment.assignment_id))]}))


class TransferAuthorization:
    """Sealed command permit; actor locking is the final authorization step."""
    def __init__(self, identity, principal, entry_id, params, initial, precondition, preview_material=None, prepared=None):
        self.preview_material,self.prepared=preview_material,prepared
        self.identity, self.principal, self.entry_id, self.params = identity, principal, entry_id, params
        self.initial, self.precondition = initial, precondition
        self._seal, self.transaction, self.captured, self.material = _SEAL, None, None, None
        self.actor, self.owned_paths, self.result_batch_id = None, [], None

    def capture(self, db):
        if self._seal is not _SEAL or self.transaction is not None:
            raise ValueError('传输授权不可复用')
        self.material = self.preview_material if self.entry_id==PREVIEW else _material(db,self.entry_id,self.params)
        self.captured = _context(db,self.identity,entry_id=self.entry_id,params=self.params,
            principal=self.principal,material=self.material)
        if self.captured.contexts != self.initial.contexts:
            raise authz_error(409,'AUTHZ_CONTEXT_STALE','批量传输目标已变化，请重新预览')
        self.original_projects,self.original_contracts=_targets(db,self.material)
        self.relationships=_authorization_relationships(db,self.original_projects)
        self.transaction = db.get_transaction()

    def _finish(self, db, *, additional=None):
        if self._seal is not _SEAL or self.transaction is not db.get_transaction() or self.captured is None:
            raise ValueError('传输授权必须与业务写入在同一事务')
        # Match the domain project -> contract order. Never hold a contract
        # while waiting for a newly discovered parent project.
        material = dict(self.material)
        if additional is not None:
            material['actual_result'] = additional
        project_ids, contracts = _targets(db,material)
        list(db.scalars(select(MaintenanceProject.project_id).where(MaintenanceProject.project_id.in_(project_ids))
            .order_by(MaintenanceProject.project_id).with_for_update(read=True)))
        if contracts:
            list(db.scalars(select(MaintenanceProjectContract.project_contract_id).where(
                MaintenanceProjectContract.project_contract_id.in_([r[0] for r in contracts]))
                .order_by(MaintenanceProjectContract.project_contract_id).with_for_update(read=True)))
        self.actor = service.lock_actor(db,self.identity)
        current_projects,_ = _targets(db,material)
        if not set(current_projects)<=set(project_ids):
            raise authz_error(409,'AUTHZ_CONTEXT_STALE','等待领域锁时合同归属已变化')
        current = _context(db,self.identity,entry_id=self.entry_id,params=self.params,
            locked_user=self.actor,material=material)
        require_complete(service.context_accesses(db,self.identity,current,locked_user=self.actor))
        original = service.context_accesses(db,self.identity,self.captured,locked_user=self.actor)
        require_complete(original)
        current_contracts={row[0]:row[1] for row in _targets(db,self.material)[1]}
        if (any(current_contracts.get(row[0])!=row[1] for row in self.original_contracts)
                or _authorization_relationships(db,self.original_projects)!=self.relationships):
            raise authz_error(409,'AUTHZ_CONTEXT_STALE','锁等待后批量传输授权关系已变化')
        if self.entry_id in (PREVIEW,RULING):
            locked_context=_context(db,self.identity,entry_id=self.entry_id,params=self.params,
                locked_user=self.actor,material=self.material)
            if locked_context.contexts != self.captured.contexts:
                raise authz_error(409,'AUTHZ_CONTEXT_STALE','锁等待后批量传输原目标关系已变化')
        service.check_context_precondition(self.precondition,original,self.captured.contexts[0].ref)

    def prepare_preview(self, db, files):
        self.capture(db)
        prepared = self.prepared
        if prepared['payload_hash'] != self.material['payload_hash']:
            raise authz_error(409,'AUTHZ_CONTEXT_STALE','批量预览计划已变化')
        self._finish(db)
        return prepared

    def archive_bytes(self, content, digest):
        if self.actor is None or sha256(content).hexdigest() != digest:
            raise artifacts.unavailable()
        from app.config import get_settings
        directory=Path(get_settings().raw_file_dir);directory.mkdir(parents=True,exist_ok=True)
        descriptor,path=tempfile.mkstemp(prefix='project-transfer-',suffix='.xlsx',dir=directory)
        self.owned_paths.append(path)
        with os.fdopen(descriptor,'wb') as output:
            output.write(content);output.flush();os.fsync(output.fileno())
        business.pipeline.verify_archive(path,digest)
        return path

    def publish_preview(self,db,row):
        if self.actor is None or self.transaction is not db.get_transaction():
            raise ValueError('预览发布缺少最终授权')
        self.result_batch_id=row.id
        report=row.report_json;scope={}
        public={item['row_key']:item for item in report['public']['rows']}
        for key,mapping in report['row_map'].items():
            operation=report['plans'][mapping['plan_index']]['plan']['operations'][mapping['operation_index']]
            projects,contracts=_targets(db,{'operation':operation,'row':public[key]})
            scope[key]=([{'project_id':pid} for pid in projects]+
                [{'project_contract_id':item[0],'project_id':item[1]} for item in contracts])
            # An actual new-project row has no persisted project yet.
            if not projects:scope[key].append({'unresolved':True})
        row.report_json={**report,'scope_by_row':scope}
        db.flush()
        artifacts.register_artifact_binding(db,self.identity,
            target=artifacts.ArtifactTarget(KIND,str(row.id),1),input_digest=_input_digest(db,row),locked_user=self.actor)

    def projected_report(self,db,row):
        if row.id!=self.material['batch_id']:
            # A selection-hash replay points at another actual durable batch;
            # authorize its targets before reading any receipt facts.
            with read_snapshot(lambda:Session(db.get_bind())) as (snapshot,proof):
                _admit_apply(snapshot,self.identity,row.id,self.params,proof)
        _verify(db,row,receipt=row.status=='success')
        return _selected_report(db,row.id,self.params['row_keys'])

    def save_apply_report(self,db,row,report):
        _merge_report(db,row,{key:report[key] for key in
            ('result','selected_row_keys','selection_hash','applied_at','applied_by')})

    def finish_apply(self,db,row,*,report,replay=False):
        db.flush()
        _verify(db,row,receipt=replay)
        self._finish(db,additional={'batch_id':row.id,'result':report.get('result')})
        if not replay:
            _merge_report(db,row,{'apply_owner_user_id':self.principal.id})
            db.flush()
            target=artifacts.ArtifactTarget(RECEIPT,str(row.id),1)
            artifacts.register_artifact_binding(db,self.identity,target=target,
                input_digest=_receipt_state(db,target).input_digest,locked_user=self.actor)

    def cleanup(self,bind):
        if not self.owned_paths:return
        try:
            with Session(bind) as check:
                # Previews deliberately do not yet have sys_raw_file rows.
                if self.result_batch_id and check.get(SysImportBatch,self.result_batch_id) is not None:return
            for path in self.owned_paths:Path(path).unlink(missing_ok=True)
        except Exception:
            return


def execute(db,identity,*,entry_id,params,view=None,precondition=None):
    if view not in (None,'authorization'):
        raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')
    parsed=foundation.decode_precondition(precondition)
    with read_snapshot(lambda:Session(db.get_bind())) as (snapshot,proof):
        preview_material,prepared=(_prepare_authorized_preview(snapshot,identity,params,proof=proof)
            if entry_id==PREVIEW else (None,None))
        initial=_context(snapshot,identity,entry_id=entry_id,params=params,read_snapshot=proof,material=preview_material)
        accesses=service.context_accesses(snapshot,identity,initial,read_snapshot=proof)
        require_complete(accesses)
        if view=='authorization':return service.context_resolution(accesses,initial.contexts[0].ref)
        service.check_context_precondition(parsed,accesses,initial.contexts[0].ref)
        principal=accesses.principal
    db.rollback();artifacts.begin_artifact_protocol(db.get_bind())
    permit=TransferAuthorization(identity,principal,entry_id,params,initial,parsed,preview_material,prepared)
    try:
        if entry_id==PREVIEW:
            result=business.preview_transfer(db,params['files'],operated_by=principal.username,_authorization=permit)
        elif entry_id==APPLY:
            result=business.apply_transfer(db,**params,operated_by=principal.username,_authorization=permit)
        else:
            # Same ledger mutex as the domain primitive, before capturing its
            # actual original receipt and before any actor lock.
            receipt=_material(db,entry_id,params)
            business._advisory_lock(db,f"maintenance-receipt-ledger:{receipt['project_contract_id']}")
            permit.capture(db)
            result=business.rule_receipt(db,**params,operated_by=principal.username)
            permit._finish(db,additional=result)
        db.commit();return result
    except BaseException:
        db.rollback();permit.cleanup(db.get_bind());raise
