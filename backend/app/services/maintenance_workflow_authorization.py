"""Actual workflow carriers and lifecycle authorization for maintenance imports.

The shared foundation owns project relationship evidence and policy execution.
This module binds each workflow's real rows, immutable input and owner to it.
"""
from sqlalchemy import select, func, cast, Text
from sqlalchemy.dialects.postgresql import JSONPATH, JSONB, aggregate_order_by
from sqlalchemy.orm import Session, load_only

from app.authz import runtime, service
from app.authz.context import ObjectRef, create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.authz.provenance import AccessCollector
from app.models.maintenance_acceptance_checklist import MaintenanceAcceptanceChecklistBatch, MaintenanceAcceptanceChecklistItem
from app.models.maintenance_project import MaintenanceProject
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation

CHECKLIST_APPLY = 'POST /api/maintenance/acceptance-checklist/{batch_id}/apply'
CHECKLIST_PREVIEW = 'POST /api/maintenance/projects/stable/{project_id}/acceptance/checklist/preview'
def multipart_precondition(query_value, form_value):
    """One normalized CAS value; a second transport cannot silently override it."""
    if form_value is not None and type(form_value) is not str:
        raise authz_error(422,'AUTHZ_INVALID_REQUEST','multipart授权前置条件必须为JSON字符串')
    query=foundation.decode_precondition(query_value)
    form=foundation.decode_precondition(form_value)
    if query is not None and form is not None and query!=form:
        raise authz_error(422,'AUTHZ_INVALID_REQUEST','授权前置条件重复且不一致')
    return form if form is not None else query


def require_complete(accesses):
    """A domain command's complete resource/action contract is conjunctive."""
    for context in accesses.batch.contexts:
        for resource, actions in context.requirements.items():
            for action in actions:
                accesses.require(context.ref, resource, action)
    return accesses


def captured_provenance(accesses):
    collector = AccessCollector(accesses.principal.id, accesses.principal.authorization_epoch,
                                service.CATALOG_VERSION)
    for access in accesses.accesses.values():
        collector.record(access)
    return collector.snapshot()


def lock_project(db, params):
    project = db.scalar(select(MaintenanceProject).options(load_only(MaintenanceProject.project_id,raiseload=True)).where(
        MaintenanceProject.project_id == params['project_id']).with_for_update()
        .execution_options(populate_existing=True))
    if project is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '维保项目不存在')
    return project


def lock_checklist(db, params):
    project_id = db.scalar(select(MaintenanceAcceptanceChecklistBatch.project_id).where(
        MaintenanceAcceptanceChecklistBatch.batch_id == params['batch_id']))
    if project_id is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '清单批次不存在')
    lock_project(db, {'project_id': project_id})
    row = db.scalar(select(MaintenanceAcceptanceChecklistBatch).options(load_only(
        MaintenanceAcceptanceChecklistBatch.batch_id,MaintenanceAcceptanceChecklistBatch.project_id,raiseload=True)).where(
        MaintenanceAcceptanceChecklistBatch.batch_id == params['batch_id']).with_for_update()
        .execution_options(populate_existing=True))
    if row is None or row.project_id != project_id:
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '清单项目归属已变化')
    return row


def _checklist_header(db,batch_id):
    model=MaintenanceAcceptanceChecklistBatch
    item=MaintenanceAcceptanceChecklistItem
    item_value=func.jsonb_build_object(*[value for column in item.__table__.columns
        for value in (column.name,column)])
    items=select(func.coalesce(func.jsonb_agg(aggregate_order_by(item_value,item.row_no,item.item_id)),
        cast('[]',JSONB))).where(item.batch_id==model.batch_id).correlate(model).scalar_subquery()
    # Status, failure report and replacement links change legitimately at apply.
    # The actual parsed input rows and every immutable batch field do not.
    value=func.jsonb_build_object('batch_id',model.batch_id,'project_id',model.project_id,
        'owner_user_id',model.owner_user_id,'file_hash',model.file_hash,'filename',model.filename,
        'idempotency_key',model.idempotency_key,'uploaded_by',model.uploaded_by,
        'uploaded_at',model.uploaded_at,'item_rows',model.item_rows,'issue_rows',model.issue_rows,'items',items)
    return db.execute(select(model.batch_id,model.project_id,model.owner_user_id,model.file_hash,model.status,
        _opaque_json_digest(value).label('input_digest')).where(model.batch_id==batch_id)).first()


