"""Warehouse authorization over actual document/assignment closure and stable receipts.

Preview remains a pure parser. A receipt records the stable uploader, while every
replay re-resolves its current parents and verifies the immutable sidecar.
"""
import hashlib
import json

from sqlalchemy import Text, and_, cast, exists, func, or_, select, true, union
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors

from app.authz import service
from app.authz.context import ObjectRef, PolicySubject, canonical_json, create_context_batch, relation_revision
from app.authz.engine import QueryBindings
from app.authz.errors import authz_error
from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.dimensions import DimPart, PartAlias
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_bad_return import MaintenanceBadReturn
from app.models.maintenance_project_operations import MaintenanceSiteIssue, MaintenanceSiteIssueDeliverySource
from app.services import maintenance_warehouse_site_issue_bridge as bridge
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectUserAssignment
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as Assignment
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseImportBatch as Batch, MaintenanceWarehouseDocument as Document,
    MaintenanceWarehouseDocumentLink as Link, MaintenanceWarehouseDocumentLine as Line,
    MaintenanceWarehouseAmbiguity as Ambiguity, MaintenanceWarehouseAuditEvent as Audit,
)
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_warehouse as business
from app.services.maintenance_workflow_authorization import require_complete, captured_provenance

PREFIX = 'POST /api/maintenance/'
PREVIEW = PREFIX + 'warehouse-imports/preview'
APPLY = PREFIX + 'warehouse-imports/{import_id}/apply'
DOCUMENTS = PREFIX + 'warehouse-documents/search'
AMBIGUITIES = PREFIX + 'warehouse-ambiguities/search'
RESOLVE = PREFIX + 'warehouse-ambiguities/{ambiguity_id}/resolve'
RECONCILE = PREFIX + 'warehouse-integrations/reconcile'
WAREHOUSE = 'maintenance_warehouse'
IMPORT = 'maintenance_warehouse_import'
KIND = 'warehouse_import'
PURPOSE = 'apply_receipt'


def _parents_table():
    direct = select(Link.document_id.label('document_id'), Link.target_id.label('project_id')).where(
        Link.status == 'active', Link.target_type == 'maintenance_project')
    source = select(Link.document_id.label('document_id'), Assignment.project_id.label('project_id')).join(
        Assignment, Assignment.source_order_id == Link.target_id).where(
        Link.status == 'active', Link.target_type == 'maintenance_order', Assignment.is_active.is_(True))
    issue = select(Link.document_id.label('document_id'), MaintenanceSiteIssue.project_id.label('project_id')).join(
        MaintenanceSiteIssue, MaintenanceSiteIssue.issue_id == Link.target_id).where(
        Link.status == 'active', Link.target_type == 'maintenance_site_issue')
    returned = select(Link.document_id.label('document_id'), MaintenanceBadReturn.project_id.label('project_id')).join(
        MaintenanceBadReturn, MaintenanceBadReturn.return_id == Link.target_id).where(
        Link.status == 'active', Link.target_type == 'maintenance_bad_return')
    local = union(direct, source, issue, returned).subquery('warehouse_local_parents')
    closure = select(Document.document_id.label('root_id'), cast(Document.document_id, Text).label('document_id')).cte(
        'warehouse_document_closure', recursive=True)
    # UNION (not UNION ALL) makes cycles terminate without dropping any actual parent.
    closure = closure.union(select(closure.c.root_id, cast(Link.target_id, Text)).join(
        Link, Link.document_id == closure.c.document_id).where(
        Link.status == 'active', Link.target_type == 'warehouse_document'))
    return select(closure.c.root_id.label('document_id'), local.c.project_id).join(
        local, local.c.document_id == closure.c.document_id).subquery('warehouse_actual_parents')


def _query_table(principal):
    # Current closed scoped policy is primary_manager only. A future M-or-V
    # policy must preserve per-parent OR, not combine all-M with all-V.
    parents = _parents_table()
    projects = foundation.project_relation_table(principal)
    columns = []
    for name in ('is_manager', 'is_viewer', 'is_sales', 'is_active'):
        present = exists(select(parents.c.project_id).where(parents.c.document_id == Document.document_id))
        denied = exists(select(parents.c.project_id).outerjoin(projects,
            projects.c.project_id == parents.c.project_id).where(
                parents.c.document_id == Document.document_id,
                or_(projects.c.project_id.is_(None), projects.c[name].is_not(True))))
        columns.append(and_(present, ~denied).label(name))
    return select(Document.document_id.label('document_id'), *columns).subquery('warehouse_authorized_documents')


