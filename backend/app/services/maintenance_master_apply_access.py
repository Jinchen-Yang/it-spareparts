"""Locked master writes and immutable, stable-owner application receipts.

The initial upload is never a replay capability. A replay resolves a real
Operation/Audit pair, checks its immutable input/output and authorizes all of
its original parents again. No username is used as a domain owner.
"""
from dataclasses import replace
import hashlib
import json

from sqlalchemy import select, func, cast, Text
from types import SimpleNamespace
from sqlalchemy.orm import sessionmaker

from app.authz import service
from app.authz.context import ObjectRef, create_context_batch, read_snapshot, relation_revision, canonical_json
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookOperation, MaintenanceProjectOperationAudit
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_master_command_access as command
from app.services import maintenance_project_master_workbook as master
from app.services.maintenance_workflow_authorization import require_complete, captured_provenance

KIND = 'master_workbook_operation'
PURPOSE = 'apply_receipt'
_CONTRACT_KEYS = {'schema_version', 'branch', 'project_id', 'parent_ids', 'sources', 'file_sha256',
                  'command_plan_sha256', 'result_sha256'}


def operation_key(plan, user_id, *, file_sha256=None):
    version = 'v2' if type(plan) is master.MasterV2Plan else 'v1'
    digest = file_sha256 if file_sha256 is not None else plan.file_sha256
    return f'authz-master-{version}:' + artifacts.stable_operation_key(user_id, f'master_{version}_apply',
        hashlib.sha256(canonical_json([plan.project_id, getattr(plan, 'export_id', None), digest]).encode()).hexdigest())


def _receipt(db, receipt_id, *, metadata_only=False):
    if type(receipt_id) is not str or not receipt_id.isdecimal() or int(receipt_id) <= 0:
        raise artifacts.unavailable()
    row = db.get(MaintenanceProjectWorkbookOperation, int(receipt_id), populate_existing=True)
    if row is None or row.owner_user_id is None or row.operation_type != 'file_apply':
        raise artifacts.unavailable()
    model = MaintenanceProjectOperationAudit
    predicate = (model.project_id == row.project_id,
        model.entity_type == 'master_workbook_operation', model.entity_id == str(row.id),
        model.action == 'apply_completed', model.operated_at == row.created_at)
    audit_headers = list(db.execute(select(model.id, model.operated_by,
        model.after_json['schema'].astext.label('schema'),
        model.after_json['authorization_contract'].label('contract'),
        func.encode(func.sha256(func.convert_to(cast(model.after_json, Text), 'UTF8')), 'hex').label('audit_digest')
    ).where(*predicate)))
    if len(audit_headers) != 1:
        raise artifacts.unavailable()
    header = audit_headers[0]
    if metadata_only:
        audits = [SimpleNamespace(id=header.id, operated_by=header.operated_by,
            after_json={'schema': header.schema, 'authorization_contract': header.contract})]
    else:
        audits = list(db.scalars(select(model).where(*predicate)))
    if len(audits) != 1:
        raise artifacts.unavailable()
    audit = audits[0]
    envelope = audit.after_json
    if type(envelope) is not dict:
        raise artifacts.unavailable()
    if envelope.get('schema') == 'master_v2_apply_receipt_v1':
        saved = True if metadata_only else master._v2_stored_apply_result(db, row)
        key_plan = master.MasterV2Plan(row.project_id, (), export_id=row.export_id, file_sha256=row.file_sha256)
    elif envelope.get('schema') == 'master_v1_apply_receipt_v1':
        response = envelope.get('response')
        if not metadata_only and (set(envelope) != {'schema', 'operation_key', 'response', 'authorization_contract'}
                or envelope['operation_key'] != row.operation_key or audit.operated_by != row.operated_by
                or row.export_id is not None or type(response) is not dict
                or response.get('applied_by') != row.operated_by or response.get('replayed') is not False):
            raise artifacts.unavailable()
        saved = True if metadata_only else response
        key_plan = master.MasterPlan(row.project_id)
    else:
        raise artifacts.unavailable()
    contract = audit.after_json.get('authorization_contract') if type(audit.after_json) is dict else None
    if (saved is None or type(contract) is not dict or set(contract) != _CONTRACT_KEYS
            or contract['schema_version'] != 1 or type(contract['schema_version']) is not int
            or contract['project_id'] != row.project_id or contract['file_sha256'] != row.file_sha256
            or type(contract['parent_ids']) is not list or not contract['parent_ids']
            or any(type(pid) is not str or not pid for pid in contract['parent_ids'])
            or contract['parent_ids'] != sorted(set(contract['parent_ids']))
            or row.project_id not in contract['parent_ids'] or type(contract['sources']) is not list):
        raise artifacts.unavailable()
    branch = contract['branch']
    if type(branch) is not str or branch.startswith('replay_') or 'replay_' + branch not in ENTRY_CONTRACTS[command.APPLY]['branches']:
        raise artifacts.unavailable()
    if not metadata_only and relation_revision(audit.after_json['response']) != contract['result_sha256']:
        raise artifacts.unavailable()
    source_ids = set()
    for source in contract['sources']:
        if (type(source) is not dict or set(source) != {'source_order_id', 'old_project_id', 'new_project_id'}
                or type(source['source_order_id']) is not str or not source['source_order_id']
                or source['source_order_id'] in source_ids or source['new_project_id'] != row.project_id
                or (source['old_project_id'] is not None and source['old_project_id'] not in contract['parent_ids'])):
            raise artifacts.unavailable()
        source_ids.add(source['source_order_id'])
    expected_key = operation_key(key_plan, row.owner_user_id, file_sha256=row.file_sha256)
    if row.operation_key != expected_key or row.payload_hash != row.file_sha256:
        raise artifacts.unavailable()
    digest = relation_revision({'operation': {'id': row.id, 'owner_user_id': row.owner_user_id,
        'project_id': row.project_id, 'export_id': row.export_id, 'input': row.file_sha256,
        'operation_key': row.operation_key, 'operated_by': row.operated_by},
        'audit_id': audit.id, 'audit_digest': header.audit_digest})
    return row, audit, contract, digest


