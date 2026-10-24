"""Actual private Agent targets and execution contracts, separate from content lineage."""
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_identity
from app.authz import service, runtime
from app.authz.context import PolicySubject, _freeze, canonical_json, create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.authz.engine import QueryBindings
from app.db import get_db
from app.models.chat import ChatSession
from app.security import require_page, get_current_user_context

CHAT = 'POST /api/agent/chat'
STREAM = 'POST /api/agent/chat/stream'
LIST = 'GET /api/agent/sessions'
CREATE = 'POST /api/agent/sessions'
RENAME = 'PATCH /api/agent/sessions/{session_id}'
DELETE = 'DELETE /api/agent/sessions/{session_id}'
HISTORY = 'GET /api/agent/sessions/{session_id}/messages'
RUN = 'POST /api/agent/sessions/{session_id}/chat/stream'
ATTACH = 'POST /api/agent/sessions/{session_id}/chat/attach'
CANCEL = 'POST /api/agent/sessions/{session_id}/chat/cancel'
UPLOAD = 'POST /api/agent/upload'
PREVIEW = 'GET /api/agent/files/{file_id}/preview'
DOWNLOAD = 'GET /api/agent/files/{file_id}'
EXCEL = 'TOOL agent.write_excel'
REPORT = 'TOOL agent.write_report'
ENTRIES = (CHAT, STREAM, LIST, CREATE, RENAME, DELETE, HISTORY, RUN, ATTACH, CANCEL,
           UPLOAD, PREVIEW, DOWNLOAD, EXCEL, REPORT)


def foundation():
    # The core extraction owns this exact registry; no second Agent registry.
    from app.authz import domain_contexts
    return domain_contexts


def digest(value):
    return sha256(canonical_json(value).encode()).hexdigest()


def page_gate(db=Depends(get_db), ctx=Depends(get_current_user_context)):
    if not runtime.canonical_management_required(db):
        require_page('page_chat')(ctx)


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    f = foundation()
    actor = service._read_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    if actor is None:
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '需要有效实名账号')
    contract = ENTRY_CONTRACTS[entry_id]
    rule = contract['branches']['default']
    slot = contract['slots']['context_0']
    kind = slot.get('object_type') if slot['kind'] == 'object' else slot['kind']
    owner = actor.id
    selection = {}
    state = {'owner_user_id': owner}
    if kind == 'chat_session':
        sid = params.get('session_id')
        if type(sid) is not int or sid <= 0:
            raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '会话编号无效')
        row = db.execute(select(ChatSession.id, ChatSession.owner_user_id, ChatSession.archived,
            ChatSession.created_at).where(ChatSession.id == sid)).one_or_none()
        if row is None or row.archived:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '会话不存在')
        owner = row.owner_user_id
        state = {'id': row.id, 'owner_user_id': owner, 'archived': row.archived,
                 'created_at': row.created_at.isoformat()}
        object_id = str(row.id)
        if entry_id == RUN:
            selection = {'message_sha256': params.get('message_sha256') or digest(params['message'])}
        elif entry_id == RENAME:
            if not params.get('title_sha256') and not params['title'].strip():
                raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '标题不能为空')
            selection = {'title_sha256': params.get('title_sha256') or digest(params['title'].strip())}
    elif kind == 'agent_file':
        from app.services import agent_file_authorization as files
        meta, state_digest = files.metadata(db, params['file_id'])
        owner = meta['owner_user_id']
        object_id = params['file_id']
        state = {'file_id': object_id, 'input_digest': state_digest, 'owner_user_id': owner}
    elif kind == 'collection':
        object_id = slot['collection_ref']
    elif kind == 'operation_input':
        if entry_id in (CHAT, STREAM) and any(m.get('role') != 'user' for m in params.get('messages', [])):
            raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '仅允许用户输入，历史由服务端读取')
        command_digest = digest({'entry_id': entry_id, 'actor_id': actor.id, 'command': params})
        object_id = command_digest
        selection = {'command_sha256': command_digest}
        if entry_id.startswith('TOOL '):
            selection['tool'] = entry_id.removeprefix('TOOL agent.')
    else:
        raise ValueError('Unregistered Agent target kind')
    ref = f.slot_ref(entry_id, 'context_0', object_id, selection=selection)
    subjects = {resource: (() if kind == 'collection' else
        (PolicySubject(object_id, {'is_owner': owner == actor.id}),)) for resource in rule['requirements']}
    context = f.resolved_context(db, entry_id=entry_id, branch='default', carrier=ref,
        slots={'context_0': (ref,)}, subjects=subjects,
        revision=relation_revision({'state': state, 'selection': selection,
                                    'actor': actor.id, 'epoch': actor.authorization_epoch}))
    return create_context_batch(db, [context])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None,
               actions_by_resource=None):
    # Only actual persistent readers can be content dependencies.
    if entry_id not in (HISTORY, ATTACH, PREVIEW, DOWNLOAD, LIST):
        raise authz_error(403, 'AUTHZ_ARTIFACT_STALE', '执行命令不能作为历史读取来源')
    params = ({'session_id': int(ref.id)} if ref.kind == 'chat_session' else
              {'file_id': ref.id} if ref.kind == 'agent_file' else {})
    return _resolve(db, identity, entry_id=entry_id, params=params, branch=branch,
                    read_snapshot=read_snapshot, locked_user=locked_user)