def _query_bindings(db, principal, context, resource):
    table = _query_table(principal)
    return QueryBindings(resource, table, {f'request.resource.attr.{name}': table.c[name]
        for name in ('is_manager', 'is_viewer', 'is_sales', 'is_active')})


foundation.register_query_bindings(WAREHOUSE, _query_bindings)


def _condition(access):
    tables = {column.table for column in visitors.iterate(access.predicate)
        if getattr(getattr(column, 'table', None), 'name', None) == 'warehouse_authorized_documents'}
    if len(tables) > 1:
        raise authz_error(503, 'AUTHZ_INVALID_SCOPE_PLAN', '仓库查询范围不一致')
    table = tables.pop() if tables else _query_table(access.principal)
    # all-project grants include legitimately unassigned documents/ambiguities.
    if access.predicate.compare(true()):
        return None
    return Document.document_id.in_(select(table.c.document_id).where(access.predicate))


def _parent_rows(db, document_ids, source_ids=(), project_ids=()):
    document_ids = set(document_ids)
    frontier = set(document_ids)
    while frontier:
        linked = set(db.scalars(select(Link.target_id).where(Link.document_id.in_(frontier),
            Link.status == 'active', Link.target_type == 'warehouse_document')))
        frontier = linked - document_ids
        document_ids.update(frontier)
    links = list(db.execute(select(Link.link_id, Link.document_id, Link.target_type, Link.target_id,
        Link.status, Link.version).where(Link.document_id.in_(document_ids)).order_by(Link.link_id)))
    sources = set(source_ids) | {r.target_id for r in links if r.status == 'active' and r.target_type == 'maintenance_order'}
    parents = set(project_ids) | {r.target_id for r in links if r.status == 'active' and r.target_type == 'maintenance_project'}
    assignments = list(db.execute(select(Assignment.assignment_id, Assignment.source_order_id,
        Assignment.project_id, Assignment.version, Assignment.is_active).where(
        Assignment.source_order_id.in_(sources)).order_by(Assignment.assignment_id)))
    parents.update(row.project_id for row in assignments if row.is_active)
    issue_ids = sorted({r.target_id for r in links if r.status == 'active' and r.target_type == 'maintenance_site_issue'})
    return_ids = sorted({r.target_id for r in links if r.status == 'active' and r.target_type == 'maintenance_bad_return'})
    issue_parents = list(db.execute(select(MaintenanceSiteIssue.issue_id, MaintenanceSiteIssue.project_id)
        .where(MaintenanceSiteIssue.issue_id.in_(issue_ids)).order_by(MaintenanceSiteIssue.issue_id)))
    return_parents = list(db.execute(select(MaintenanceBadReturn.return_id, MaintenanceBadReturn.project_id)
        .where(MaintenanceBadReturn.return_id.in_(return_ids)).order_by(MaintenanceBadReturn.return_id)))
    parents.update(r.project_id for r in [*issue_parents, *return_parents])
    return {'documents': sorted(set(document_ids)), 'sources': sorted(sources), 'parents': sorted(parents),
            'links': [list(r) for r in links], 'assignments': [list(r) for r in assignments],
            'issue_parents': [list(r) for r in issue_parents], 'return_parents': [list(r) for r in return_parents]}


