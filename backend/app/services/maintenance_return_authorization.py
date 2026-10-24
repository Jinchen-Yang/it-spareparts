"""Exact return workflow contracts over current parents, with atomic final authorization.

Domain primitives keep their original locks and invariants. Canonical commands
stage their work, then re-authorize the complete BEFORE target set under the
final actor lock; neither their own version bump nor transfer grants permission.
"""
from dataclasses import replace
import hashlib
import json

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import PolicySubject, canonical_json, create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.maintenance_bad_return import MaintenanceBadReturnCommand as Command, MaintenanceBadReturn as BadReturn, MaintenanceBadReturnLine as BadLine, MaintenanceReturnObligation as Obligation
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage as Salvage
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine as Receipt
from app.models.maintenance_project import MaintenanceProject as Project
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit as Audit
from app.models.maintenance import FMaintenanceOrder as Source
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as Assignment
from app.models.master_data import ProductCategory
from app.services import maintenance_authorization as foundation
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_bad_returns as bad
from app.services import maintenance_bad_salvage as salvage
from app.services import maintenance_return_receipts as receipts
from app.services.maintenance_workflow_authorization import require_complete, captured_provenance

RESOURCES = frozenset(('maintenance_return_obligation', 'maintenance_return_receipt',
                      'maintenance_salvage', 'maintenance_return_categories'))
ENTRIES = {key: value for key, value in ENTRY_CONTRACTS.items()
           if 'default' in value['branches'] and set(value['branches']['default']['requirements']) <= RESOURCES}
READ_ACTIONS = frozenset(('read', 'read_demands', 'read_summary', 'read_receipts', 'read_audit'))


def _selection(entry, params):
    fields = ENTRIES[entry]['slots']['context_0']['selector_schema']['properties']
    return jsonable_encoder({key: value for key, value in params.items() if key in fields})


