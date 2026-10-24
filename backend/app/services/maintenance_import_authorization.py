"""Exact document/shipment staging contracts and immutable stored-input bindings."""
import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import func, select, cast, Text, literal
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from app.authz import runtime, service
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.models.maintenance_doc_import import MaintenanceDocImportBatch, MaintenanceDocHeadRow, MaintenanceDocLineRow
from app.models.maintenance_ckd_import import MaintenanceCkdImportBatch, MaintenanceCkdHeadRow, MaintenanceCkdLineRow
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services.maintenance_workflow_authorization import require_complete

DOC_PREVIEW = 'POST /api/maintenance/doc-imports/{doc_type}/preview'
CKD_PREVIEW = 'POST /api/maintenance/ckd-imports/preview'
DOC_READ = 'GET /api/maintenance/doc-imports/{batch_id}'
CKD_READ = 'GET /api/maintenance/ckd-imports/{batch_id}'
CONFIG = {
    'document_import_batch': (MaintenanceDocImportBatch, MaintenanceDocHeadRow, MaintenanceDocLineRow, 'maintenance_document_import', DOC_PREVIEW, DOC_READ),
    'shipment_import_batch': (MaintenanceCkdImportBatch, MaintenanceCkdHeadRow, MaintenanceCkdLineRow, 'maintenance_shipment_import', CKD_PREVIEW, CKD_READ),
}


def _stored_input(db, kind, batch):
    _, Head, Line, *_ = CONFIG[kind]
    def digest(Model, condition):
        columns = [c for c in Model.__table__.columns if c.name not in ('project_id', 'issues')]
        row = func.jsonb_build_object(*[item for c in columns for item in (literal(c.name), c)])
        canonical = func.coalesce(cast(func.jsonb_agg(aggregate_order_by(row, Model.row_id)), Text), '[]')
        return db.scalar(select(func.encode(func.sha256(func.convert_to(canonical, 'UTF8')), 'hex'))
                         .where(condition))
    # Return only opaque digests from SQL. Bootstrap must not materialize raw
    # cells or financial values before its exact current resource decision.
    heads = digest(Head, Head.batch_id == batch.batch_id)
    lines = digest(Line, Line.head_row_id.in_(select(Head.row_id).where(Head.batch_id == batch.batch_id)))
    body = {'batch': [batch.batch_id, batch.file_hash, batch.owner_user_id,
                     getattr(batch, 'doc_type', 'ckd_shipment'), batch.filename],
            'heads': heads, 'lines': lines}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _header(db, kind, batch_id):
    Model = CONFIG[kind][0]
    columns = [Model.batch_id, Model.file_hash, Model.owner_user_id, Model.filename, Model.status]
    if kind == 'document_import_batch':
        columns.extend([Model.doc_type, Model.report_json['protocol'].astext.label('protocol')])
    columns.append(func.encode(func.sha256(func.convert_to(cast(Model.report_json, Text), 'UTF8')), 'hex').label('report_digest'))
    row = db.execute(select(*columns).where(Model.batch_id == batch_id)).first()
    return SimpleNamespace(**row._mapping) if row is not None else None


def _state(db, target):
    row = _header(db, target.kind, target.id)
    if row is None or row.owner_user_id is None or target.generation != 1:
        raise artifacts.unavailable()
    if target.kind == 'document_import_batch' and row.protocol == 'return_receipts_v1':
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(target, ObjectRef(target.kind, row.batch_id),
                                         _stored_input(db, target.kind, row), row.owner_user_id)


for _kind in CONFIG:
    artifacts.register_artifact_domain(_kind, artifacts.ArtifactDomainAdapter(_state, None, (), private=False))