def _input_plan(db, parsed):
    refs = {v for doc in parsed.documents if (v := doc.stable_refs.get('maintenance_order'))}
    sources = list(db.scalars(select(FMaintenanceOrder.raw_order_id).where(
        or_(FMaintenanceOrder.raw_order_id.in_(refs), FMaintenanceOrder.order_no.in_(refs)))))
    ids = {business._document_id(doc.document_type, doc.document_no) for doc in parsed.documents}
    upstream = {v for doc in parsed.documents if (v := doc.stable_refs.get('upstream_document'))}
    ids.update(db.scalars(select(Document.document_id).where(or_(
        Document.source_document_id.in_(upstream), Document.document_no.in_(upstream)))))
    # Return associations are derived from exact warehouse references, not names.
    refs = {str(v) for doc in parsed.documents for v in (doc.source_document_id, doc.document_no) if v}
    returns = list(db.execute(select(MaintenanceBadReturn.return_id, MaintenanceBadReturn.project_id).where(
        MaintenanceBadReturn.status != 'void', or_(MaintenanceBadReturn.warehouse_reference.in_(refs),
                                                  MaintenanceBadReturn.inbound_reference.in_(refs)))))
    parent_ids = {row.project_id for row in returns}
    plan = _parent_rows(db, ids, sources, parent_ids)
    pns = {line.pn for doc in parsed.documents for line in doc.lines if line.pn}
    parts = set()
    for chunk in business._chunks(pns):
        parts.update(db.scalars(select(DimPart.id).where(DimPart.pn_std.in_(chunk), DimPart.status == 'active')))
        parts.update(db.scalars(select(DimPart.id).join(PartAlias, PartAlias.part_id == DimPart.id).where(
            PartAlias.pn_raw.in_(chunk), PartAlias.status == 'active', DimPart.status == 'active')))
    plan['parts'] = sorted(parts)
    plan['aliases'] = [list(r) for r in db.execute(select(PartAlias.id, PartAlias.part_id, PartAlias.pn_raw, PartAlias.status)
        .where(PartAlias.pn_raw.in_(pns)).order_by(PartAlias.id))]
    plan['candidate_returns'] = sorted({row.return_id for row in returns})
    plan['input_sha256'] = parsed.source_file_hash
    plan['adapter_version'] = parsed.adapter_version
    return plan


def _reconcile_plan(db):
    ids = list(db.scalars(select(Link.document_id).where(Link.status == 'active',
        Link.line_id.is_(None), Link.target_type == 'maintenance_order').distinct().order_by(Link.document_id)))
    return _parent_rows(db, ids)


def _opaque_digest(value):
    return func.encode(func.sha256(func.convert_to(func.coalesce(cast(value, Text), 'null'), 'UTF8')), 'hex')


def _resolve_plan(db, params):
    # Candidate identity membership is checked inside SQL; evidence/candidate
    # business JSON never crosses the bootstrap boundary before its PDP result.
    candidate = func.jsonb_array_elements(Ambiguity.candidates_json).table_valued('value').alias('warehouse_candidate')
    value = cast(candidate.c.value, JSONB)
    membership = exists(select(1).select_from(candidate).where(
        value['target_type'].astext == params.get('target_type'),
        value['target_id'].astext == params.get('target_id')))
    row = db.execute(select(Ambiguity.ambiguity_id, Ambiguity.document_id, Ambiguity.version, Ambiguity.status,
        _opaque_digest(Ambiguity.candidates_json).label('candidates_digest'),
        _opaque_digest(Ambiguity.evidence_json).label('evidence_digest'), membership.label('candidate_matches'))
        .where(Ambiguity.ambiguity_id == params['ambiguity_id'])).first()
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '关联歧义不存在')
    docs, sources, parents = ({row.document_id} if row.document_id else set()), set(), set()
    target_type, target_id = params.get('target_type'), params.get('target_id')
    if params.get('decision') == 'link':
        if not row.candidate_matches:
            raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '稳定目标不属于该歧义的候选集合')
        if target_type == 'maintenance_order': sources.add(target_id)
        elif target_type == 'maintenance_project': parents.add(target_id)
        elif target_type == 'warehouse_document': docs.add(target_id)
        elif target_type in ('maintenance_site_issue', 'maintenance_bad_return'):
            model = MaintenanceSiteIssue if target_type == 'maintenance_site_issue' else MaintenanceBadReturn
            key = model.issue_id if target_type == 'maintenance_site_issue' else model.return_id
            pid = db.scalar(select(model.project_id).where(key == target_id))
            if pid is None: raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '关联目标已变化')
            parents.add(pid)
    plan = _parent_rows(db, docs, sources, parents)
    plan['ambiguity'] = [row.ambiguity_id, row.document_id, row.version, row.status,
                         row.candidates_digest, row.evidence_digest]
    if target_type == 'dim_part' and target_id and target_id.isdecimal(): plan['parts'] = [int(target_id)]
    if target_type == 'maintenance_site_issue': plan['target_issues'] = [target_id]
    if target_type == 'maintenance_bad_return': plan['target_returns'] = [target_id]
    return plan


