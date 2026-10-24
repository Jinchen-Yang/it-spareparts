"""Demand contracts: actual source/line/intent ancestry, whole commands, current receipts."""
from dataclasses import replace
from datetime import date
import hashlib
import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, text, literal_column, true
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import PolicySubject, create_context_batch, relation_revision, canonical_json, plain
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.maintenance import (FMaintenanceOrder as Order, FMaintenanceLine as Line,
    MaintenanceDemandDeleteIntent as Intent, MaintenanceDemandDeleteIntentItem as Item,
    MaintenanceDemandTombstone as Tombstone)
from app.models.authorization import SysAuthorizationArtifact
from app.models.maintenance_project import MaintenanceProject as Project
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as Assignment
from app.security import UserContext
from app.services import maintenance_authorization as foundation
from app.services import maintenance_demands as business
from app.services import maintenance_demand_manual as manual
from app.services import authorization_artifacts as artifacts
from app.services.maintenance_workflow_authorization import require_complete, captured_provenance

RESOURCE = 'maintenance_demand'
BASE = '/api/maintenance/demands'
SEARCH = 'POST '+BASE+'/search'
FAST = 'POST '+BASE+'/void-fast'
CREATE_INTENT = 'POST '+BASE+'/delete-intents'
GET_INTENT = 'GET '+BASE+'/delete-intents/{intent_id}'
ARM = 'POST '+BASE+'/delete-intents/{intent_id}/arm'
EXECUTE = 'POST '+BASE+'/delete-intents/{intent_id}/execute'
CANCEL = 'POST '+BASE+'/delete-intents/{intent_id}/cancel'
RESTORE = 'POST '+BASE+'/{source_order_id}/restore'
REVIEW = 'GET '+BASE+'/manual-protection-review'
PATCH = 'PATCH '+BASE+'/lines/{raw_line_id}'
CREATE = 'POST '+BASE+'/lines'
CLEAR = 'POST '+BASE+'/lines/{raw_line_id}/clear-override'
LINES = 'GET '+BASE+'/orders/{source_order_id}/lines'
ENTRIES = {entry: rule for entry, rule in ENTRY_CONTRACTS.items() if entry.split(' ', 1)[1].startswith(BASE+'/')}
READS = {SEARCH, GET_INTENT, REVIEW, LINES}
COLLECTIONS = {SEARCH, REVIEW}


def _selection(entry, slot, params):
    values = {**params}
    if entry == PATCH:
        values['updates_plan_sha256'] = relation_revision(jsonable_encoder(params['updates']))
    fields = ENTRIES[entry]['slots'][slot]['selector_schema']['properties']
    return jsonable_encoder({k: v for k, v in values.items() if k in fields})


def _intent(db, intent_id, principal):
    row = db.get(Intent, intent_id, populate_existing=True)
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '删除意图不存在')
    if row.owner_user_id is None or row.owner_user_id != principal.id:
        raise authz_error(403, 'AUTHZ_DENIED', '不能认领其他账号或历史删除意图')
    return row


def _intent_metadata(db, intent_id, principal):
    row = db.execute(select(Intent.intent_id, Intent.owner_user_id, Intent.status,
        Intent.request_digest, Intent.selection_digest, Intent.not_before, Intent.expires_at)
        .where(Intent.intent_id == intent_id)).first()
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '删除意图不存在')
    if row.owner_user_id is None or row.owner_user_id != principal.id:
        raise authz_error(403, 'AUTHZ_DENIED', '不能认领其他账号或历史删除意图')
    return row