def checklist_input_digest(db,batch_id):
    row=_checklist_header(db,batch_id)
    if row is None:
        raise artifacts.unavailable()
    return row.input_digest


def _checklist_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    row = _checklist_header(db,params['batch_id'])
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '清单批次不存在')
    principal, projects = foundation.resolve_project_subjects(db, identity, [row.project_id],
        read_snapshot=read_snapshot, locked_user=locked_user)
    # Owner remains an independent AND even when the resource scope is all.
    if row.owner_user_id is None or row.owner_user_id != principal.id:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '只能应用本人上传的清单批次；历史批次请重新上传')
    evidence = projects[row.project_id]
    carrier = foundation.slot_ref(entry_id, 'context_0', row.batch_id)
    requirements = ENTRY_CONTRACTS[entry_id]['branches']['default']['requirements']
    context = foundation.resolved_context(db, entry_id=entry_id, branch='default', carrier=carrier,
        slots={'context_0': (carrier,)}, subjects={r: (evidence.subject,) for r in requirements},
        revision=relation_revision({'batch': [row.batch_id, row.project_id, row.owner_user_id,
            row.input_digest, row.status], 'project': evidence.authorization_revision}))
    return create_context_batch(db, [context])


def _checklist_reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    return _checklist_context(db, identity, entry_id=entry_id, params={'batch_id': ref.id}, branch=branch,
                             read_snapshot=read_snapshot, locked_user=locked_user)


def _checklist_state(db, target):
    row = _checklist_header(db,target.id)
    if row is None or row.owner_user_id is None:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(artifacts.ArtifactTarget(target.kind, row.batch_id, 1),
        ObjectRef('acceptance_checklist_batch', row.batch_id), row.input_digest, row.owner_user_id)


foundation.register_entry_resolver(CHECKLIST_APPLY, _checklist_context,
                                   reference_resolver=_checklist_reference)
artifacts.register_artifact_domain('acceptance_checklist_batch',
    artifacts.ArtifactDomainAdapter(_checklist_state, None, ()))


def command_authorizer(identity, *, entry_id, params, authorization_precondition=None, branch=None):
    """Callback for an existing domain primitive, after its complete lock set."""
    def authorize(db):
        actor = service.lock_actor(db, identity)
        batch = foundation.resolve_entry_context(db, identity, entry_id=entry_id,
            params=params, branch=branch, locked_user=actor)
        accesses = service.context_accesses(db, identity, batch, locked_user=actor)
        require_complete(accesses)
        if len(batch.contexts) != 1:
            raise ValueError('This command requires a single registered carrier')
        service.check_context_precondition(authorization_precondition, accesses, batch.contexts[0].ref)
        return accesses.principal
    return authorize


LEDGER_PREVIEW = 'POST /api/maintenance/ledger-imports/preview'
LEDGER_READ = 'GET /api/maintenance/ledger-imports/{batch_id}'
LEDGER_APPLY = 'POST /api/maintenance/ledger-imports/{batch_id}/apply'


def input_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    """Only registered source preview commands mint this transient context."""
    from hashlib import sha256
    from app.authz.context import PolicySubject, canonical_json
    from app.authz.catalog import get_resource_definition
    if entry_id != LEDGER_PREVIEW or params.get('doc_type') != 'ledger':
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '未登记的上传输入上下文')
    digest = params.get('input_sha256')
    if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '上传输入摘要无效')
    principal, _ = foundation.resolve_project_subjects(db, identity, [],
        read_snapshot=read_snapshot, locked_user=locked_user)
    selection = {'input_sha256': digest, 'doc_type': 'ledger'}
    operation_id = sha256(canonical_json({'entry_id': entry_id, 'actor': principal.id,
        'epoch': principal.authorization_epoch, 'selection': selection}).encode()).hexdigest()
    carrier = foundation.slot_ref(entry_id, 'context_0', operation_id, selection=selection)
    rule = ENTRY_CONTRACTS[entry_id]['branches']['default']
    subjects = {}
    for resource in rule['requirements']:
        attrs = get_resource_definition(resource).query_attributes
        subjects[resource] = (PolicySubject(operation_id,
            {'is_owner': True} if attrs else {}),)
    return create_context_batch(db, [foundation.resolved_context(db, entry_id=entry_id,
        branch='default', carrier=carrier, slots={'context_0': (carrier,)}, subjects=subjects,
        revision=relation_revision({'input': carrier.to_dict(), 'actor': principal.id,
                                   'epoch': principal.authorization_epoch}))])