def _batch_metadata(db, import_id):
    return db.execute(select(Batch.import_id, Batch.owner_user_id, Batch.source_file_hash, Batch.adapter_version)
        .where(Batch.import_id == import_id)).first()


def _receipt_metadata(db, import_id):
    row = _batch_metadata(db, import_id)
    if row is None or row.owner_user_id is None:
        raise artifacts.unavailable()
    contract = db.scalar(select(Audit.after_json['authorization_contract']).where(
        Audit.event_id == business._uuid(f'maintenance-warehouse-audit:import:{import_id}')))
    if (not isinstance(contract, dict) or set(contract) != {'schema_version', 'plan', 'result_sha256'}
            or contract['schema_version'] != 1 or not isinstance(contract['plan'], dict)
            or contract['plan'].get('input_sha256') != row.source_file_hash
            or contract['plan'].get('adapter_version') != row.adapter_version):
        raise artifacts.unavailable()
    # Only the immutable contract contains lineage metadata; the result body is
    # hashed by PostgreSQL and remains unread until the complete current grant.
    result_digest = db.scalar(select(_opaque_digest(Batch.result_json)).where(Batch.import_id == import_id))
    digest = relation_revision({'import_id': row.import_id, 'owner': row.owner_user_id,
        'input': row.source_file_hash, 'contract': contract, 'result_digest': result_digest})
    return row, contract, digest