def _state(db, target):
    if target.generation != 1:
        raise artifacts.unavailable()
    row, _, _, digest = _receipt(db, target.id, metadata_only=True)
    return artifacts.ArtifactDomainState(target, ObjectRef(KIND, str(row.id)), digest, row.owner_user_id)


def _replay_context(db, identity, *, receipt_id, read_snapshot=None, locked_user=None):
    row, _, contract, digest = _receipt(db, receipt_id, metadata_only=True)
    principal, evidence = foundation.resolve_project_subjects(db, identity, contract['parent_ids'],
        read_snapshot=read_snapshot, locked_user=locked_user)
    if principal.id != row.owner_user_id:
        raise artifacts.unavailable()
    # This historical result cannot claim a source subsequently moved outside
    # the original closure. Re-generation is required instead of scope guessing.
    current_sources = []
    for source in contract['sources']:
        current = db.scalar(select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == source['source_order_id'],
            MaintenanceSourceOrderAssignment.is_active.is_(True)))
        exists = db.scalar(select(FMaintenanceOrder.id).where(
            FMaintenanceOrder.raw_order_id == source['source_order_id']))
        if exists is None or current is None or current.project_id not in contract['parent_ids']:
            raise artifacts.unavailable()
        current_sources.append([source['source_order_id'], current.assignment_id, current.version, current.project_id])
    branch = 'replay_' + contract['branch']
    rule = ENTRY_CONTRACTS[command.APPLY]['branches'][branch]
    ref = foundation.slot_ref(command.APPLY, 'receipt', str(row.id))
    parent = foundation.slot_ref(command.APPLY, 'context_0', row.project_id, selection={
        'file_sha256': row.file_sha256, 'command_plan_sha256': contract['command_plan_sha256']})
    slots = {'context_0': (parent,), 'receipt': (ref,)}
    if contract['sources']:
        slots['source_orders'] = tuple(foundation.slot_ref(command.APPLY, 'source_orders', source['source_order_id'])
            for source in contract['sources'])
        slots['source_projects'] = tuple(foundation.slot_ref(command.APPLY, 'source_projects', pid)
            for pid in contract['parent_ids'])
    # The complete original parent set remains mandatory for replay. Each grant
    # supplies its own fields and scope over these current real relationships.
    subjects = {resource: tuple(evidence[pid].subject for pid in
        (contract['parent_ids'] if resource == 'maintenance_general_workbook_write' and contract['sources'] else [row.project_id]))
        for resource in rule['requirements']}
    context = foundation.resolved_context(db, entry_id=command.APPLY, branch=branch,
        carrier=ref, slots=slots, subjects=subjects, revision=relation_revision({
            'receipt': digest, 'actor': [principal.id, principal.authorization_epoch],
            'current_sources': current_sources,
            'parents': {pid: item.authorization_revision for pid, item in evidence.items()}}))
    return create_context_batch(db, [context]), digest


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    if entry_id != command.APPLY or ref.kind != KIND or ref.selector:
        raise artifacts.unavailable()
    batch, _ = _replay_context(db, identity, receipt_id=ref.id,
        read_snapshot=read_snapshot, locked_user=locked_user)
    if batch.contexts[0].branch != branch or batch.contexts[0].ref != ref:
        raise artifacts.unavailable()
    return batch


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    if type(branch) is str and branch.startswith('replay_'):
        batch, _ = _replay_context(db, identity, receipt_id=params['_receipt_id'],
            read_snapshot=read_snapshot, locked_user=locked_user)
        if batch.contexts[0].branch != branch:
            raise artifacts.unavailable()
        return batch
    return command._resolve(db, identity, entry_id=entry_id, params=params, branch=branch,
        read_snapshot=read_snapshot, locked_user=locked_user)