def _input(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    selection = {name: params[name] for name in ('input_sha256', 'doc_type')}
    principal, _ = foundation.resolve_project_subjects(db, identity, [], read_snapshot=read_snapshot, locked_user=locked_user)
    operation_id = relation_revision({'entry': entry_id, 'actor': principal.id,
                                     'epoch': principal.authorization_epoch, 'selection': selection})
    ref = foundation.slot_ref(entry_id, 'context_0', operation_id, selection=selection)
    requirements = ENTRY_CONTRACTS[entry_id]['branches']['default']['requirements']
    return create_context_batch(db, [foundation.resolved_context(db, entry_id=entry_id, branch='default',
        carrier=ref, slots={'context_0': (ref,)},
        subjects={r: (PolicySubject(operation_id, {'is_owner': True}),) for r in requirements},
        revision=operation_id)])


def _read_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    kind = 'document_import_batch' if entry_id == DOC_READ else 'shipment_import_batch'
    Model, _, _, resource, *_ = CONFIG[kind]
    batch = _header(db, kind, params['batch_id'])
    if batch is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '导入批次不存在')
    principal, _ = foundation.resolve_project_subjects(db, identity, [], read_snapshot=read_snapshot, locked_user=locked_user)
    state = _state(db, artifacts.ArtifactTarget(kind, batch.batch_id, 1))
    ref = foundation.slot_ref(entry_id, 'context_0', batch.batch_id)
    return create_context_batch(db, [foundation.resolved_context(db, entry_id=entry_id, branch='default',
        carrier=ref, slots={'context_0': (ref,)}, subjects={resource: (PolicySubject(batch.batch_id,
            {'is_owner': batch.owner_user_id == principal.id}),)},
        revision=relation_revision({'input': state.input_digest, 'status': batch.status,
            'report_digest': batch.report_digest, 'actor': principal.id, 'epoch': principal.authorization_epoch}))])


def _reference(db, identity, ref, **kwargs):
    return _read_context(db, identity, params={'batch_id': ref.id}, **kwargs)


for _entry in (DOC_PREVIEW, CKD_PREVIEW):
    foundation.register_entry_resolver(_entry, _input)
for _entry in (DOC_READ, CKD_READ):
    foundation.register_entry_resolver(_entry, _read_context, reference_resolver=_reference)


@dataclass(frozen=True)
class PreviewAuthorization:
    identity: object
    actor: object
    kind: str


def prepare_preview(db, *, kind, parsed, identity, idempotency_key, precondition=None,
                    producer_authorization=None, commit=True):
    _, _, _, resource, entry, _ = CONFIG[kind]
    doc_type = parsed.get('doc_type', 'ckd_shipment')
    if producer_authorization is not None:
        from app.services.maintenance_ai_fallback_authorization import AiAcceptanceAuthorization
        if type(producer_authorization) is not AiAcceptanceAuthorization or commit:
            raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', 'AI 接受必须使用同事务的受控生产许可')
        actor_id = producer_authorization.prepare(db, doc_type, parsed['file_hash'])
        identity = producer_authorization.identity
    elif runtime.is_authoritative(db, resource):
        params = {'doc_type': doc_type, 'input_sha256': parsed['file_hash']}
        with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
                entry_id=entry, params=params) as (_, _, _, accesses):
            require_complete(accesses)
            actor_id = accesses.principal.id
        artifacts.begin_artifact_protocol(db.get_bind())
    else:
        return None
    key = artifacts.stable_operation_key(actor_id, kind + '_preview', idempotency_key)
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
    if producer_authorization is not None:
        actor = producer_authorization.authorize(db, doc_type, parsed['file_hash'])
    else:
        actor = service.lock_actor(db, identity)
        batch = _input(db, identity, entry_id=entry, params=params, locked_user=actor)
        accesses = service.context_accesses(db, identity, batch, locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
    return PreviewAuthorization(identity, actor, kind)


def finish_preview(db, authorization, batch, idempotency_key):
    if authorization is None:
        return
    db.flush()
    target = artifacts.ArtifactTarget(authorization.kind, batch.batch_id, 1)
    state = _state(db, target)
    artifacts.register_artifact_binding(db, authorization.identity, target=target,
        input_digest=state.input_digest,
        operation=artifacts.IdempotentOperation(authorization.kind + '_preview', idempotency_key),
        locked_user=authorization.actor)


def read_batch(db, identity, *, kind, batch_id, authorization_view=False, precondition=None):
    Model, _, _, _, _, entry = CONFIG[kind]
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry, params={'batch_id': batch_id}) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, ref)
        artifacts.load_artifact_binding(snapshot, target=artifacts.ArtifactTarget(kind, batch_id, 1))
        row = snapshot.get(Model, batch_id)
        result = {key: getattr(row, key) for key in ('batch_id', 'file_hash', 'filename',
            'uploaded_by', 'head_rows', 'line_rows', 'issue_rows', 'status', 'applied_by')}
        if kind == 'document_import_batch':
            result['doc_type'] = row.doc_type
        result.update(uploaded_at=row.uploaded_at.isoformat() if row.uploaded_at else None,
                      applied_at=row.applied_at.isoformat() if row.applied_at else None,
                      report=row.report_json, authorization_context=accesses.wire(ref))
        return result