def _receipt(db, import_id):
    row = db.get(Batch, import_id, populate_existing=True)
    if row is None or row.owner_user_id is None: raise artifacts.unavailable()
    event = db.get(Audit, business._uuid(f'maintenance-warehouse-audit:import:{import_id}'), populate_existing=True)
    contract = event.after_json.get('authorization_contract') if event and isinstance(event.after_json, dict) else None
    if (not isinstance(contract, dict) or set(contract) != {'schema_version', 'plan', 'result_sha256'}
            or contract['schema_version'] != 1 or contract['result_sha256'] != relation_revision(row.result_json)
            or contract['plan'].get('input_sha256') != row.source_file_hash
            or contract['plan'].get('adapter_version') != row.adapter_version):
        raise artifacts.unavailable()
    return row, contract, _receipt_metadata(db, import_id)[2]


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    principal, _ = foundation.resolve_project_subjects(db, identity, [], read_snapshot=read_snapshot, locked_user=locked_user)
    selected, slots = 'default', {}
    if entry_id in (DOCUMENTS, AMBIGUITIES):
        names = ('q', 'document_type') if entry_id == DOCUMENTS else ('q', 'status', 'ambiguity_type')
        refid = 'maintenance_warehouse_documents' if entry_id == DOCUMENTS else 'warehouse_ambiguities'
        carrier = foundation.slot_ref(entry_id, 'context_0', refid, selection={n: params.get(n) for n in names})
        slots = {'context_0': (carrier,)}
        plan = _parent_rows(db, list(db.scalars(select(Document.document_id))))
        # Include ambiguity membership/generation so counts and selected rows share this observation.
        plan['ambiguities'] = [list(r) for r in db.execute(select(Ambiguity.ambiguity_id, Ambiguity.document_id,
            Ambiguity.version, Ambiguity.status).order_by(Ambiguity.ambiguity_id))]
        resource, subjects = WAREHOUSE, ()
    elif entry_id in (PREVIEW, APPLY):
        parsed = params.get('_parsed')
        if entry_id == APPLY and parsed is None:
            row, contract, _ = _receipt_metadata(db, params['import_id'])
            plan = _parent_rows(db, contract['plan']['documents'], contract['plan']['sources'], contract['plan']['parents'])
            plan.update({k: contract['plan'][k] for k in ('input_sha256', 'adapter_version', 'parts', 'candidate_returns', 'aliases')})
        else:
            plan = _input_plan(db, parsed)
            row = _batch_metadata(db, business._import_id(parsed)) if entry_id == APPLY else None
        if entry_id == PREVIEW:
            carrier = foundation.slot_ref(entry_id, 'context_0', plan['input_sha256'], selection={
                'input_sha256': plan['input_sha256'], 'doc_type': 'warehouse_workbook'})
            slots = {'context_0': (carrier,)}
        else:
            if params.get('import_id') != (row.import_id if parsed is None else business._import_id(parsed)):
                raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '上传文件与导入标识不一致')
            selected = 'existing' if row else 'new'
            if row:
                stored, contract, _ = _receipt_metadata(db, row.import_id)
                if stored.owner_user_id != principal.id: raise artifacts.unavailable()
                # Re-uploading does not silently replace the original full target set.
                if set(plan['documents']) != set(contract['plan']['documents']): raise artifacts.unavailable()
            key = 'context_0' if row else 'context_1'
            carrier = foundation.slot_ref(entry_id, key, row.import_id if row else plan['input_sha256'], selection={
                'input_sha256': plan['input_sha256'], 'adapter_version': plan['adapter_version']})
            slots = {key: (carrier,)}
        resource, subjects = IMPORT, None
    elif entry_id == RESOLVE:
        plan = _resolve_plan(db, params)
        carrier = foundation.slot_ref(entry_id, 'context_0', params['ambiguity_id'], selection={
            n: params.get(n) for n in ('decision', 'link_kind', 'target_type', 'target_id')})
        slots = {'context_0': (carrier,)}
        resource, subjects = WAREHOUSE, None
    else:
        plan = _reconcile_plan(db)
        digest = relation_revision({'plan': plan, 'reason': params['reason']})
        carrier = foundation.slot_ref(entry_id, 'context_2', digest, selection={'command_plan_sha256': digest})
        slots = {'context_0': (foundation.slot_ref(entry_id, 'context_0', 'warehouse_integration_candidates'),),
                 'context_2': (carrier,)}
        if plan['documents']:
            slots['context_1'] = tuple(foundation.slot_ref(entry_id, 'context_1', did) for did in plan['documents'])
        else: selected = 'empty'
        resource, subjects = IMPORT, None
    _, evidence = foundation.resolve_project_subjects(db, identity, plan['parents'],
        read_snapshot=read_snapshot, locked_user=locked_user)
    if subjects is None and resource == IMPORT:
        subjects = tuple(PolicySubject(pid, {}) for pid in plan['parents']) or (PolicySubject(carrier.id, {}),)
    if subjects is None:
        subjects = tuple(evidence[pid].subject for pid in plan['parents']) or (PolicySubject(carrier.id,
            {'is_manager': False, 'is_viewer': False, 'is_sales': False, 'is_active': False}),)
    context = foundation.resolved_context(db, entry_id=entry_id, branch=selected, carrier=carrier,
        slots=slots, subjects={resource: subjects}, revision=relation_revision({
            'carrier': carrier.to_dict(), 'plan': plan,
            'parents': {pid: value.authorization_revision for pid, value in evidence.items()},
            'actor': [principal.id, principal.authorization_epoch]}), empty_target_set=selected == 'empty')
    return create_context_batch(db, [context])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    if entry_id != APPLY or ref.kind != KIND: raise artifacts.unavailable()
    return _resolve(db, identity, entry_id=entry_id, params={'import_id': ref.id}, branch='existing',
                    read_snapshot=read_snapshot, locked_user=locked_user)


for _entry in (PREVIEW, APPLY, DOCUMENTS, AMBIGUITIES, RESOLVE, RECONCILE):
    foundation.register_entry_resolver(_entry, _resolve, reference_resolver=_reference if _entry == APPLY else None)


def _state(db, target):
    row, _, digest = _receipt_metadata(db, target.id)
    if target.generation != 1: raise artifacts.unavailable()
    ref = foundation.slot_ref(APPLY, 'context_0', row.import_id, selection={
        'input_sha256': row.source_file_hash, 'adapter_version': row.adapter_version})
    return artifacts.ArtifactDomainState(target, ref, digest, row.owner_user_id)


