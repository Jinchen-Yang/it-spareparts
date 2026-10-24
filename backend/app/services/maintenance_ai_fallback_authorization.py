"""AI suggestions authorize the actual input, then the stored typed proposal.

The provider call runs without a database transaction. Acceptance serializes
the real proposal and the destination preview before its final actor lock.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import Text, func, literal, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors

from app.authz import service
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.engine import QueryBindings
from app.authz.errors import authz_error
from app.authz.principal import load_principal
from app.models.maintenance_ai_fallback import MaintenanceAiMappingProposal as Proposal
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_ai_fallback as business
from app.services.maintenance_workflow_authorization import require_complete

BASE = '/api/maintenance/ai-fallback'
LIST = 'GET ' + BASE + '/proposals'
PROPOSE = 'POST ' + BASE + '/propose'
ACCEPT = 'POST ' + BASE + '/{proposal_id}/accept'
RESOURCES = ('maintenance_ai_document', 'maintenance_ai_ledger', 'maintenance_ai_proposals')


def _branch(doc_type):
    if doc_type not in business.DOC_TYPE_KIND:
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知单据类型')
    return 'variant_1' if doc_type == 'ledger' else 'variant_0'


def _row(db, proposal_id, *, lock=False):
    query = select(Proposal).where(Proposal.proposal_id == proposal_id).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '提案不存在')
    if row.owner_user_id is None:
        raise artifacts.unavailable()
    return row


def _digest_expression():
    # Status is mutable workflow state. The source proposal itself never changes.
    columns = {'proposal_id': Proposal.proposal_id, 'owner_user_id': Proposal.owner_user_id,
        'doc_type': Proposal.doc_type, 'file_hash': Proposal.file_hash, 'filename': Proposal.filename,
        'headers': Proposal.header_snapshot, 'samples': Proposal.sample_rows, 'proposal': Proposal.proposal,
        'provider': Proposal.provider, 'model': Proposal.model, 'prompt_hash': Proposal.prompt_hash}
    body = func.jsonb_build_object(*[item for key, value in columns.items() for item in (literal(key), value)])
    return func.encode(func.sha256(func.convert_to(body.cast(Text), 'UTF8')), 'hex')


def _metadata(db, proposal_id):
    # Full headers, samples and mapping stay inside PostgreSQL until the actual
    # accept contract is allowed. The complete immutable proposal still binds
    # the artifact through an opaque SQL digest, including unknown JSON keys.
    row = db.execute(select(Proposal.proposal_id, Proposal.owner_user_id, Proposal.doc_type,
        Proposal.file_hash, Proposal.status, Proposal.accepted_batch_id,
        Proposal.accepted_by, Proposal.accepted_at, _digest_expression().label('content_digest'))
        .where(Proposal.proposal_id == proposal_id)).first()
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '提案不存在')
    if row.owner_user_id is None:
        raise artifacts.unavailable()
    return row


def _digest(db, row):
    if hasattr(row, 'content_digest'):
        return row.content_digest
    return db.scalar(select(_digest_expression()).where(Proposal.proposal_id == row.proposal_id))


def _state(db, target):
    row = _metadata(db, target.id)
    if target.generation != 1:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(target, ObjectRef('ai_proposal', row.proposal_id),
        _digest(db, row), row.owner_user_id)


artifacts.register_artifact_domain('ai_proposal', artifacts.ArtifactDomainAdapter(_state, None, (), private=False))


def _verify(db, row):
    return artifacts.load_artifact_binding(db,
        target=artifacts.ArtifactTarget('ai_proposal', row.proposal_id, 1))


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    principal, _ = foundation.resolve_project_subjects(db, identity, [],
        read_snapshot=read_snapshot, locked_user=locked_user)
    if entry_id == ACCEPT:
        row = _metadata(db, params['proposal_id'])
        _verify(db, row)
        selected = _branch(row.doc_type)
        carrier = foundation.slot_ref(entry_id, 'context_0', row.proposal_id)
        revision = {'content': _digest(db, row), 'status': row.status,
                    'accepted_batch_id': row.accepted_batch_id}
    elif entry_id == PROPOSE:
        selected = _branch(params['doc_type'])
        selection = {'doc_type': params['doc_type'], 'input_sha256': params['input_sha256']}
        input_id = relation_revision({'actor_id': principal.id,
            'epoch': principal.authorization_epoch, 'entry_id': entry_id, 'selection': selection})
        carrier = foundation.slot_ref(entry_id, 'context_0', input_id, selection=selection)
        revision = carrier.to_dict()
    elif entry_id == LIST:
        selected = 'default'
        carrier = foundation.slot_ref(entry_id, 'context_0', 'ai_proposals_list',
            selection={'doc_type': params.get('doc_type')})
        # Owner predicates and rows are executed in the same read snapshot.
        revision = {'actor_id': principal.id, 'carrier': carrier.to_dict()}
    else:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '未登记的 AI 提案入口')
    if branch is not None and branch != selected:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '提案实际单据类型与授权分支不符')
    requirements = ENTRY_CONTRACTS[entry_id]['branches'][selected]['requirements']
    subjects = {resource: (() if entry_id == LIST else (PolicySubject(carrier.id, {}),))
                for resource in requirements}
    return create_context_batch(db, [foundation.resolved_context(db, entry_id=entry_id,
        branch=selected, carrier=carrier, slots={'context_0': (carrier,)}, subjects=subjects,
        revision=relation_revision(revision))])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    from app.authz.context import plain
    params = {'proposal_id': ref.id} if entry_id == ACCEPT else plain(ref.selector).get('selection', {})
    return _resolve(db, identity, entry_id=entry_id, params=params, branch=branch,
        read_snapshot=read_snapshot, locked_user=locked_user)


for entry in (LIST, PROPOSE, ACCEPT):
    foundation.register_entry_resolver(entry, _resolve,
        reference_resolver=None if entry == PROPOSE else _reference)


def _query(db, principal, context, resource):
    if context.ref.kind != 'collection' or context.ref.id != 'ai_proposals_list':
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '提案查询集合不匹配')
    table = select(Proposal.proposal_id,
        (Proposal.owner_user_id == principal.id).label('is_owner')).subquery('ai_authorization_proposals')
    return QueryBindings(resource, table, {'request.resource.attr.is_owner': table.c.is_owner})


foundation.register_query_bindings('maintenance_ai_proposals', _query)


def _condition(access):
    tables = {column.table for column in visitors.iterate(access.predicate)
        if getattr(getattr(column, 'table', None), 'name', None) == 'ai_authorization_proposals'}
    if len(tables) > 1:
        raise authz_error(503, 'AUTHZ_ENGINE_UNAVAILABLE', '提案查询范围无法确认')
    if not tables:
        return access.predicate
    table = tables.pop()
    return Proposal.proposal_id.in_(select(table.c.proposal_id).where(access.predicate))


def view(db, identity, *, entry_id, params, precondition=None, require=False):
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry_id, params=params) as (_, _, batch, accesses):
        if require:
            require_complete(accesses)
            service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
        return service.context_resolution(accesses, batch.contexts[0].ref)


def list_proposals(db, identity, *, doc_type=None, authorization_view=False, precondition=None):
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=LIST, params={'doc_type': doc_type}) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        access = accesses.require(ref, 'maintenance_ai_proposals', 'read')
        service.check_context_precondition(precondition, accesses, ref)
        rows = business.list_proposals(snapshot, doc_type=doc_type, authorization_condition=_condition(access))
        return {'rows': rows, 'authorization_context': accesses.wire(ref)}


def propose(db, identity, *, data, doc_type, filename, headers, samples, precondition=None, llm_call=None):
    params = {'doc_type': doc_type, 'input_sha256': sha256(data).hexdigest()}
    view(db, identity, entry_id=PROPOSE, params=params, precondition=precondition, require=True)
    # Transport has only authenticated/read so far. Never hold its transaction
    # (or an actor lock) over the external provider call.
    db.rollback()
    row, result = business.prepare_proposal(doc_type=doc_type, data=data, filename=filename,
        headers=headers, samples=samples, operated_by='', llm_call=llm_call)
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        actor = service.lock_actor(db, identity)
        batch = foundation.resolve_entry_context(db, identity, entry_id=PROPOSE, params=params, locked_user=actor)
        accesses = service.context_accesses(db, identity, batch, locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
        row.created_by, row.owner_user_id = actor.username, actor.id
        db.add(row)
        db.flush()
        artifacts.register_artifact_binding(db, identity,
            target=artifacts.ArtifactTarget('ai_proposal', row.proposal_id, 1), input_digest=_digest(db, row), locked_user=actor)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


_SEAL = object()


@dataclass(frozen=True)
class AiAcceptanceAuthorization:
    """Issued internally for one locked real proposal in one transaction."""
    db: Session
    transaction: object
    identity: object
    proposal_id: str
    doc_type: str
    input_digest: str
    precondition: object
    seal: object

    def prepare(self, db, doc_type, input_digest):
        if (self.seal is not _SEAL or db is not self.db
                or db.get_transaction() is not self.transaction or not self.transaction.is_active
                or doc_type != self.doc_type or input_digest != self.input_digest):
            raise authz_error(403, 'AUTHZ_FORBIDDEN', 'AI 采纳授权与当前事务或输入不一致')
        row = _metadata(db, self.proposal_id)
        if row.doc_type != doc_type or row.file_hash != input_digest:
            raise artifacts.unavailable()
        _verify(db, row)
        # This read only chooses the stable idempotency namespace; it grants
        # nothing. The final authority check follows the destination locks.
        principal = load_principal(db, self.identity)
        if principal is None:
            raise authz_error(401, 'AUTHN_SESSION_INVALID', '需要有效实名账号')
        return principal.id

    def authorize(self, db, doc_type, input_digest):
        self.prepare(db, doc_type, input_digest)
        actor = service.lock_actor(db, self.identity)
        batch = foundation.resolve_entry_context(db, self.identity, entry_id=ACCEPT,
            params={'proposal_id': self.proposal_id}, locked_user=actor)
        accesses = service.context_accesses(db, self.identity, batch, locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(self.precondition, accesses, batch.contexts[0].ref)
        return actor


def accept(db, identity, *, proposal_id, data, filename, idempotency_key, precondition=None):
    view(db, identity, entry_id=ACCEPT, params={'proposal_id': proposal_id}, precondition=precondition, require=True)
    db.rollback()
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        row = _row(db, proposal_id, lock=True)
        _verify(db, row)
        if sha256(data).hexdigest() != row.file_hash:
            raise business.AIProposalError('上传文件与提案不匹配')
        permit = AiAcceptanceAuthorization(db, db.get_transaction(), identity, proposal_id,
            row.doc_type, row.file_hash, precondition, _SEAL)
        if row.status == 'accepted':
            permit.authorize(db, row.doc_type, row.file_hash)
            if row.accepted_batch_id is None:
                raise business.AIProposalError('提案状态异常')
            _verify_accepted_batch(db, row)
            result = row.accepted_batch_id
        else:
            if row.status != 'pending':
                raise business.AIProposalError('提案已拒绝，不能采纳')
            parsed = business._parse_with_mapping(row.doc_type, data, filename, row.proposal)
            producer = business.ledger if row.doc_type == 'ledger' else business.ckd if row.doc_type == 'ckd_shipment' else business.docs
            # Two proposals may interpret the very same file differently. The
            # destination's normal file-based key must never collapse them.
            preview_key = relation_revision({'protocol': 'ai_accept_preview_v1',
                'proposal_id': row.proposal_id, 'proposal_digest': _digest(db, row), 'key': idempotency_key})
            result = producer.store_preview(db, parsed, '', idempotency_key=preview_key,
                commit=False, producer_authorization=permit)
            actor = permit.authorize(db, row.doc_type, row.file_hash)
            row.status, row.accepted_batch_id = 'accepted', result
            row.accepted_by, row.accepted_at = actor.username, datetime.now(timezone.utc)
            db.flush()
            target = artifacts.ArtifactTarget('ai_proposal_acceptance', row.proposal_id, 1)
            artifacts.register_artifact_binding(db, identity, target=target,
                input_digest=_receipt_state(db, target).input_digest, locked_user=actor)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def _batch_state(db, row):
    from app.models.maintenance_ckd_import import MaintenanceCkdImportBatch
    from app.models.maintenance_doc_import import MaintenanceDocImportBatch
    from app.models.maintenance_ledger import MaintenanceLedgerImportBatch
    model, kind = ((MaintenanceLedgerImportBatch, 'ledger_import_batch') if row.doc_type == 'ledger'
        else (MaintenanceCkdImportBatch, 'shipment_import_batch') if row.doc_type == 'ckd_shipment'
        else (MaintenanceDocImportBatch, 'document_import_batch'))
    columns = [model.batch_id, model.owner_user_id, model.file_hash]
    if hasattr(model, 'doc_type'):
        columns.append(model.doc_type)
    batch = db.execute(select(*columns).where(model.batch_id == row.accepted_batch_id)).first()
    if batch is None or batch.owner_user_id is None or batch.file_hash != row.file_hash:
        raise artifacts.unavailable()
    if hasattr(batch, 'doc_type') and batch.doc_type != row.doc_type:
        raise artifacts.unavailable()
    binding = artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget(kind, batch.batch_id, 1))
    return batch, binding


def _receipt_state(db, target):
    row = _metadata(db, target.id)
    if target.generation != 1 or row.status != 'accepted' or row.accepted_at is None:
        raise artifacts.unavailable()
    batch, binding = _batch_state(db, row)
    digest = relation_revision({'proposal': _digest(db, row), 'batch_id': batch.batch_id,
        'batch_kind': binding.target.kind, 'batch_digest': binding.input_digest,
        'owner_user_id': batch.owner_user_id, 'accepted_by': row.accepted_by,
        'accepted_at': row.accepted_at.isoformat()})
    return artifacts.ArtifactDomainState(target, ObjectRef('ai_proposal', row.proposal_id), digest, batch.owner_user_id)


artifacts.register_artifact_domain('ai_proposal_acceptance', artifacts.ArtifactDomainAdapter(_receipt_state, None, (), private=False))


def _verify_accepted_batch(db, row):
    artifacts.load_artifact_binding(db,
        target=artifacts.ArtifactTarget('ai_proposal_acceptance', row.proposal_id, 1))