DOC_APPLY = 'POST /api/maintenance/doc-imports/{batch_id}/apply'
CKD_APPLY = 'POST /api/maintenance/ckd-imports/{batch_id}/apply'


def _head_relationships(db, kind, condition):
    Head = CONFIG[kind][1]
    columns = [Head.row_id, Head.batch_id, Head.wbdd_no]
    if kind == 'document_import_batch':
        columns.extend([Head.xsdd_no, Head.project_name, Head.project_id])
    return [SimpleNamespace(**row._mapping) for row in db.execute(
        select(*columns).where(condition).order_by(Head.row_id))]


def _project_id(db, kind, head):
    """Same legacy WBDD -> XSDD -> exact project-code precedence, only IDs."""
    from app.models.maintenance import FMaintenanceOrder
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
    if head.wbdd_no:
        source_id = db.execute(select(FMaintenanceOrder.raw_order_id).where(
            FMaintenanceOrder.order_no == head.wbdd_no)).scalar_one_or_none()
        if source_id is not None:
            parent = db.execute(select(MaintenanceSourceOrderAssignment.project_id).where(
                MaintenanceSourceOrderAssignment.source_order_id == source_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True))).scalar_one_or_none()
            if parent is not None:
                return parent
    if kind == 'document_import_batch':
        if head.xsdd_no:
            parent = db.execute(select(MaintenanceProjectContract.project_id).where(
                MaintenanceProjectContract.contract_no == head.xsdd_no)).scalar_one_or_none()
            if parent is not None:
                return parent
        if head.project_name:
            return db.execute(select(MaintenanceProject.project_id).where(
                MaintenanceProject.project_code == head.project_name)).scalar_one_or_none()
    return None


def _target_plan(db, kind, batch):
    from app.models.maintenance import FMaintenanceOrder
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from app.models.maintenance_project import MaintenanceProjectContract
    _, Head, _, *_ = CONFIG[kind]
    heads = _head_relationships(db, kind, Head.batch_id == batch.batch_id)
    targets = {}
    for head in heads:
        # Even ignored/frozen rows can contain an existing project association;
        # never discard them before checking the complete input closure.
        pid = _project_id(db, kind, head)
        targets[head.row_id] = [pid, getattr(head, 'project_id', None)]
    project_ids = sorted({p for pair in targets.values() for p in pair if p})
    wbdds = {h.wbdd_no for h in heads if h.wbdd_no}
    sources = list(db.execute(select(FMaintenanceOrder.id, FMaintenanceOrder.raw_order_id, FMaintenanceOrder.order_no)
        .where(FMaintenanceOrder.order_no.in_(wbdds)).order_by(FMaintenanceOrder.id)))
    assignments = list(db.execute(select(MaintenanceSourceOrderAssignment.assignment_id,
        MaintenanceSourceOrderAssignment.source_order_id, MaintenanceSourceOrderAssignment.project_id,
        MaintenanceSourceOrderAssignment.version, MaintenanceSourceOrderAssignment.is_active)
        .where(MaintenanceSourceOrderAssignment.source_order_id.in_([r.raw_order_id for r in sources]))
        .order_by(MaintenanceSourceOrderAssignment.assignment_id)))
    xsdds = {h.xsdd_no for h in heads if getattr(h, 'xsdd_no', None)}
    contracts = list(db.execute(select(MaintenanceProjectContract.contract_id,
        MaintenanceProjectContract.project_id, MaintenanceProjectContract.contract_no,
        MaintenanceProjectContract.version).where(MaintenanceProjectContract.contract_no.in_(xsdds))
        .order_by(MaintenanceProjectContract.contract_id)))
    return {'targets': targets, 'projects': project_ids, 'sources': [list(r) for r in sources],
            'assignments': [list(r) for r in assignments], 'contracts': [list(r) for r in contracts]}