def _document_access(db, identity, document_ref, *, read_snapshot=None, locked_user=None, **_):
    batch = _resolve(db, identity, entry_id=APPLY, params={'import_id': document_ref.id},
                     read_snapshot=read_snapshot, locked_user=locked_user)
    accesses = service.context_accesses(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    require_complete(accesses)
    row, contract, digest = _receipt(db, document_ref.id)
    ref = batch.contexts[0].ref
    current = _parent_rows(db, contract['plan']['documents'], contract['plan']['sources'], contract['plan']['parents'])
    _, evidence = foundation.resolve_project_subjects(db, identity, current['parents'],
        read_snapshot=read_snapshot, locked_user=locked_user)
    # Immutable bytes alone do not bind their current parent topology. This
    # revision deliberately excludes actor/epoch, so rename does not invalidate
    # content while actual assignment/link movement invalidates old receipts.
    content_revision = relation_revision({'receipt': digest, 'topology': current,
        'parents': {pid: value.content_revision for pid, value in evidence.items()}})
    return foundation.DocumentAccess(ref, batch.contexts[0].authorization_revision,
        tuple(accesses.accesses.values()), (ref,), content_revision)


_PROJECTION = {'warehouse_apply_receipt': 1}
def _inspect(content):
    value = json.loads(content)
    if not isinstance(value, dict) or not {'import_id', 'source_file_hash', 'adapter_version'} <= set(value):
        raise artifacts.unavailable()
    return _PROJECTION


artifacts.register_artifact_domain(KIND, artifacts.ArtifactDomainAdapter(_state, _document_access,
    (artifacts.ArtifactFormat('warehouse_apply_receipt_v1', PURPOSE, APPLY, _PROJECTION, _inspect),)))


def _view_value(view):
    if view not in (None, 'authorization'): raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知授权视图')


def read(db, identity, *, entry_id, params, view=None, precondition=None, content=None, filename=None, hmac_key=None):
    _view_value(view)
    params = dict(params)
    if content is not None: params['_parsed'] = business.parse_warehouse_workbook(content)
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry_id, params=params) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if view == 'authorization': return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(foundation.decode_precondition(precondition), accesses, ref)
        if entry_id == PREVIEW:
            result = business.preview_import(content, filename=filename, hmac_key=hmac_key)
        else:
            action = 'read_documents' if entry_id == DOCUMENTS else 'read_ambiguities'
            condition = _condition(accesses.require(ref, WAREHOUSE, action))
            function = business.search_documents if entry_id == DOCUMENTS else business.search_ambiguities
            result = function(snapshot, **params, user_ctx=None, authorization_condition=condition)
        return {**result, 'authorization_context': accesses.wire(ref)}


def _lock_domain(db, *, entry_id, params):
    # Existing assignment/import writers use this outer lock. Domain row locks
    # follow, with the actor acquired only after the entire actual closure.
    db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
    if entry_id == APPLY:
        business._lock_business_identities(db, params['_parsed'])
        plan = _input_plan(db, params['_parsed'])
    elif entry_id == RESOLVE: plan = _resolve_plan(db, params)
    else: plan = _reconcile_plan(db)
    db.execute(select(func.pg_advisory_xact_lock(bridge._INTEGRATION_LOCK_KEY)))
    for model, key, ids in (
        (MaintenanceProject, MaintenanceProject.project_id, plan['parents']),
        (FMaintenanceOrder, FMaintenanceOrder.raw_order_id, plan['sources']),
        (Assignment, Assignment.source_order_id, plan['sources']),
        (MaintenanceProjectUserAssignment, MaintenanceProjectUserAssignment.project_id, plan['parents']),
        (Document, Document.document_id, plan['documents']),
        (Line, Line.document_id, plan['documents']),
        (Link, Link.document_id, plan['documents']),
        (Ambiguity, Ambiguity.document_id, plan['documents']),
        (DimPart, DimPart.id, plan.get('parts', [])),
        (PartAlias, PartAlias.id, [r[0] for r in plan.get('aliases', [])]),
        (MaintenanceSiteIssue, MaintenanceSiteIssue.issue_id,
            list({r[0] for r in plan['issue_parents']} | set(plan.get('target_issues', [])))),
        (MaintenanceBadReturn, MaintenanceBadReturn.return_id,
            list({r[0] for r in plan['return_parents']} | set(plan.get('candidate_returns', []))
                 | set(plan.get('target_returns', [])))),
        (MaintenanceSiteIssueDeliverySource, MaintenanceSiteIssueDeliverySource.source_order_id, plan['sources']),
    ):
        if ids: list(db.scalars(select(model).where(key.in_(ids)).order_by(*model.__table__.primary_key)
            .with_for_update().execution_options(populate_existing=True)))
    if entry_id == APPLY:
        list(db.scalars(select(Batch).where(Batch.import_id == params['import_id']).with_for_update()
            .execution_options(populate_existing=True)))
    elif entry_id == RESOLVE:
        db.scalar(select(Ambiguity).where(Ambiguity.ambiguity_id == params['ambiguity_id']).with_for_update()
            .execution_options(populate_existing=True))
    after = _input_plan(db, params['_parsed']) if entry_id == APPLY else (
        _resolve_plan(db, params) if entry_id == RESOLVE else _reconcile_plan(db))
    if after != plan: raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '仓库单据关系已变化，请重新解析')