def _actual(db, entry, params, principal):
    """Identity and relationship columns only; no facts or receipt bodies in the view."""
    sources = set()
    parents = {params['project_id']} if params.get('project_id') else set()
    metadata = []
    intent = None
    if entry in (FAST, CREATE_INTENT) and (params.get('idempotency_key') or params.get('_receipt_id')):
        intent_id = params.get('_receipt_id') or db.scalar(select(Intent.intent_id).where(Intent.idempotency_key == params['idempotency_key']))
        if intent_id:
            intent = _intent_metadata(db, intent_id, principal)
    elif params.get('intent_id'):
        intent = _intent_metadata(db, params['intent_id'], principal)
    if intent is not None:
        rows = list(db.execute(select(Item.source_order_id, Item.version_digest,
            Item.snapshot_json['active_project_assignment'].label('assignment')).where(Item.intent_id == intent.intent_id).order_by(Item.ordinal)))
        sources.update(row.source_order_id for row in rows)
        parents.update(row.assignment['project_id'] for row in rows if row.assignment)
        metadata.extend([['intent_item', row.source_order_id, row.version_digest, row.assignment] for row in rows])
        # FAST records include already-voided sources which have no new Item.
        recorded = db.scalar(select(Intent.result_json['source_order_ids']).where(Intent.intent_id == intent.intent_id))
        if recorded:
            sources.update(recorded)
        metadata.append(['intent', intent.intent_id, intent.owner_user_id, intent.status,
            intent.request_digest, intent.selection_digest, str(intent.not_before), str(intent.expires_at)])
    if entry == CREATE and params.get('idempotency_key'):
        # The actual manual source is named by a stable actor/key namespace.
        # Existing/replayed and newly staged rows must still belong to the input
        # project; do not validate only the submitted project on the final pass.
        digest_key = hashlib.sha1(f"authz-v1:demand-create:{principal.id}:{params['idempotency_key']}".encode()).hexdigest()
        existing_source = db.scalar(select(Order.raw_order_id).where(Order.raw_order_id == 'page-manual-'+digest_key))
        if existing_source:
            sources.add(existing_source)
    if 'source_order_ids' in params:
        sources.update(business._validate_source_order_ids(params['source_order_ids']))
    if params.get('source_order_id'):
        sources.add(business._validate_source_order_id(params['source_order_id']))
    if params.get('raw_line_id'):
        row = db.execute(select(Line.id, Line.raw_line_id, Line.order_id, Order.raw_order_id,
            literal_column('f_maintenance_line.xmin::text').label('revision'))
            .join(Order, Order.id == Line.order_id).where(Line.raw_line_id == params['raw_line_id'])).first()
        if row is None:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '需求单明细不存在')
        sources.add(row.raw_order_id)
        metadata.append(['line', *list(row)])
    if sources:
        rows = list(db.execute(select(Order.raw_order_id, Order.id, Order.import_batch_id, Order.data_status,
            literal_column('f_maintenance_order.xmin::text').label('revision')).where(Order.raw_order_id.in_(sources)).order_by(Order.raw_order_id)))
        if len(rows) != len(sources):
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '所选需求单不存在')
        metadata.extend([['source', *list(row)] for row in rows])
    links = list(db.execute(select(Assignment.assignment_id, Assignment.source_order_id,
        Assignment.project_id, Assignment.version, Assignment.is_active)
        .where(Assignment.source_order_id.in_(sources)).order_by(Assignment.assignment_id))) if sources else []
    active = {row.source_order_id for row in links if row.is_active}
    parents.update(row.project_id for row in links if row.is_active)
    metadata.extend([['assignment', *list(row)] for row in links])
    # A tombstone owns its historical project relationship even after the source
    # assignment is deactivated. Never resolve it from a client parent or username.
    dead = list(db.execute(select(Tombstone.source_order_id, Tombstone.delete_intent_id,
        Tombstone.version, Tombstone.restored_at).where(Tombstone.source_order_id.in_(sources)))) if sources else []
    historical = set()
    for row in dead:
        prior = db.scalar(select(Item.snapshot_json['active_project_assignment']).where(
            Item.intent_id == row.delete_intent_id, Item.source_order_id == row.source_order_id))
        if prior:
            parents.add(prior['project_id']); historical.add(row.source_order_id)
        metadata.append(['tombstone', row.source_order_id, row.delete_intent_id, row.version, str(row.restored_at), prior])
    unassigned = sources - active - historical
    return sorted(parents), sorted(sources), sorted(unassigned), metadata, intent


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    principal = foundation._named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    if entry_id in COLLECTIONS:
        ref = foundation.slot_ref(entry_id, 'context_0', 'maintenance_demand_selection', selection=_selection(entry_id, 'context_0', params))
        context = foundation.resolved_context(db, entry_id=entry_id, branch='default', carrier=ref,
            slots={'context_0': (ref,)}, subjects={RESOURCE: ()}, revision=foundation.project_collection_revision(db, principal, ref))
        return create_context_batch(db, [context])
    parents, sources, unassigned, metadata, intent = _actual(db, entry_id, params, principal)
    evidence = foundation.project_evidence(db, principal, parents)
    selected = 'default'
    if entry_id == FAST and intent is not None:
        selected = 'replay'
        ref = foundation.slot_ref(entry_id, 'context_receipt', intent.intent_id,
            selection=_selection(entry_id, 'context_receipt', params))
        slots = {'context_receipt': (ref,)}
    elif entry_id in (FAST, CREATE_INTENT):
        plan = {'command': _selection(entry_id, 'context_0', params), 'sources': sources, 'objects': metadata, 'parents': parents}
        digest = relation_revision(plan)
        ref = foundation.slot_ref(entry_id, 'context_1', digest, selection={'command_plan_sha256': digest})
        discovery = foundation.slot_ref(entry_id, 'context_0', 'maintenance_demand_selection', selection=plan['command'])
        slots = {'context_0': (discovery,), 'context_1': (ref,)}
    else:
        object_id = params.get('intent_id') or params.get('raw_line_id') or params.get('source_order_id') or params.get('project_id')
        ref = foundation.slot_ref(entry_id, 'context_0', object_id, selection=_selection(entry_id, 'context_0', params))
        slots = {'context_0': (ref,)}
    if branch is not None and selected != branch:
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '需求单授权分支已改变')
    subjects = tuple(evidence[pid].subject for pid in parents) + tuple(PolicySubject('source:'+sid,
        {'is_manager': False, 'is_viewer': False, 'is_sales': False, 'is_active': True}) for sid in unassigned)
    if not subjects:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '需求单命令缺少实际目标')
    context = foundation.resolved_context(db, entry_id=entry_id, branch=selected, carrier=ref, slots=slots,
        subjects={RESOURCE: subjects}, revision=relation_revision({'carrier': ref.to_dict(), 'objects': metadata,
            'projects': {pid: evidence[pid].authorization_revision for pid in parents},
            'actor': [principal.id, principal.authorization_epoch, principal.salesperson_name]}))
    return create_context_batch(db, [context])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    params = plain(ref.selector).get('selection', {})
    key = 'intent_id' if '{intent_id}' in entry_id else 'raw_line_id' if '{raw_line_id}' in entry_id else 'source_order_id' if '{source_order_id}' in entry_id else 'project_id'
    if entry_id == FAST:
        params['_receipt_id'] = ref.id
    else:
        params[key] = ref.id
    return _resolve(db, identity, entry_id=entry_id, params=params, branch=branch, read_snapshot=read_snapshot, locked_user=locked_user)