def _document(db, identity, ref, *, entry_id, format_purpose, read_snapshot=None, locked_user=None):
    if entry_id != command.APPLY or format_purpose != PURPOSE or ref.kind != KIND:
        raise artifacts.unavailable()
    batch, digest = _replay_context(db, identity, receipt_id=ref.id,
        read_snapshot=read_snapshot, locked_user=locked_user)
    accesses = service.context_accesses(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    require_complete(accesses)
    context = batch.contexts[0]
    refs = tuple(dict.fromkeys(child for values in context.slots.values() for child in values))
    return foundation.DocumentAccess(ref=ref, authorization_revision=context.authorization_revision,
        content_revision=digest, accesses=tuple(accesses.accesses.values()), content_manifest=refs)


_RECEIPT_PROJECTION = {'schema_version': 1, 'payload': 'complete_master_apply_receipt'}

def _inspect(content):
    value = json.loads(content)
    if (type(value) is not dict or value.get('replayed') is not False
            or type(value.get('applied_by')) is not str or type(value.get('import_batch_id')) is not str
            or type(value.get('sheets')) is not list):
        raise artifacts.unavailable()
    return _RECEIPT_PROJECTION


artifacts.register_artifact_domain(KIND, artifacts.ArtifactDomainAdapter(_state, _document,
    (artifacts.ArtifactFormat('master_apply_receipt_v1', PURPOSE, command.APPLY,
        _RECEIPT_PROJECTION, _inspect),), private=True))


class MasterWriteAuthorization:
    """Internal transaction callbacks. Evidence capture alone never grants a write."""
    def __init__(self, identity, plan, principal, *, precondition=None, file_sha256=None):
        self.identity, self.plan, self.principal = identity, plan, principal
        self.precondition = precondition
        self.file_sha256 = file_sha256 if file_sha256 is not None else plan.file_sha256
        self.branches = command.selected_branches(command.APPLY, plan)
        self.parent_ids = sorted({plan.project_id, *(change.previous_project_id
            for change in getattr(plan, 'assignment_changes', ()) if change.previous_project_id)})
        self.captured = {}
        self.actor = None
        self.accesses = None

    @property
    def owner_user_id(self):
        return self.principal.id

    @property
    def operation_key(self):
        return operation_key(self.plan, self.owner_user_id, file_sha256=self.file_sha256)

    def lock_closure(self, db):
        # Called after global/XSDD/workbook-state locks, before the old routine
        # takes its first project/detail lock. Final actor lock comes much later.
        projects = list(db.scalars(select(MaintenanceProject).where(
            MaintenanceProject.project_id.in_(self.parent_ids)).order_by(MaintenanceProject.project_id)
            .with_for_update().execution_options(populate_existing=True)))
        if {row.project_id for row in projects} != set(self.parent_ids):
            raise artifacts.unavailable()
        ids = sorted(change.source_order_id for change in getattr(self.plan, 'assignment_changes', ()))
        if ids:
            list(db.scalars(select(FMaintenanceOrder).where(FMaintenanceOrder.raw_order_id.in_(ids))
                .order_by(FMaintenanceOrder.raw_order_id).with_for_update()
                .execution_options(populate_existing=True)))
            list(db.scalars(select(MaintenanceSourceOrderAssignment).where(
                MaintenanceSourceOrderAssignment.source_order_id.in_(ids),
                MaintenanceSourceOrderAssignment.is_active.is_(True))
                .order_by(MaintenanceSourceOrderAssignment.source_order_id).with_for_update()
                .execution_options(populate_existing=True)))

    def capture(self, db):
        params = {'project_id': self.plan.project_id, 'file_sha256': self.file_sha256,
                  '_parsed_plan': self.plan}
        self.captured = {branch: command._resolve(db, self.identity, entry_id=command.APPLY,
            params=params, branch=branch, _principal=self.principal) for branch in self.branches}

    def check_source_command(self, *, project_id, items):
        expected = sorted((c.source_order_id, c.expected_assignment_id, c.expected_version)
                          for c in getattr(self.plan, 'assignment_changes', ()))
        supplied = sorted((item['source_order_id'], item.get('expected_assignment_id'), item.get('expected_version'))
                          for item in items)
        if not self.captured or project_id != self.plan.project_id or supplied != expected:
            raise ValueError('来源单命令必须属于锁内完整工作簿闭包')

    def authorize_after(self, db):
        if not self.captured:
            raise ValueError('整本写入缺少锁内变更前的完整领域闭包')
        db.flush()
        self.actor = service.lock_actor(db, self.identity)
        principal, evidence = foundation.resolve_project_subjects(db, self.identity, self.parent_ids,
            locked_user=self.actor)
        if principal.id != self.principal.id or principal.authorization_epoch != self.principal.authorization_epoch:
            raise artifacts.unavailable()
        for change in getattr(self.plan, 'assignment_changes', ()):
            current = db.scalar(select(MaintenanceSourceOrderAssignment).where(
                MaintenanceSourceOrderAssignment.source_order_id == change.source_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True)))
            if current is None or current.project_id != self.plan.project_id:
                raise artifacts.unavailable()
        from app.authz import runtime
        states = runtime.runtime_states(db)
        resources = {resource for batch in self.captured.values() for context in batch.contexts
                     for resource in context.requirements}
        if any(states[resource].runtime_state == 'suspended' for resource in resources):
            raise authz_error(503, 'AUTHZ_RESOURCE_SUSPENDED', '完整工作簿授权模块已暂停')
        candidates = []
        for batch in self.captured.values():
            if any(states[resource].runtime_state == 'legacy' for context in batch.contexts
                   for resource in context.requirements):
                continue
            # Preserve the BEFORE closure and its optimistic-concurrency revision,
            # but evaluate current relationships and grants over every original ID.
            contexts = tuple(replace(context, policy_subjects={resource:
                tuple(evidence[subject.id].subject for subject in subjects)
                for resource, subjects in context.policy_subjects.items()}) for context in batch.contexts)
            candidates.append(service.context_accesses(db, self.identity,
                create_context_batch(db, contexts), locked_user=self.actor))
        if not candidates:
            raise authz_error(503, 'AUTHZ_RESOURCE_UNAVAILABLE', '完整工作簿授权模块未启用')
        for accesses in candidates:
            if all(accesses.get(context.ref, resource, action) is not None for context in accesses.batch.contexts
                   for resource, actions in context.requirements.items() for action in actions):
                self.accesses = accesses
                break
        if self.accesses is None:
            require_complete(candidates[0])
        service.check_context_precondition(self.precondition, self.accesses, self.accesses.batch.contexts[0].ref)

    def metadata(self, result):
        if self.accesses is None:
            raise ValueError('未完成最终授权不能登记回执')
        return {'schema_version': 1, 'branch': self.accesses.batch.contexts[0].branch,
            'project_id': self.plan.project_id, 'parent_ids': self.parent_ids,
            'sources': [{'source_order_id': change.source_order_id, 'old_project_id': change.previous_project_id,
                         'new_project_id': self.plan.project_id} for change in getattr(self.plan, 'assignment_changes', ())],
            'file_sha256': self.file_sha256, 'command_plan_sha256': command.plan_digest(self.plan),
            'result_sha256': relation_revision(result)}

    def publish(self, db, receipt):
        if self.accesses is None or self.actor is None or receipt.owner_user_id != self.owner_user_id:
            raise ValueError('发布必须跟随同事务最终授权')
        db.flush()
        target = artifacts.ArtifactTarget(KIND, str(receipt.id), 1)
        row, audit, _, digest = _receipt(db, target.id)
        batch, _ = _replay_context(db, self.identity, receipt_id=target.id, locked_user=self.actor)
        current = service.context_accesses(db, self.identity, batch, locked_user=self.actor)
        require_complete(current)
        binding = artifacts.register_artifact_binding(db, self.identity, target=target,
            input_digest=digest, operation=artifacts.IdempotentOperation('master_apply_receipt', row.operation_key),
            provenance=captured_provenance(current), locked_user=self.actor)
        artifacts.publish_artifact(db, self.identity, binding=binding, purpose=PURPOSE,
            content=canonical_json(audit.after_json['response']).encode(), expected_generation=1, locked_user=self.actor)

    def publish_v1(self, db, result):
        receipt = MaintenanceProjectWorkbookOperation(project_id=self.plan.project_id,
            export_id=None, file_sha256=self.file_sha256, operation_key=self.operation_key,
            payload_hash=self.file_sha256, operation_type='file_apply',
            operated_by=self.principal.username, owner_user_id=self.owner_user_id)
        db.add(receipt)
        db.flush()
        db.add(MaintenanceProjectOperationAudit(project_id=self.plan.project_id,
            entity_type='master_workbook_operation', entity_id=str(receipt.id), action='apply_completed',
            before_json=None, after_json={'schema': 'master_v1_apply_receipt_v1',
                'operation_key': self.operation_key, 'response': result,
                'authorization_contract': self.metadata(result)},
            reason='项目总表 V1 整本应用', operated_by=self.principal.username, operated_at=receipt.created_at))
        self.publish(db, receipt)

    def replay(self, db, receipt):
        self.actor = service.lock_actor(db, self.identity)
        batch, _ = _replay_context(db, self.identity, receipt_id=str(receipt.id), locked_user=self.actor)
        accesses = service.context_accesses(db, self.identity, batch, locked_user=self.actor)
        require_complete(accesses)
        # Legacy rows or forged replay claims cannot gain provenance here.
        manifest = artifacts.require_locked_artifact(db, self.identity,
            target=artifacts.ArtifactTarget(KIND, str(receipt.id), 1), purpose=PURPOSE, locked_user=self.actor,
            authorization_precondition=self.precondition)
        _, audit, _, _ = _receipt(db, str(receipt.id))
        if hashlib.sha256(canonical_json(audit.after_json['response']).encode()).hexdigest() != manifest['content_sha256']:
            raise artifacts.unavailable()
        return accesses