def _actual(db, entry, params):
    """Metadata only: never use a supplied parent instead of a stored child parent."""
    parents = {params['project_id']} if params.get('project_id') else set()
    rows = []
    carrier_id = params.get('project_id')
    for key, model, pk in (('return_id', BadReturn, 'return_id'),
                           ('obligation_id', Obligation, 'obligation_id'),
                           ('salvage_id', Salvage, 'salvage_id'),
                           ('receipt_id', Receipt, 'rkd_line_id')):
        if key not in params or not params[key]:
            continue
        row = db.execute(select(getattr(model, pk).label('id'), model.project_id, model.version)
            .where(getattr(model, pk) == params[key])).first()
        if row is None:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '返还业务对象不存在')
        parents.add(row.project_id)
        rows.append([key, params[key], row.project_id, row.version])
        if ENTRIES[entry]['slots']['context_0']['kind'] == 'object':
            carrier_id = params[key]
    obligation_ids = {line['obligation_id'] for line in params.get('lines', [])}
    if params.get('return_id'):
        obligation_ids.update(db.scalars(select(BadLine.obligation_id).where(BadLine.return_id == params['return_id'])))
    if obligation_ids:
        found = list(db.execute(select(Obligation.obligation_id, Obligation.project_id, Obligation.version)
            .where(Obligation.obligation_id.in_(obligation_ids))))
        if len(found) != len(obligation_ids):
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '返还义务不存在')
        for row in found:
            parents.add(row.project_id)
            rows.append(['obligation', row.obligation_id, row.project_id, row.version])
    if params.get('replaces_return_id'):
        row = db.execute(select(BadReturn.return_id, BadReturn.project_id, BadReturn.version)
            .where(BadReturn.return_id == params['replaces_return_id'])).first()
        if row is None:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '被替代返还单不存在')
        parents.add(row.project_id)
        rows.append(['replacement', row.return_id, row.project_id, row.version])
    # New WBDD references bind their stable identity and actual assignment. The
    # original receipt's historical link is not a new association on plain edits.
    if params.get('wbdd_no'):
        orders = list(db.scalars(select(Source.raw_order_id).where(Source.order_no == params['wbdd_no'].strip())))
        links = list(db.execute(select(Assignment.assignment_id, Assignment.source_order_id,
            Assignment.project_id, Assignment.version).where(Assignment.source_order_id.in_(orders),
                Assignment.is_active.is_(True)).order_by(Assignment.assignment_id)))
        parents.update(link.project_id for link in links)
        rows.extend([['source', *list(link)] for link in links])
    return sorted(parents), sorted(rows, key=str), carrier_id


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    parents, rows, carrier_id = _actual(db, entry_id, params)
    principal, evidence = foundation.resolve_project_subjects(db, identity, parents,
        read_snapshot=read_snapshot, locked_user=locked_user)
    slot = ENTRIES[entry_id]['slots']['context_0']
    if slot['kind'] == 'collection':
        carrier_id = slot['collection_ref']
    carrier = foundation.slot_ref(entry_id, 'context_0', carrier_id, selection=_selection(entry_id, params))
    requirements = ENTRIES[entry_id]['branches']['default']['requirements']
    subjects = tuple(evidence[pid].subject for pid in parents)
    if not subjects:
        subjects = (PolicySubject(carrier.id, {}),)
    context = foundation.resolved_context(db, entry_id=entry_id, branch='default', carrier=carrier,
        slots={'context_0': (carrier,)}, subjects={r: subjects for r in requirements},
        revision=relation_revision({'carrier': carrier.to_dict(), 'objects': rows,
            'projects': {pid: evidence[pid].authorization_revision for pid in parents},
            'actor': [principal.id, principal.authorization_epoch, principal.salesperson_name]}))
    return create_context_batch(db, [context])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    # Exact producer entry and command input remain paired with its carrier.
    selection = dict(ref.selector.get('selection', {}))
    params = dict(selection)
    if '{return_id}' in entry_id:
        params['return_id'] = ref.id
    elif '{obligation_id}' in entry_id:
        params['obligation_id'] = ref.id
    elif '{receipt_id}' in entry_id:
        params['receipt_id'] = ref.id
    elif '{salvage_id}' in entry_id:
        params['salvage_id'] = ref.id
    elif ref.kind == 'project':
        params['project_id'] = ref.id
    return _resolve(db, identity, entry_id=entry_id, params=params, read_snapshot=read_snapshot, locked_user=locked_user)


for _entry in ENTRIES:
    foundation.register_entry_resolver(_entry, _resolve, reference_resolver=_reference)


KIND = 'return_command'
BAD_COMMANDS = {entry: next(iter(value['branches']['default']['requirements'].values()))[0]
    for entry, value in ENTRIES.items() if 'maintenance_return_obligation' in value['branches']['default']['requirements']
    and next(iter(value['branches']['default']['requirements'].values()))[0] not in READ_ACTIONS}


def _receipt(db, command_id):
    row = db.get(Command, command_id, populate_existing=True)
    if row is None or row.owner_user_id is None or not isinstance(row.response_json, dict):
        raise artifacts.unavailable()
    value = row.response_json
    if value.get('schema_version') != 1 or not isinstance(value.get('authorization_contract'), dict):
        raise artifacts.unavailable()
    contract = value['authorization_contract']
    entry, params = contract.get('entry'), contract.get('params')
    if (entry not in BAD_COMMANDS or not isinstance(params, dict)
            or params.get('idempotency_key') != row.idempotency_key
            or params.get('project_id') != row.project_id
            or row.action != {'mark_in_transit': 'in_transit'}.get(BAD_COMMANDS[entry], BAD_COMMANDS[entry])):
        raise artifacts.unavailable()
    entity_key = 'obligation_id' if row.entity_type == 'return_obligation' else 'return_id'
    if value.get('response', {}).get(entity_key) != row.entity_id:
        raise artifacts.unavailable()
    carrier_id = row.project_id if ENTRIES[entry]['slots']['context_0']['kind'] == 'project' else row.entity_id
    ref = foundation.slot_ref(entry, 'context_0', carrier_id, selection=_selection(entry, params))
    return row, contract, ref, relation_revision({'input': row.request_fingerprint, 'contract': contract,
        'result': value['response'], 'owner': row.owner_user_id})