def _apply_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None, _principal=None):
    kind = 'document_import_batch' if entry_id == DOC_APPLY else 'shipment_import_batch'
    Model, _, _, resource, *_ = CONFIG[kind]
    row = _header(db, kind, params['batch_id'])
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '导入批次不存在')
    state = _state(db, artifacts.ArtifactTarget(kind, row.batch_id, 1))
    plan = _target_plan(db, kind, row)
    if _principal is None:
        principal, projects = foundation.resolve_project_subjects(db, identity, plan['projects'],
            read_snapshot=read_snapshot, locked_user=locked_user)
    else:
        principal = _principal
        projects = foundation.project_evidence(db, principal, plan['projects'])
    selected = 'default' if projects else 'no_targets'
    if branch not in (None, selected):
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '导入实际目标集合已变化')
    ref = foundation.slot_ref(entry_id, 'context_0', row.batch_id)
    slots = {'context_0': (ref,)}
    if projects:
        slots['context_1'] = tuple(foundation.slot_ref(entry_id, 'context_1', pid) for pid in projects)
    target_subjects = tuple(e.subject for e in projects.values()) if projects else (
        PolicySubject(row.batch_id, {'is_active': False, 'is_manager': False, 'is_viewer': False, 'is_sales': False}),)
    return create_context_batch(db, [foundation.resolved_context(db, entry_id=entry_id,
        branch=selected, carrier=ref, slots=slots,
        subjects={resource: (PolicySubject(row.batch_id, {'is_owner': row.owner_user_id == principal.id}),),
                  resource + '_targets': target_subjects},
        revision=relation_revision({'input': state.input_digest, 'status': row.status, 'plan': plan,
            'projects': {p: e.authorization_revision for p,e in projects.items()},
            'actor': [principal.id, principal.authorization_epoch]}))])


def _apply_reference(db, identity, ref, **kwargs):
    return _apply_context(db, identity, params={'batch_id': ref.id}, **kwargs)


for _entry in (DOC_APPLY, CKD_APPLY):
    foundation.register_entry_resolver(_entry, _apply_context, reference_resolver=_apply_reference)


class ImportApplyAuthorization:
    def __init__(self, db, identity, context, precondition):
        self.db, self.identity, self.context, self.precondition = db, identity, context, precondition
        self.transaction = db.get_transaction()

    def finish(self, db):
        if self.db is not db or self.transaction is not db.get_transaction():
            raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '导入写事务不匹配')
        actor = service.lock_actor(db, self.identity)
        accesses = service.context_accesses(db, self.identity, self.context, locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(self.precondition, accesses, self.context.contexts[0].ref)


def apply_batch(db, identity, *, kind, batch_id, authorization_view=False, precondition=None):
    from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
    from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
    from app.models.maintenance import FMaintenanceOrder
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from app.services import maintenance_project_operations as operations
    from app.services import maintenance_doc_import as docs, maintenance_ckd_import as ckd
    Model, Head, Line, _, *_ = CONFIG[kind]
    entry = DOC_APPLY if kind == 'document_import_batch' else CKD_APPLY
    params = {'batch_id': batch_id}
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity, entry_id=entry,
            params=params) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, ref)
        artifacts.load_artifact_binding(snapshot, target=artifacts.ArtifactTarget(kind, batch_id, 1))
        principal = accesses.principal
    try:
        db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
        if kind == 'document_import_batch':
            from app.services.maintenance_return_receipt_import import _lock
            _lock(db, 'return-receipt-import-apply')
        row = db.scalar(select(Model).where(Model.batch_id == batch_id).with_for_update().execution_options(populate_existing=True))
        if row is None:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '导入批次不存在')
        plan = _target_plan(db, kind, row)
        operations.lock_workbook_states(db, project_ids=plan['projects'])
        for model, key, ids in (
            (MaintenanceProject, MaintenanceProject.project_id, plan['projects']),
            (MaintenanceProjectContract, MaintenanceProjectContract.contract_id, [r[0] for r in plan['contracts']]),
            (FMaintenanceOrder, FMaintenanceOrder.id, [r[0] for r in plan['sources']]),
            (MaintenanceSourceOrderAssignment, MaintenanceSourceOrderAssignment.assignment_id, [r[0] for r in plan['assignments']]),
            (Head, Head.batch_id, [batch_id]), (Line, Line.batch_id, [batch_id])):
            if ids:
                list(db.scalars(select(model).where(key.in_(ids)).order_by(*model.__table__.primary_key)
                    .with_for_update().execution_options(populate_existing=True)))
        if _target_plan(db, kind, row) != plan:
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '导入目标关联已变化')
        artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget(kind, batch_id, 1))
        context = _apply_context(db, identity, entry_id=entry, params=params, _principal=principal)
        authorization = ImportApplyAuthorization(db, identity, context, precondition)
        # The legacy primitives acquire their remaining stock/fact locks and do
        # all writes in this transaction. No user lock is held until they finish.
        result = (docs if kind == 'document_import_batch' else ckd).apply_batch(db, batch_id,
            principal.username, allowed_project_ids=set(plan['projects']), commit=False,
            _authorization=authorization)
        authorization.finish(db)
        db.commit()
        return {'batch_id': batch_id, **result}
    except BaseException:
        db.rollback()
        raise