def _bindings(db, actor, context, resource):
    if context.entry_id != LIST or resource != 'private_chat_session':
        raise ValueError('Unknown private Agent collection')
    table = select(ChatSession.id.label('session_id'),
        (ChatSession.owner_user_id == actor.id).label('is_owner')).subquery('agent_owned_sessions')
    return QueryBindings(resource, table, {'request.resource.attr.is_owner': table.c.is_owner})


_registered = False

def register():
    global _registered
    if _registered:
        return
    f = foundation()
    for entry in ENTRIES:
        f.register_entry_resolver(entry, _resolve, reference_resolver=_reference)
    f.register_query_bindings('private_chat_session', _bindings)
    _registered = True


@dataclass(frozen=True)
class ExecutionContract:
    entry_id: str
    params: Mapping

    def __post_init__(self):
        if self.entry_id not in ENTRIES:
            raise ValueError('Unknown Agent execution entry')
        if not isinstance(self.params, Mapping):
            raise ValueError('Agent execution parameters must be an object')
        object.__setattr__(self, 'params', _freeze(self.params))

    def authorize_in(self, db, identity, *, read_snapshot=None, locked_user=None, precondition=None):
        register()
        f = foundation()
        batch = f.resolve_entry_context(db, identity, entry_id=self.entry_id, params=self.params,
            read_snapshot=read_snapshot, locked_user=locked_user)
        if self.entry_id == LIST:
            actor = service._read_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
            bindings = {(context.ref.key, r): _bindings(db, actor, context, r)
                        for context in batch.contexts for r in context.requirements}
            accesses = service.context_query_accesses(db, identity, batch, bindings,
                read_snapshot=read_snapshot, locked_user=locked_user)
        else:
            accesses = service.context_accesses(db, identity, batch,
                read_snapshot=read_snapshot, locked_user=locked_user)
        for context in batch.contexts:
            for resource, actions in context.requirements.items():
                for action in actions:
                    accesses.require(context.ref, resource, action)
        ref = batch.contexts[0].ref
        service.check_context_precondition(f.decode_precondition(precondition), accesses, ref)
        return accesses, ref

    def authorize(self, bind, identity, *, precondition=None):
        from app.authz.context import read_snapshot
        with read_snapshot(lambda: Session(bind)) as (db, proof):
            return self.authorize_in(db, identity, read_snapshot=proof, precondition=precondition)


def precondition(query, body):
    f = foundation()
    query = f.decode_precondition(query)
    body = f.decode_precondition(body)
    if query is not None and body is not None and query != body:
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '授权前置条件重复且不一致')
    return body if body is not None else query


def prepare(access, entry_id, params, *, view=None, precondition=None):
    if not access.authoritative:
        return None
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知授权视图')
    contract = ExecutionContract(entry_id, params)
    access.bind_execution(contract, precondition=precondition if view is None else None)
    if view == 'authorization':
        accesses, ref = contract.authorize(access.bind, access.identity)
        return service.context_resolution(accesses, ref)
    return None