for _entry in ENTRIES:
    foundation.register_entry_resolver(_entry, _resolve, reference_resolver=None if _entry in COLLECTIONS or _entry in (PATCH, CREATE_INTENT) else _reference)


KIND = 'demand_delete_receipt'


def _receipt(db, target):
    row = db.get(Intent, target.id, populate_existing=True)
    if row is None or row.owner_user_id is None or target.generation != 1 or row.status != 'executed' or not isinstance(row.result_json, dict):
        raise artifacts.unavailable()
    ref = foundation.slot_ref(GET_INTENT, 'context_0', row.intent_id)
    digest = relation_revision({'request': row.request_digest, 'selection': row.selection_digest,
        'owner': row.owner_user_id, 'result': row.result_json,
        'items': list(db.scalars(select(Item.snapshot_json).where(Item.intent_id == row.intent_id).order_by(Item.ordinal)))})
    return row, ref, digest


def _state(db, target):
    row, ref, digest = _receipt(db, target)
    return artifacts.ArtifactDomainState(target, ref, digest, row.owner_user_id)


def _producer(db, row):
    if row.result_json.get('mode') == 'void_fast':
        return FAST, {'source_order_ids': row.result_json['source_order_ids'], 'reason': row.reason,
            'idempotency_key': row.idempotency_key, '_receipt_id': row.intent_id}
    return EXECUTE, {'intent_id': row.intent_id, 'digest': row.selection_digest}