def _ledger_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    from app.authz.context import PolicySubject
    from app.models.maintenance_ledger import MaintenanceLedgerImportBatch
    model = MaintenanceLedgerImportBatch
    row = db.execute(select(model.batch_id, model.owner_user_id, model.file_hash,
        model.status, model.applied_at).where(model.batch_id == params['batch_id'])).first()
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '台账批次不存在')
    if row.owner_user_id is None:
        raise artifacts.unavailable()
    principal, _ = foundation.resolve_project_subjects(db, identity, [],
        read_snapshot=read_snapshot, locked_user=locked_user)
    carrier = foundation.slot_ref(entry_id, 'context_0', row.batch_id)
    context = foundation.resolved_context(db, entry_id=entry_id, branch='default', carrier=carrier,
        slots={'context_0': (carrier,)}, subjects={'maintenance_ledger_import': (
            PolicySubject(row.batch_id, {'is_owner': row.owner_user_id == principal.id}),)},
        revision=relation_revision({'batch': [row.batch_id, row.owner_user_id, row.file_hash,
            row.status, row.applied_at.isoformat() if row.applied_at else None],
            'actor': [principal.id, principal.authorization_epoch]}))
    return create_context_batch(db, [context])


def _ledger_reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    return _ledger_context(db, identity, entry_id=entry_id, params={'batch_id': ref.id}, branch=branch,
                           read_snapshot=read_snapshot, locked_user=locked_user)


def ledger_input_digest(db, row):
    import hashlib
    import json
    from app.models.maintenance_ledger import (MaintenanceLedgerContractRow,
        MaintenanceLedgerPlanRow, MaintenanceLedgerExpenseRow)
    content = {'batch': [row.batch_id, row.file_hash, row.owner_user_id, row.source_kind, row.filename]}
    for model in (MaintenanceLedgerContractRow, MaintenanceLedgerPlanRow, MaintenanceLedgerExpenseRow):
        columns = [c for c in model.__table__.columns if c.name not in ('issues', 'project_id', 'contract_id')]
        content[model.__tablename__] = [dict(r._mapping) for r in db.execute(
            select(*columns).where(model.batch_id == row.batch_id).order_by(model.row_id))]
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _ledger_state(db, target):
    from app.models.maintenance_ledger import MaintenanceLedgerImportBatch
    row = db.get(MaintenanceLedgerImportBatch, target.id, populate_existing=True)
    if row is None or row.owner_user_id is None or target.generation != 1:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(target,
        ObjectRef('ledger_import_batch', row.batch_id), ledger_input_digest(db, row), row.owner_user_id)



foundation.register_entry_resolver(LEDGER_PREVIEW, input_context)
for _entry in (LEDGER_READ, LEDGER_APPLY):
    foundation.register_entry_resolver(_entry, _ledger_context, reference_resolver=_ledger_reference)
artifacts.register_artifact_domain('ledger_import_batch', artifacts.ArtifactDomainAdapter(_ledger_state, None, ()))


MONTHLY_DOWNLOAD = 'GET /api/maintenance/project-manager/workbooks/v3'
MONTHLY_STATUS = 'GET /api/maintenance/project-manager/workbooks/v3/status'
MONTHLY_VALIDATE = 'POST /api/maintenance/project-manager/workbooks/v3/validate'
MONTHLY_APPLY = 'POST /api/maintenance/project-manager/workbooks/v3/apply'


def _opaque_json_digest(value):
    return func.encode(func.sha256(func.convert_to(cast(value,Text),'UTF8')),'hex')


def _monthly_header(db,batch_id):
    from app.models.maintenance_manager import MaintenanceManagerUploadBatch as model
    value=func.jsonb_build_object('file_sha256',model.file_sha256,'owner',model.owner_user_id,
        'month',model.report_month,'scope',model.scope_version,'data',model.data_version,
        'plan',model.plan_json,'issues',model.issues_json)
    project_ids=func.jsonb_path_query_array(model.plan_json,cast('$.project_scope[*].project_id',JSONPATH))
    return db.execute(select(model.batch_id,model.owner_user_id,model.report_month,model.status,
        project_ids.label('project_ids'),_opaque_json_digest(value).label('input_digest'))
        .where(model.batch_id==batch_id)).first()