def _artifact_state(db, target):
    if target.generation != 1:
        raise artifacts.unavailable()
    row, _, ref, digest = _receipt(db, target.id)
    return artifacts.ArtifactDomainState(target, ref, digest, row.owner_user_id)


def _document(db, identity, document_ref, *, entry_id, format_purpose, read_snapshot=None, locked_user=None):
    key = document_ref.selector.get('selection', {}).get('idempotency_key')
    row = db.scalar(select(Command).where(Command.idempotency_key == key))
    if row is None:
        raise artifacts.unavailable()
    row, contract, ref, digest = _receipt(db, row.command_id)
    if entry_id != contract['entry'] or format_purpose != BAD_COMMANDS[entry_id] or document_ref != ref:
        raise artifacts.unavailable()
    batch = _resolve(db, identity, entry_id=entry_id, params=contract['params'],
        read_snapshot=read_snapshot, locked_user=locked_user)
    accesses = service.context_accesses(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    require_complete(accesses)
    # Current child and every actual parent, even for the original create receipt.
    child_params = {**contract['params'], ('obligation_id' if row.entity_type == 'return_obligation' else 'return_id'): row.entity_id}
    parents, objects, _ = _actual(db, entry_id, child_params)
    _, evidence = foundation.resolve_project_subjects(db, identity, parents, read_snapshot=read_snapshot, locked_user=locked_user)
    actual = replace(batch.contexts[0], policy_subjects={resource: tuple(evidence[pid].subject for pid in parents)
        for resource in batch.contexts[0].requirements})
    current = service.context_accesses(db, identity, create_context_batch(db, [actual]), read_snapshot=read_snapshot, locked_user=locked_user)
    require_complete(current)
    return foundation.DocumentAccess(ref, actual.authorization_revision, tuple(current.accesses.values()), (ref,),
        relation_revision({'receipt': digest, 'parents': parents,
            'topology': [[r[0], r[1], r[2]] for r in objects]}))


def _inspect(content):
    value = json.loads(content)
    if not isinstance(value, dict) or not ('return_id' in value or 'obligation_id' in value):
        raise artifacts.unavailable()
    return {'return_command_receipt': 1}


artifacts.register_artifact_domain(KIND, artifacts.ArtifactDomainAdapter(_artifact_state, _document,
    tuple(artifacts.ArtifactFormat('return_command_' + action + '_v1', action, entry,
        {'return_command_receipt': 1}, _inspect) for entry, action in BAD_COMMANDS.items())))


def _audit(db, identity, batch, proof, params):
    context = batch.contexts[0]
    items = receipts.receipt_audit(db, receipt_id=params['receipt_id'])
    targets = []
    for item in items:
        ids = {item['project_id']}
        for value in (item.get('before_json'), item.get('after_json')):
            if value and value.get('project_id'):
                ids.add(value['project_id'])
        targets.append(ids)
    _, evidence = foundation.resolve_project_subjects(db, identity, set().union(*targets) if targets else set(), read_snapshot=proof)
    allowed = {}
    visible = []
    for item, ids in zip(items, targets):
        key = tuple(sorted(ids))
        if key not in allowed:
            historical = replace(context, policy_subjects={'maintenance_return_receipt': tuple(evidence[pid].subject for pid in key)})
            decisions = service.context_accesses(db, identity, create_context_batch(db, [historical]), read_snapshot=proof)
            allowed[key] = decisions.get(context.ref, 'maintenance_return_receipt', 'read_audit') is not None
        if allowed[key]:
            visible.append(item)
    return {'receipt_id': params['receipt_id'], 'items': visible}


def _read(db, entry, params, identity, batch, proof):
    path = entry.split(' ', 1)[1]
    if path.endswith('/return-categories'):
        rows = db.scalars(select(ProductCategory).order_by(ProductCategory.category_major,
            ProductCategory.category_minor.nullsfirst(), ProductCategory.id))
        return {'categories': [{'category_id': r.id, 'category_major': r.category_major,
                                'category_minor': r.category_minor} for r in rows]}
    if path.endswith('/return-obligations/search'):
        return bad.search_return_obligations(db, **params)
    if path.endswith('/bad-returns/search'):
        return bad.search_bad_returns(db, **params)
    if path.endswith('/return-rate'):
        return bad.project_return_rate(db, project_id=params['project_id'])
    if path.endswith('/salvages'):
        return salvage.list_salvage(db, params['project_id'])
    if path.endswith('/return-receipt-summary'):
        return receipts.receipt_summary(db, project_id=params['project_id'])
    if path.endswith('/return-receipt-demands'):
        return receipts.demand_candidates(db, capacity_only=True, **params)
    if path.endswith('/return-receipts'):
        return receipts.search_receipts(db, **params)
    if path.endswith('/audit'):
        return _audit(db, identity, batch, proof, params)
    raise ValueError('Unregistered return read')


def _write(db, entry, params, principal):
    values = {**params, 'operated_by': principal.username}
    contract = {'entry': entry, 'params': jsonable_encoder(params)}
    if entry == 'POST /api/maintenance/bad-returns':
        return bad.create_bad_return(db, **values, owner_user_id=principal.id, authorization_contract=contract)
    if '/bad-returns/{return_id}/' in entry:
        action = entry.rsplit('/', 1)[1].replace('-', '_')
        return bad.transition_bad_return(db, **values, action=action, owner_user_id=principal.id, authorization_contract=contract)
    if entry.endswith('/resolve-category'):
        return bad.resolve_obligation_category(db, **values, owner_user_id=principal.id, authorization_contract=contract)
    if entry == 'POST /api/maintenance/projects/stable/{project_id}/salvages':
        return salvage.register_salvage(db, **values, owner_user_id=principal.id)
    if entry.endswith('/salvages/{salvage_id}/void'):
        return salvage.void_salvage(db, salvage_id=params['salvage_id'],
            version=params['version'], operated_by=principal.username)
    if entry == 'POST /api/maintenance/projects/stable/{project_id}/return-receipts':
        return receipts.register_receipt(db, **values, owner_user_id=principal.id)
    if entry.startswith('PATCH '):
        return receipts.update_receipt(db, receipt_id=params['receipt_id'], expected_version=params['version'],
            reason=params['reason'], operated_by=principal.username,
            updates={k: v for k, v in params.items() if k not in ('receipt_id', 'version', 'reason')})
    if entry.endswith('/return-receipts/{receipt_id}/void'):
        return receipts.void_receipt(db, receipt_id=params['receipt_id'], expected_version=params['version'],
            reason=params['reason'], operated_by=principal.username)
    raise ValueError('Unregistered return command')


def _locks(db, entry, params):
    # Same outer lock as ownership/import writers. Child rows are locked before
    # their authoritative before-observation; domain stock/capacity locks follow.
    db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
    if entry in BAD_COMMANDS:
        bad._lock_idempotency_key(db, params['idempotency_key'])
    parents, _, _ = _actual(db, entry, params)
    db.scalars(select(Project).where(Project.project_id.in_(parents)).order_by(Project.project_id).with_for_update()).all()
    for model, pk in ((BadReturn, BadReturn.return_id), (Obligation, Obligation.obligation_id),
                      (Salvage, Salvage.salvage_id), (Receipt, Receipt.rkd_line_id)):
        db.scalars(select(model).where(model.project_id.in_(parents)).order_by(pk).with_for_update()
                   .execution_options(populate_existing=True)).all()
    if _actual(db, entry, params)[0] != parents:
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '返还对象的实际项目集合已改变')
    return parents