RELINK = 'POST /api/maintenance/doc-imports/relink-projects'


def _relink_plan(db):
    heads = _head_relationships(db, 'document_import_batch',
        MaintenanceDocHeadRow.batch_id.in_(select(MaintenanceDocImportBatch.batch_id).where(
            MaintenanceDocImportBatch.status == 'applied')) & MaintenanceDocHeadRow.project_id.is_(None))
    targets = [[r.row_id, r.batch_id, _project_id(db, 'document_import_batch', r)] for r in heads]
    return {'targets': targets, 'projects': sorted({r[2] for r in targets if r[2]}),
        'batches': {bid: _target_plan(db, 'document_import_batch', _header(db, 'document_import_batch', bid))
                    for bid in sorted({r.batch_id for r in heads})}}


def _relink_context(db, identity, *, entry_id=RELINK, params=None, branch=None,
                    read_snapshot=None, locked_user=None, _principal=None):
    plan = _relink_plan(db)
    if _principal is None:
        principal, projects = foundation.resolve_project_subjects(db, identity, plan['projects'],
            read_snapshot=read_snapshot, locked_user=locked_user)
    else:
        principal = _principal
        projects = foundation.project_evidence(db, principal, plan['projects'])
    selected = 'default' if projects else 'empty'
    if branch not in (None,selected):
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '重关联实际目标集合已变化')
    digest = relation_revision(plan)
    oid = relation_revision({'plan': digest, 'actor': principal.id, 'epoch': principal.authorization_epoch})
    ref = foundation.slot_ref(RELINK, 'context_2', oid, selection={'command_plan_sha256': digest})
    slots = {'context_0': (foundation.slot_ref(RELINK,'context_0','document_import_relink_candidates'),), 'context_2':(ref,)}
    if projects:
        slots['context_1'] = tuple(foundation.slot_ref(RELINK,'context_1',pid) for pid in projects)
    subjects = tuple(p.subject for p in projects.values()) if projects else (
        PolicySubject(oid, {'is_active':False,'is_manager':False,'is_viewer':False,'is_sales':False}),)
    return create_context_batch(db,[foundation.resolved_context(db,entry_id=RELINK,branch=selected,
        carrier=ref,slots=slots,subjects={'maintenance_document_relink': subjects},
        revision=relation_revision({'plan':plan,'projects':{p:e.authorization_revision for p,e in projects.items()},
            'actor':[principal.id,principal.authorization_epoch]}),empty_target_set=not projects)])


foundation.register_entry_resolver(RELINK, _relink_context)


def relink(db, identity, *, authorization_view=False, precondition=None):
    from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
    from app.models.maintenance_project import MaintenanceProject
    from app.services import maintenance_doc_import as docs
    from app.services import maintenance_project_operations as operations
    from app.services.maintenance_return_receipt_import import _lock
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()),identity,
            entry_id=RELINK,params={}) as (_,_,batch,accesses):
        ref=batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses,ref)
        require_complete(accesses)
        service.check_context_precondition(precondition,accesses,ref)
        principal=accesses.principal
    try:
        db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
        _lock(db,'return-receipt-import-apply')
        plan=_relink_plan(db)
        operations.lock_workbook_states(db,project_ids=plan['projects'])
        list(db.scalars(select(MaintenanceProject).where(MaintenanceProject.project_id.in_(plan['projects']))
            .order_by(MaintenanceProject.project_id).with_for_update().execution_options(populate_existing=True)))
        list(db.scalars(select(MaintenanceDocHeadRow).where(MaintenanceDocHeadRow.row_id.in_([r[0] for r in plan['targets']]))
            .order_by(MaintenanceDocHeadRow.row_id).with_for_update().execution_options(populate_existing=True)))
        if _relink_plan(db)!=plan:
            raise authz_error(409,'AUTHZ_CONTEXT_STALE','重关联实际目标集合已变化')
        captured=_relink_context(db,identity,_principal=principal)
        result=docs.relink_projects(db,allowed_project_ids=set(plan['projects']),commit=False,
                                  _authorization=ImportApplyAuthorization(db,identity,captured,precondition))
        actor=service.lock_actor(db,identity)
        accesses=service.context_accesses(db,identity,captured,locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(precondition,accesses,captured.contexts[0].ref)
        db.commit()
        return {**result,'authorization_context':accesses.wire(captured.contexts[0].ref)}
    except BaseException:
        db.rollback()
        raise