def monthly_plan_digest(db,row):
    state=_monthly_header(db,row.batch_id)
    if state is None:raise artifacts.unavailable()
    return state.input_digest


def _monthly_state(db, target):
    row = _monthly_header(db,target.id)
    if row is None:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(artifacts.ArtifactTarget(target.kind, row.batch_id, 1),
        ObjectRef('manager_upload_batch', row.batch_id), row.input_digest, row.owner_user_id)


def _monthly_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    from hashlib import sha256
    from app.authz.context import canonical_json
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from app.models.maintenance_manager import MaintenanceManagerUploadBatch
    principal, _ = foundation.resolve_project_subjects(db, identity, [],
        read_snapshot=read_snapshot, locked_user=locked_user)
    row = None
    if entry_id == MONTHLY_APPLY:
        row = _monthly_header(db,params['validation_token'])
        if row is None or row.owner_user_id != principal.id:
            raise artifacts.unavailable()
        artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget('manager_upload_batch', row.batch_id, 1))
        month = row.report_month.strftime('%Y-%m')
    else:
        month = params['report_month']
    ids = sorted(set(db.scalars(select(MaintenanceProjectUserAssignment.project_id)
        .join(MaintenanceProject, MaintenanceProject.project_id == MaintenanceProjectUserAssignment.project_id)
        .where(MaintenanceProjectUserAssignment.user_id == principal.id,
            MaintenanceProjectUserAssignment.responsibility_type == 'primary_manager',
            MaintenanceProjectUserAssignment.archived_at.is_(None), MaintenanceProject.is_active.is_(True)))))
    if not ids:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '当前账号没有本人负责的有效项目')
    if row is not None:
        saved_ids = sorted(set(row.project_ids or []))
        if saved_ids != ids:
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '历史校验涉及的完整项目范围已变化，请重新下载')
    _, projects = foundation.resolve_project_subjects(db, identity, ids,
        read_snapshot=read_snapshot, locked_user=locked_user)
    if entry_id == MONTHLY_VALIDATE:
        command_digest = sha256(canonical_json({'month': month, 'file_sha256': params['file_sha256']}).encode()).hexdigest()
        discovery = foundation.slot_ref(entry_id, 'context_0', 'maintenance_manager_monthly_projects',
            selection={'report_month': month, 'file_sha256': params['file_sha256'],
                       'command_plan_sha256': command_digest})
        operation_id = sha256(canonical_json({'owner': principal.id, 'epoch': principal.authorization_epoch,
            'command_plan_sha256': command_digest, 'entry': entry_id}).encode()).hexdigest()
        carrier = foundation.slot_ref(entry_id, 'context_1', operation_id,
            selection={'command_plan_sha256': command_digest})
        slots = {'context_0': (discovery,), 'context_1': (carrier,)}
    elif entry_id == MONTHLY_APPLY:
        carrier = foundation.slot_ref(entry_id, 'context_0', row.batch_id,
            selection={'validation_token': row.batch_id, 'data_version': params['data_version']})
        slots = {'context_0': (carrier,)}
    else:
        carrier = foundation.slot_ref(entry_id, 'context_0', 'maintenance_manager_monthly_projects',
            selection={'report_month': month})
        slots = {'context_0': (carrier,)}
    context = foundation.resolved_context(db, entry_id=entry_id, branch=branch or 'default', carrier=carrier,
        slots=slots, subjects={'maintenance_manager_workbook': tuple(projects[pid].subject for pid in ids)},
        revision=relation_revision({'carrier': carrier.to_dict(),
            'projects': [projects[pid].authorization_revision for pid in ids],
            'batch': None if row is None else [row.batch_id, row.input_digest, row.status]}))
    return create_context_batch(db, [context])


def _monthly_reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    from app.authz.context import plain
    selection = plain(ref.selector).get('selection', {})
    return _monthly_context(db, identity, entry_id=entry_id, params=selection, branch=branch,
                            read_snapshot=read_snapshot, locked_user=locked_user)


for _entry in (MONTHLY_DOWNLOAD, MONTHLY_STATUS, MONTHLY_VALIDATE, MONTHLY_APPLY):
    foundation.register_entry_resolver(_entry, _monthly_context,
        reference_resolver=None if _entry == MONTHLY_VALIDATE else _monthly_reference)