def _producer_ref(db, state):
    row, _, _ = _receipt(db, state.target)
    entry, params = _producer(db, row)
    return foundation.slot_ref(entry, 'context_receipt' if entry == FAST else 'context_0', row.intent_id,
        selection=_selection(entry, 'context_receipt' if entry == FAST else 'context_0', params))


def _document(db, identity, ref, *, entry_id, format_purpose, read_snapshot=None, locked_user=None):
    row, expected, digest = _receipt(db, artifacts.ArtifactTarget(KIND, ref.id, 1))
    producer, command = _producer(db, row)
    if ref != expected or (entry_id not in (GET_INTENT, producer)):
        raise artifacts.unavailable()
    params = {'intent_id': row.intent_id} if entry_id == GET_INTENT else command
    batch = _resolve(db, identity, entry_id=entry_id, params=params, read_snapshot=read_snapshot, locked_user=locked_user)
    current = service.context_accesses(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    require_complete(current)
    principal = next(iter(current.accesses.values())).principal
    parents, sources, unassigned, _, _ = _actual(db, entry_id, params, principal)
    producer_ref = _producer_ref(db, artifacts.ArtifactDomainState(artifacts.ArtifactTarget(KIND, row.intent_id, 1), ref, digest, row.owner_user_id))
    refs = (ref,) if ref == producer_ref else (ref, producer_ref)
    return foundation.DocumentAccess(ref, batch.contexts[0].authorization_revision, tuple(current.accesses.values()), refs,
        relation_revision({'receipt': digest, 'parents': parents, 'sources': sources, 'unassigned': unassigned}))


def _inspect(content):
    value = json.loads(content)
    if not isinstance(value, dict) or not isinstance(value.get('source_order_ids'), list):
        raise artifacts.unavailable()
    return {'demand_delete_result': 1}


artifacts.register_artifact_domain(KIND, artifacts.ArtifactDomainAdapter(_state, _document, tuple(
    artifacts.ArtifactFormat('demand_'+mode+'_'+consumer+'_v1', mode+'_'+consumer,
        GET_INTENT if consumer == 'read' else producer, {'demand_delete_result': 1}, _inspect,
        producer_entry_id=producer, producer_carrier_kind='demand_delete_intent', producer_ref=_producer_ref)
    for mode, producer in (('fast', FAST), ('execute', EXECUTE)) for consumer in ('read', 'replay'))))


def _verify_receipt(db, identity, row, *, proof=None, actor=None, consumer='read'):
    target = artifacts.ArtifactTarget(KIND, row.intent_id, 1)
    purpose = ('fast' if row.result_json.get('mode') == 'void_fast' else 'execute')+'_'+consumer
    if actor is not None:
        manifest = artifacts.require_locked_artifact(db, identity, target=target, purpose=purpose, locked_user=actor)
    else:
        manifest = artifacts.require_artifact(db, identity, target=target, purpose=purpose, read_snapshot=proof)
    if hashlib.sha256(canonical_json(row.result_json).encode()).hexdigest() != manifest['content_sha256']:
        raise artifacts.unavailable()


def _read(db, identity, entry, params, batch, accesses, proof):
    ref = batch.contexts[0].ref
    action = next(iter(batch.contexts[0].requirements[RESOURCE]))
    access = accesses.require(ref, RESOURCE, action)
    if entry in COLLECTIONS:
        visible = None if access.predicate.compare(true()) else set(db.scalars(select(Project.project_id).where(foundation.project_condition(access))))
        if entry == SEARCH:
            context = service.field_context(access, UserContext(user_id=access.principal.username, role=access.principal.role))
            return business.search_demands(db, **params, allowed_project_ids=visible, user_ctx=context)
        return manual.list_manual_protection_review(db, allowed_project_ids=visible, **params)
    if entry == GET_INTENT:
        row = _intent(db, params['intent_id'], access.principal)
        if row.result_json is not None:
            _verify_receipt(db, identity, row, proof=proof)
        return business.get_delete_intent(db, intent_id=row.intent_id, operated_by=access.principal.username, owner_user_id=access.principal.id)
    if entry == LINES:
        from app.api.maintenance_demands import _list_demand_lines_data
        return _list_demand_lines_data(db, params['source_order_id'], visible=None)
    raise ValueError('Unregistered demand read')


def _write(db, entry, params, principal):
    common = {'operated_by': principal.username}
    if entry in (FAST, CREATE_INTENT):
        operation = business.void_fast if entry == FAST else business.create_delete_intent
        return operation(db, **params, **common, owner_user_id=principal.id)
    if entry in (ARM, EXECUTE, CANCEL):
        operation = {ARM: business.arm_delete_intent, EXECUTE: business.execute_delete_intent, CANCEL: business.cancel_delete_intent}[entry]
        return operation(db, **params, **common, owner_user_id=principal.id)
    if entry == RESTORE:
        return business.restore_demand(db, **params, **common)
    if entry == PATCH:
        return manual.patch_demand_line(db, **params, **common)
    if entry == CLEAR:
        return manual.clear_override(db, **{k: v for k, v in params.items() if k != 'field_name'}, field=params['field_name'], **common)
    if entry == CREATE:
        try:
            parsed = date.fromisoformat(params['order_date'])
        except ValueError:
            raise HTTPException(400, 'order_date 必须是 YYYY-MM-DD') from None
        return manual.create_manual_demand_line(db, **{**params, 'order_date': parsed}, **common, owner_user_id=principal.id)
    raise ValueError('Unregistered demand command')


def _locks(db, entry, params, principal):
    # Preserve the domain idempotency/global/state/source/assignment/line order.
    if entry in (FAST, CREATE_INTENT) and params.get('idempotency_key'):
        db.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': 'maintenance-demand-delete-intent:'+params['idempotency_key']})
    db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
    parents, sources, _, _, intent = _actual(db, entry, params, principal)
    if intent is not None:
        db.get(Intent, intent.intent_id, with_for_update=True, populate_existing=True)
    from app.services import maintenance_project_operations as operations
    operations.lock_workbook_states(db, project_ids=parents)
    db.scalars(select(Project).where(Project.project_id.in_(parents)).order_by(Project.project_id).with_for_update()).all()
    orders = db.scalars(select(Order).where(Order.raw_order_id.in_(sources)).order_by(Order.raw_order_id).with_for_update().execution_options(populate_existing=True)).all()
    db.scalars(select(Assignment).where(Assignment.source_order_id.in_(sources)).order_by(Assignment.source_order_id, Assignment.assignment_id).with_for_update().execution_options(populate_existing=True)).all()
    db.scalars(select(Line).where(Line.order_id.in_([order.id for order in orders])).order_by(Line.order_id, Line.raw_line_id).with_for_update().execution_options(populate_existing=True)).all()
    db.scalars(select(Tombstone).where(Tombstone.source_order_id.in_(sources)).order_by(Tombstone.source_order_id).with_for_update().execution_options(populate_existing=True)).all()
    after = _actual(db, entry, params, principal)
    if after[0] != parents or after[1] != sources:
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '需求单实际目标已改变')
    return parents, sources