def execute(db, identity, entry, params, *, view=None, precondition=None):
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知授权视图')
    action = next(iter(ENTRIES[entry]['branches']['default']['requirements'].values()))[0]
    factory = lambda: Session(db.get_bind())
    expected = foundation.decode_precondition(precondition)
    with foundation.read_context_snapshot(factory, identity, entry_id=entry, params=params) as (snapshot, proof, batch, accesses):
        ref = batch.contexts[0].ref
        if view == 'authorization':
            return JSONResponse(jsonable_encoder(service.context_resolution(accesses, ref)), headers={"Cache-Control": "no-store"})
        require_complete(accesses)
        service.check_context_precondition(expected, accesses, ref)
        if action in READ_ACTIONS:
            payload = _read(snapshot, entry, params, identity, batch, proof)
            if payload is None:
                raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '业务对象不存在')
            return {**payload, 'authorization_context': accesses.wire(ref)}
    if entry in BAD_COMMANDS:
        artifacts.begin_artifact_protocol(db.get_bind())
    try:
        parents = _locks(db, entry, params)
        with foundation.read_context_snapshot(factory, identity, entry_id=entry, params=params) as (_, _, locked_batch, before):
            require_complete(before)
            ref = locked_batch.contexts[0].ref
            service.check_context_precondition(expected, before, ref)
            captured = locked_batch.contexts
            if any(subject.id not in parents for context in captured
                   for subjects in context.policy_subjects.values() for subject in subjects):
                raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '返还对象的锁后项目集合已改变')
            principal = before.principal
        payload = _write(db, entry, params, principal)
        if payload is None:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '业务对象不存在')
        db.flush()
        actor = service.lock_actor(db, identity)
        current, evidence = foundation.resolve_project_subjects(db, identity, parents, locked_user=actor)
        if current.id != principal.id or current.authorization_epoch != principal.authorization_epoch:
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '账号权限已改变，请重新确认')
        after_parents, _, _ = _actual(db, entry, params)
        if not set(after_parents).issubset(parents):
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '业务实际目标超出已核验集合')
        contexts = [replace(context, policy_subjects={r: tuple(evidence[s.id].subject for s in subjects)
            for r, subjects in context.policy_subjects.items()}) for context in captured]
        final = service.context_accesses(db, identity, create_context_batch(db, contexts), locked_user=actor)
        require_complete(final)
        service.check_context_precondition(expected, final, ref)
        if entry in BAD_COMMANDS:
            row = db.scalar(select(Command).where(Command.idempotency_key == params['idempotency_key']))
            target = artifacts.ArtifactTarget(KIND, row.command_id, 1)
            purpose = BAD_COMMANDS[entry]
            stored, _, _, digest = _receipt(db, row.command_id)
            content = canonical_json(stored.response_json['response']).encode()
            if payload.get('idempotent_replay'):
                manifest = artifacts.require_locked_artifact(db, identity, target=target, purpose=purpose, locked_user=actor)
                if hashlib.sha256(content).hexdigest() != manifest['content_sha256']:
                    raise artifacts.unavailable()
            else:
                persistent = _resolve(db, identity, entry_id=entry, params=params, locked_user=actor)
                provenance = service.context_accesses(db, identity, persistent, locked_user=actor)
                require_complete(provenance)
                binding = artifacts.register_artifact_binding(db, identity, target=target, input_digest=digest,
                    provenance=captured_provenance(provenance), locked_user=actor)
                artifacts.publish_artifact(db, identity, binding=binding, purpose=purpose, content=content,
                    expected_generation=1, locked_user=actor)
        result = {**payload, 'authorization_context': final.wire(ref)}
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