artifacts.register_artifact_domain('manager_upload_batch', artifacts.ArtifactDomainAdapter(_monthly_state, None, ()))


PROJECT_WORKBOOK_DOWNLOAD = 'GET /api/maintenance/projects/stable/{project_id}/workbook'
PROJECT_WORKBOOK_VALIDATE = 'POST /api/maintenance/projects/stable/{project_id}/workbook/validate'
PROJECT_WORKBOOK_APPLY = 'POST /api/maintenance/projects/stable/{project_id}/workbook/apply'
PROJECT_WORKBOOK_ERRORS = 'GET /api/maintenance/workbook-validations/{validation_id}/errors.xlsx'


def _project_workbook_header(db,validation_id):
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation as model
    value=func.jsonb_build_object('project',model.project_id,'file_sha256',model.file_sha256,
        'export_id',model.export_id,'expected_revision',model.expected_revision,'plan',model.plan_json,
        'issues',model.issues_json,'error_sha256',func.encode(func.sha256(model.error_workbook),'hex'))
    return db.execute(select(model.validation_id,model.project_id,model.file_sha256,model.status,
        _opaque_json_digest(value).label('input_digest')).where(model.validation_id==validation_id)).first()


def project_workbook_plan_digest(db,row):
    state=_project_workbook_header(db,row.validation_id)
    if state is None:raise artifacts.unavailable()
    return state.input_digest


def _project_workbook_state(db, target):
    row = _project_workbook_header(db,target.id)
    if row is None:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(artifacts.ArtifactTarget(target.kind, row.validation_id, 1),
        ObjectRef('workbook_validation', row.validation_id), row.input_digest, None)


def _project_workbook_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation
    validation_id = params['validation_id'] if entry_id == PROJECT_WORKBOOK_ERRORS else params['validation_token']
    row = _project_workbook_header(db,validation_id)
    if row is None or (entry_id == PROJECT_WORKBOOK_APPLY and row.project_id != params['project_id']):
        raise artifacts.unavailable()
    binding = artifacts.load_artifact_binding(db,
        target=artifacts.ArtifactTarget('project_workbook_validation', validation_id, 1))
    principal, projects = foundation.resolve_project_subjects(db, identity, [row.project_id],
        read_snapshot=read_snapshot, locked_user=locked_user)
    if principal.id != binding.subject.owner_user_id:
        raise artifacts.unavailable()
    evidence = projects[row.project_id]
    selection = {} if entry_id == PROJECT_WORKBOOK_ERRORS else {
        'validation_token': validation_id, 'data_version': params['data_version']}
    carrier = foundation.slot_ref(entry_id, 'context_0',
        validation_id if entry_id == PROJECT_WORKBOOK_ERRORS else row.project_id, selection=selection)
    context = foundation.resolved_context(db, entry_id=entry_id, branch='default', carrier=carrier,
        slots={'context_0': (carrier,)}, subjects={'maintenance_project_roundtrip': (evidence.subject,)},
        revision=relation_revision({'carrier': carrier.to_dict(), 'project': evidence.authorization_revision,
            'validation': [validation_id, binding.subject.owner_user_id, row.input_digest, row.status]}))
    return create_context_batch(db, [context])


def _project_workbook_reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    from app.authz.context import plain
    params = {'validation_id': ref.id} if entry_id == PROJECT_WORKBOOK_ERRORS else {
        'project_id': ref.id, **plain(ref.selector).get('selection', {})}
    return _project_workbook_context(db, identity, entry_id=entry_id, params=params, branch=branch,
                                    read_snapshot=read_snapshot, locked_user=locked_user)


for _entry in (PROJECT_WORKBOOK_APPLY, PROJECT_WORKBOOK_ERRORS):
    foundation.register_entry_resolver(_entry, _project_workbook_context, reference_resolver=_project_workbook_reference)


def _project_error_producer_ref(db, state):
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation
    row = _project_workbook_header(db,state.target.id)
    if row is None or row.validation_id != state.ref.id:
        raise artifacts.unavailable()
    return foundation.slot_ref(PROJECT_WORKBOOK_VALIDATE, 'context_0', row.project_id,
                               selection={'file_sha256': row.file_sha256})