def execute(db, identity, entry, params, *, view=None, precondition=None):
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知授权视图')
    if entry == SEARCH and params.get('q') is not None and len(params['q']) > 128:
        raise HTTPException(422, '维保需求单搜索条件无效')
    factory = lambda: Session(db.get_bind())
    expected = foundation.decode_precondition(precondition)
    with foundation.read_context_snapshot(factory, identity, entry_id=entry, params=params) as (snapshot, proof, batch, accesses):
        ref = batch.contexts[0].ref
        if view == 'authorization':
            return JSONResponse(jsonable_encoder(service.context_resolution(accesses, ref)), headers={'Cache-Control': 'no-store'})
        require_complete(accesses)
        service.check_context_precondition(expected, accesses, ref)
        principal = next(iter(accesses.accesses.values())).principal
        if entry in READS:
            return {**_read(snapshot, identity, entry, params, batch, accesses, proof), 'authorization_context': accesses.wire(ref)}
    if entry in (FAST, EXECUTE, CREATE):
        artifacts.begin_artifact_protocol(db.get_bind())
    try:
        parents, sources = _locks(db, entry, params, principal)
        # A fresh RR observation after every domain lock catches waits, reparenting
        # and revocation without taking actor locks before a remaining domain lock.
        with foundation.read_context_snapshot(factory, identity, entry_id=entry, params=params) as (_, _, locked, before):
            require_complete(before)
            ref = locked.contexts[0].ref
            service.check_context_precondition(expected, before, ref)
            captured = locked.contexts
            principal = next(iter(before.accesses.values())).principal
        prior_intent = db.get(Intent, params["intent_id"], populate_existing=True) if entry == EXECUTE else None
        was_executed = prior_intent is not None and prior_intent.status == "executed"
        conflict = None
        try:
            payload = _write(db, entry, params, principal)
        except business.DeleteIntentConflict as exc:
            if entry not in (ARM, EXECUTE, CANCEL):
                raise
            # The old state machine persists a terminal conflict. Its evidence
            # is committed only after the same final authorization as a success.
            conflict = exc
            payload = {}
        db.flush()
        actor = service.lock_actor(db, identity)
        current, evidence = foundation.resolve_project_subjects(db, identity, parents, locked_user=actor)
        if current.id != principal.id or current.authorization_epoch != principal.authorization_epoch:
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '账号权限已改变，请重新确认')
        after = _actual(db, entry, params, current)
        if not set(after[0]).issubset(parents) or (entry != CREATE and not set(after[1]).issubset(sources)):
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '需求单实际目标超出已核验集合')
        contexts = [replace(c, policy_subjects={RESOURCE: tuple(evidence[s.id].subject if s.id in evidence else s for s in c.policy_subjects[RESOURCE])}) for c in captured]
        final = service.context_accesses(db, identity, create_context_batch(db, contexts), locked_user=actor)
        require_complete(final)
        service.check_context_precondition(expected, final, ref)
        if conflict is not None:
            db.commit()
            raise conflict
        if entry in (FAST, EXECUTE):
            row = _intent(db, payload['intent_id'], current)
            target = artifacts.ArtifactTarget(KIND, row.intent_id, 1)
            mode = 'fast' if entry == FAST else 'execute'
            # Existing bindings prove this is replay. A legacy receipt is never
            # claimed merely because its username or idempotency key matches.
            existed = db.scalar(select(SysAuthorizationArtifact.domain_id).where(
                SysAuthorizationArtifact.domain_kind == KIND,
                SysAuthorizationArtifact.domain_id == row.intent_id))
            if existed:
                _verify_receipt(db, identity, row, actor=actor, consumer='replay')
            else:
                # Executed before this request but without a manifest is not ours.
                if captured[0].branch == 'replay' or was_executed:
                    raise artifacts.unavailable()
                producer, command = _producer(db, row)
                persistent = _resolve(db, identity, entry_id=producer, params=command, locked_user=actor)
                provenance = service.context_accesses(db, identity, persistent, locked_user=actor)
                require_complete(provenance)
                _, _, digest = _receipt(db, target)
                binding = artifacts.register_artifact_binding(db, identity, target=target, input_digest=digest,
                    provenance=captured_provenance(provenance), locked_user=actor)
                for consumer in ('read', 'replay'):
                    binding = artifacts.load_artifact_binding(db, target=target)
                    artifacts.publish_artifact(db, identity, binding=binding, purpose=mode+'_'+consumer,
                        content=canonical_json(row.result_json).encode(), expected_generation=1, locked_user=actor)
        result = {**payload, 'authorization_context': final.wire(ref)}
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