def command(db, identity, *, entry_id, params, view=None, precondition=None, content=None, filename=None, hmac_key=None):
    _view_value(view)
    params = dict(params)
    if entry_id == APPLY:
        params['_parsed'] = business.parse_warehouse_workbook(content)
        expected = business._preview_token(params['_parsed'], hmac_key)
        import hmac
        if not hmac.compare_digest(params['preview_token'], expected):
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '文件与预览签名不一致')
    decoded = foundation.decode_precondition(precondition)
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry_id, params=params) as (_, _, batch, accesses):
        ref = batch.contexts[0].ref
        if view == 'authorization': return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(decoded, accesses, ref)
    if entry_id == APPLY: artifacts.begin_artifact_protocol(db.get_bind())
    try:
        _lock_domain(db, entry_id=entry_id, params=params)
        actor = service.lock_actor(db, identity)
        batch = foundation.resolve_entry_context(db, identity, entry_id=entry_id, params=params, locked_user=actor)
        accesses = service.context_accesses(db, identity, batch, locked_user=actor)
        require_complete(accesses)
        ref = batch.contexts[0].ref
        service.check_context_precondition(decoded, accesses, ref)
        if entry_id == APPLY:
            existing = db.get(Batch, params['import_id'])
            if existing:
                manifest = artifacts.require_locked_artifact(db, identity,
                    target=artifacts.ArtifactTarget(KIND, existing.import_id, 1), purpose=PURPOSE,
                    locked_user=actor, authorization_precondition=decoded)
                if hashlib.sha256(canonical_json(existing.result_json).encode()).hexdigest() != manifest['content_sha256']:
                    raise artifacts.unavailable()
            result = business.apply_import(db, content, filename=filename, import_id=params['import_id'],
                preview_token=params['preview_token'], reason=params['reason'], operated_by=actor.username, hmac_key=hmac_key,
                owner_user_id=actor.id, authorization_receipt=lambda session, row: {
                    'schema_version': 1, 'plan': _input_plan(session, params['_parsed']),
                    'result_sha256': relation_revision(row.result_json)})
            if not existing:
                row = db.get(Batch, params['import_id'])
                persistent = foundation.resolve_entry_context(db, identity, entry_id=APPLY, params=params, locked_user=actor)
                current = service.context_accesses(db, identity, persistent, locked_user=actor)
                require_complete(current)
                target = artifacts.ArtifactTarget(KIND, row.import_id, 1)
                _, _, digest = _receipt(db, row.import_id)
                binding = artifacts.register_artifact_binding(db, identity, target=target, input_digest=digest,
                    provenance=captured_provenance(current), locked_user=actor)
                artifacts.publish_artifact(db, identity, binding=binding, purpose=PURPOSE,
                    content=canonical_json(row.result_json).encode(), expected_generation=1, locked_user=actor)
        elif entry_id == RESOLVE:
            result = business.resolve_ambiguity(db, **params, operated_by=actor.username,
                                               user_ctx=None, authorization_condition=None)
        else:
            result = business.reconcile_project_assignment_links(db, operated_by=actor.username, reason=params['reason'])
        result = {**result, 'authorization_context': accesses.wire(ref)}
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