def _project_error_document(db, identity, ref, *, entry_id, format_purpose, read_snapshot=None, locked_user=None):
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation
    row = _project_workbook_header(db,ref.id)
    if row is None or row.status != 'error' or format_purpose != 'error_download':
        raise artifacts.unavailable()
    params = ({'project_id': row.project_id, 'file_sha256': row.file_sha256}
        if entry_id == PROJECT_WORKBOOK_VALIDATE else {'validation_id': row.validation_id})
    if entry_id not in (PROJECT_WORKBOOK_VALIDATE, PROJECT_WORKBOOK_ERRORS):
        raise artifacts.unavailable()
    batch = foundation.resolve_entry_context(db, identity, entry_id=entry_id, params=params,
        read_snapshot=read_snapshot, locked_user=locked_user)
    accesses = service.context_accesses(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    require_complete(accesses)
    _, projects = foundation.resolve_project_subjects(db, identity, [row.project_id],
        read_snapshot=read_snapshot, locked_user=locked_user)
    parent = foundation.slot_ref(PROJECT_WORKBOOK_VALIDATE, 'context_0', row.project_id,
                                selection={'file_sha256': row.file_sha256})
    return foundation.DocumentAccess(ref=ref, authorization_revision=batch.contexts[0].authorization_revision,
        accesses=tuple(accesses.accesses.values()), content_manifest=(ref, parent),
        content_revision=relation_revision({'carrier': ref.to_dict(), 'parent': parent.to_dict(),
            'project': projects[row.project_id].content_revision, 'input_and_output': row.input_digest}))


def _inspect_project_error(content):
    from io import BytesIO
    from openpyxl import load_workbook
    book = load_workbook(BytesIO(content), data_only=False)
    try:
        return {'sheets': [{'name': sheet.title, 'columns': [cell.value for cell in sheet[1]],
            'state': sheet.sheet_state,
            'hidden_columns': sorted(key for key, dimension in sheet.column_dimensions.items() if dimension.hidden),
            'hidden_rows': sorted(key for key, dimension in sheet.row_dimensions.items() if dimension.hidden)}
            for sheet in book.worksheets], 'contract_version': 1}
    finally:
        book.close()


_PROJECT_ERROR_PROJECTION = {'sheets': [
    {'name': '00_错误清单', 'columns': ['错误代码', '说明', '工作表', '行号', '字段'],
     'state': 'visible', 'hidden_columns': [], 'hidden_rows': []},
    {'name': '99_元数据', 'columns': ['key', 'value'],
     'state': 'veryHidden', 'hidden_columns': [], 'hidden_rows': []}], 'contract_version': 1}
artifacts.register_artifact_domain('project_workbook_validation', artifacts.ArtifactDomainAdapter(
    _project_workbook_state, _project_error_document, (artifacts.ArtifactFormat(
        'project_workbook_errors_v2', 'error_download', PROJECT_WORKBOOK_ERRORS,
        _PROJECT_ERROR_PROJECTION, _inspect_project_error,
        producer_entry_id=PROJECT_WORKBOOK_VALIDATE, producer_carrier_kind='project',
        producer_ref=_project_error_producer_ref),)))


def _project_validate_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    _, projects = foundation.resolve_project_subjects(db, identity, [params['project_id']],
        read_snapshot=read_snapshot, locked_user=locked_user)
    evidence = projects[params['project_id']]
    selection = {key: params[key] for key in ('file_sha256', 'command_plan_sha256') if key in params}
    carrier = foundation.slot_ref(entry_id, 'context_0', params['project_id'], selection=selection)
    return create_context_batch(db, [foundation.resolved_context(db, entry_id=entry_id,
        branch='default', carrier=carrier, slots={'context_0': (carrier,)},
        subjects={'maintenance_project_roundtrip': (evidence.subject,)},
        revision=relation_revision({'project': evidence.authorization_revision, 'input': carrier.to_dict()}))])


def _project_validate_reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    from app.authz.context import plain
    return _project_validate_context(db, identity, entry_id=entry_id,
        params={'project_id': ref.id, **plain(ref.selector).get('selection', {})}, branch=branch,
        read_snapshot=read_snapshot, locked_user=locked_user)


foundation.register_entry_resolver(PROJECT_WORKBOOK_VALIDATE, _project_validate_context,
    reference_resolver=_project_validate_reference)