def find_replay(session_factory, identity, *, project_id, content, authorization_view=False,
                authorization_precondition=None):
    digest = hashlib.sha256(content).hexdigest()
    with read_snapshot(session_factory) as (db, proof):
        principal, _ = foundation.resolve_project_subjects(db, identity, [], read_snapshot=proof)
        receipts = list(db.scalars(select(MaintenanceProjectWorkbookOperation).where(
            MaintenanceProjectWorkbookOperation.project_id == project_id,
            MaintenanceProjectWorkbookOperation.file_sha256 == digest,
            MaintenanceProjectWorkbookOperation.owner_user_id == principal.id,
            MaintenanceProjectWorkbookOperation.operation_type == 'file_apply')))
        if not receipts:
            return None
        if len(receipts) != 1:
            raise artifacts.unavailable()
        row = receipts[0]
        target = artifacts.ArtifactTarget(KIND, str(row.id), 1)
        if authorization_view:
            batch, _ = _replay_context(db, identity, receipt_id=str(row.id), read_snapshot=proof)
            accesses = service.context_accesses(db, identity, batch, read_snapshot=proof)
            return service.context_resolution(accesses, batch.contexts[0].ref)
        manifest = artifacts.require_artifact(db, identity, target=target, purpose=PURPOSE,
            read_snapshot=proof, authorization_precondition=authorization_precondition)
        _, audit, _, _ = _receipt(db, target.id)
        if hashlib.sha256(canonical_json(audit.after_json['response']).encode()).hexdigest() != manifest['content_sha256']:
            raise artifacts.unavailable()
        return {**audit.after_json['response'], 'replayed': True}


def apply_upload(db, identity, *, project_id, content, force_takeover=False, merge_token=None,
                 authorization_view=False, authorization_precondition=None):
    factory = sessionmaker(bind=db.get_bind())
    replay = find_replay(factory, identity, project_id=project_id, content=content,
        authorization_view=authorization_view, authorization_precondition=authorization_precondition)
    if replay is not None:
        return replay
    if authorization_view:
        return command.validate_upload(factory, identity, project_id=project_id, content=content,
            force_takeover=force_takeover, authorization_view=True, entry_id=command.APPLY)
    # Protocol marker precedes every domain/actor lock, including on first use.
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        plan, principal, _ = command.validate_upload(factory, identity, project_id=project_id, content=content,
            force_takeover=force_takeover, authorization_precondition=authorization_precondition, entry_id=command.APPLY)
    except master.WorkbookError:
        replay = find_replay(factory, identity, project_id=project_id, content=content,
            authorization_precondition=authorization_precondition)
        if replay is not None:
            return replay
        raise
    replay = find_replay(factory, identity, project_id=project_id, content=content,
        authorization_precondition=authorization_precondition)
    if replay is not None:
        return replay
    if type(plan) is master.MasterV2Plan:
        master.verify_site_issue_merge_token(plan, f'authz-user:{principal.id}', merge_token)
    authorization = MasterWriteAuthorization(identity, plan, principal, precondition=authorization_precondition,
        file_sha256=hashlib.sha256(content).hexdigest())
    from uuid import uuid4
    try:
        if type(plan) is master.MasterPlan:
            return master.apply(db, plan, operated_by=principal.username,
                import_batch_id=str(uuid4()), _authorization=authorization)
        return master.apply_project_master_v2(db, plan, operated_by=principal.username,
            import_batch_id=str(uuid4()), user_ctx=None, _authorization=authorization)
    except BaseException:
        db.rollback()
        raise
