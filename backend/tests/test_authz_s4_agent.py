"""Real main HTTP, PostgreSQL and Cerbos. Only the external model is synthetic."""
import json
from types import MappingProxyType

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, event
from sqlalchemy.orm import Session

from app.auth import _make_token
from app.authz import runtime, service
from app.authz.principal import VerifiedIdentity
from app.authz.provenance import AgentAccess, execution_access
from app.config import get_settings
from app.main import app
from app.models.authorization import SysAuthorizationGrant, SysAuthorizationArtifact
from app.models.chat import ChatMessage, ChatSession
from app.models.system import SysUser
from app.services import agent_authorization as authz, agent_files
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def agent(db, monkeypatch, tmp_path, cerbos_url):
    settings = get_settings()
    for key, value in {'authorization_mode': 'unified_modules',
        'authz_enabled_resources': ['agent_chat', 'private_chat_session', 'agent_file', 'inventory'],
        'authz_suspended_resources': [], 'authz_sales_analysis_enabled': False,
        'authz_sales_analysis_suspended': False, 'enable_agent': True,
        'cerbos_url': cerbos_url, 'cerbos_timeout_seconds': 2.0, 'raw_file_dir': str(tmp_path)}.items():
        monkeypatch.setattr(settings, key, value)
    runtime.activate_configured_resources(db.get_bind())
    user = SysUser(username='s4-agent-owner', password_hash='synthetic-only', role='readonly',
                   permissions={'page_chat': False})
    db.add(user);db.flush()
    for resource, actions in {'agent_chat': ['use'], 'private_chat_session':
        ['read', 'create', 'rename', 'delete', 'run', 'attach', 'cancel'],
        'agent_file': ['upload', 'generate', 'preview', 'download'], 'inventory': ['read']}.items():
        db.add(SysAuthorizationGrant(user_id=user.id, resource=resource, actions=actions,
            scope='all' if resource == 'inventory' else 'self', field_permissions=[], version=1))
    db.commit()
    token, _ = _make_token(role=user.role, sub=user.username, name=None, authn='sys_user', token_version=user.token_version)
    identity = VerifiedIdentity(user.username, user.token_version, False, 'sys_user', MappingProxyType({}), user.id)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + token
        yield client, user, identity


def test_all_session_crud_and_no_active_wire(db, agent):
    client, user, _ = agent
    view = client.post('/api/agent/sessions', params={'view': 'authorization'}, json={'title': 'mine'})
    assert view.status_code == 200, view.text
    assert set(view.json()) == {'locator', 'authorization_context'}
    assert db.scalar(select(ChatSession.id)) is None
    response = client.post('/api/agent/sessions', json={'title': 'mine'})
    assert response.status_code == 200, response.text
    sid = response.json()['id']
    listing = client.get('/api/agent/sessions')
    assert listing.status_code == 200, listing.text
    assert [row['id'] for row in listing.json()['items']] == [sid]
    assert client.patch(f'/api/agent/sessions/{sid}', json={'title': 'renamed'}).status_code == 200
    assert client.get(f'/api/agent/sessions/{sid}/messages').json()['items'] == []
    response = client.post(f'/api/agent/sessions/{sid}/chat/attach')
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert [frame['type'] for frame in frames] == ['ready', 'no_active'], response.text
    assert frames[0]['run_id'] is None and frames[1]['seq'] == 1
    assert client.post(f'/api/agent/sessions/{sid}/chat/cancel').status_code == 200
    assert client.delete(f'/api/agent/sessions/{sid}').status_code == 200


def test_upload_preview_download_and_tampered_actual_bytes(db, agent):
    client, _, _ = agent
    result = client.post('/api/agent/upload', files={'file': ('safe.txt', b'original contents', 'text/plain')})
    assert result.status_code == 200, result.text
    assert set(result.json()) == {'file_id', 'ext'}
    fid = result.json()['file_id']
    assert client.get(f'/api/agent/files/{fid}/preview').status_code == 200
    assert client.get(f'/api/agent/files/{fid}').content == b'original contents'
    assert db.get(SysAuthorizationArtifact, ('agent_file', fid)) is not None
    agent_files._data_path(fid, 'txt').write_bytes(b'changed bytes')
    denied = client.get(f'/api/agent/files/{fid}')
    assert denied.status_code == 403, denied.text


def test_generation_inherits_domain_sources_without_execution_permissions(db, agent):
    _, _, identity = agent
    access = AgentAccess(db.get_bind(), identity)
    access.bind_execution(authz.ExecutionContract(authz.CHAT, {'messages': [{'role': 'user', 'content': 'hello'}]}))
    with execution_access(access):
        service.resource_access(db, identity, 'inventory', 'read')
        db.rollback()
        result = agent_files.write_report('private', ['qty'], [[7]], None, identity.username)
    assert set(result) == {'file_id', 'ext'}
    row = db.get(SysAuthorizationArtifact, ('agent_file', result['file_id']))
    assert [(r['resource_id'], r['actions']) for r in row.authorization_provenance['resources']] == [('inventory', ['read'])]
    other = AgentAccess(db.get_bind(), identity)
    other.bind_execution(authz.ExecutionContract(authz.PREVIEW, {'file_id': result['file_id']}))
    with execution_access(other):
        assert '7' in agent_files.read_document(result['file_id'])['content']
    with Session(db.get_bind()) as writer:
        target = writer.get(SysUser, identity.expected_user_id)
        target.token_version += 1;writer.commit()
    with pytest.raises(HTTPException):
        other.check()


def test_unknown_history_is_denied_before_message_facts(db, agent):
    client, user, _ = agent
    session = ChatSession(owner_sub=user.username, owner_user_id=user.id, title='user title')
    db.add(session);db.flush()
    db.add(ChatMessage(session_id=session.id, role='assistant', content='private-old', tools=[{'secret': 1}]))
    db.commit()
    statements = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement.lower().split(' from ')[0])
    event.listen(db.get_bind(), 'before_cursor_execute', observe)
    try:
        response = client.get(f'/api/agent/sessions/{session.id}/messages')
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', observe)
    assert response.status_code == 403, response.text
    assert not any('chat_message.content' in sql or 'chat_message.tools' in sql for sql in statements)


def test_stateless_and_session_stream_use_actual_ready_context(db, agent, monkeypatch):
    from app.agent import provider
    client, _, _ = agent
    monkeypatch.setattr(provider, 'is_configured', lambda: False)
    for path, body in [('/api/agent/chat/stream', {'messages': [{'role': 'user', 'content': 'hello'}]}),
                       (f"/api/agent/sessions/{client.post('/api/agent/sessions', json={}).json()['id']}/chat/stream", {'message': 'hello'})]:
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert frames[0]['type'] == 'ready'
        assert frames[0]['authorization_context']['schema_version'] == 1
        assert [frame['seq'] for frame in frames[1:]] == list(range(1, len(frames)))
        assert all(frame['run_id'] == frames[0]['run_id'] for frame in frames)
        assert frames[-1]['type'] == 'done' and frames[-1]['configured'] is False
        assert frames[-1]['stopped'] is False


def test_attach_does_not_recheck_producer_run_permission(db, agent):
    from app.api import chat_sessions
    client, user, identity = agent
    sid = client.post('/api/agent/sessions', json={}).json()['id']
    producer = AgentAccess(db.get_bind(), identity)
    producer.bind_execution(authz.ExecutionContract(authz.RUN, {'session_id': sid, 'message': 'hello'}))
    hub = chat_sessions.acquire_session(sid)
    hub.access = producer
    hub.publish({'type': 'delta', 'text': 'already authorized'})
    hub.publish({'type': 'done', 'tool_calls': []})
    hub.finish()
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == user.id,
        SysAuthorizationGrant.resource == 'private_chat_session'))
    grant.actions = ['attach']; grant.version += 1; db.commit()
    try:
        response = client.post(f'/api/agent/sessions/{sid}/chat/attach')
        assert response.status_code == 200, response.text
        assert 'already authorized' in response.text
        assert client.post(f'/api/agent/sessions/{sid}/chat/stream', json={'message': 'no'}).status_code == 403
    finally:
        chat_sessions.release_session(sid)


def test_foreign_file_and_session_do_not_inherit_administrator_access(db, agent):
    client, user, identity = agent
    other = SysUser(username='other-owner', role='admin', password_hash='synthetic')
    db.add(other); db.flush()
    foreign = ChatSession(owner_sub=other.username, owner_user_id=other.id, title='private')
    db.add(foreign); db.commit()
    assert client.get(f'/api/agent/sessions/{foreign.id}/messages').status_code == 403
    assert client.delete(f'/api/agent/sessions/{foreign.id}').status_code == 403
    assert db.get(ChatSession, foreign.id) is not None


def test_multipart_precondition_binds_actual_bytes(db, agent):
    client, _, _ = agent
    view = client.post('/api/agent/upload', params={'view': 'authorization'},
                       files={'file': ('input.txt', b'old', 'text/plain')})
    assert view.status_code == 200, view.text
    context = view.json()['authorization_context']
    proof = {k: context[k] for k in ('catalog_version', 'authorization_epoch', 'context')}
    response = client.post('/api/agent/upload', files={'file': ('input.txt', b'new', 'text/plain')},
                           data={'authorization_precondition': json.dumps(proof)})
    assert response.status_code == 409, response.text
    assert db.scalar(select(SysAuthorizationArtifact.domain_id).where(SysAuthorizationArtifact.domain_kind == 'agent_file')) is None


def test_execution_contract_is_deeply_immutable_and_keeps_actual_input(db, agent):
    _, _, identity = agent
    params = {'messages': [{'role': 'user', 'content': 'original'}]}
    contract = authz.ExecutionContract(authz.CHAT, params)
    before, ref = contract.authorize(db.get_bind(), identity)
    original_revision = before.wire(ref)['context']['authorization_revision']
    params['messages'][0]['content'] = 'caller changed input'
    with pytest.raises(TypeError):
        contract.params['messages'][0]['content'] = 'mutated contract'
    with pytest.raises(TypeError):
        contract.params['messages'][0] = {'role': 'assistant', 'content': 'forged history'}
    with pytest.raises(TypeError):
        contract.params['messages'] = []
    after, current_ref = contract.authorize(db.get_bind(), identity)
    assert contract.params['messages'][0]['content'] == 'original'
    assert current_ref == ref
    assert after.wire(current_ref)['context']['authorization_revision'] == original_revision


def test_data_responses_carry_the_same_actual_context_as_preflight(db, agent, monkeypatch):
    import os
    from pathlib import Path
    from app.agent import provider
    client, _, _ = agent
    monkeypatch.setattr(provider, 'is_configured', lambda: False)
    sid = client.post('/api/agent/sessions', json={'title': 'synthetic session'}).json()['id']
    fid = client.post('/api/agent/upload', files={'file': ('synthetic.txt', b'synthetic', 'text/plain')}).json()['file_id']
    samples = []
    for entry, path in [(authz.LIST, '/api/agent/sessions'),
                        (authz.PREVIEW, f'/api/agent/files/{fid}/preview'),
                        (authz.HISTORY, f'/api/agent/sessions/{sid}/messages')]:
        view = client.get(path, params={'view': 'authorization'})
        assert view.status_code == 200, view.text
        prior = view.json()['authorization_context']
        response = client.get(path, params={'authorization_precondition': json.dumps(
            {key: prior[key] for key in ('catalog_version', 'authorization_epoch', 'context')})})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['authorization_context'] == prior
        samples.append({'entry_id': entry, 'resolution': view.json(), 'payload': result})
    stream = client.post(f'/api/agent/sessions/{sid}/chat/stream', json={'message': 'synthetic'})
    assert stream.status_code == 200, stream.text
    output = os.environ.get('S4_AGENT_WIRE_EXPORT')
    if output:
        Path(output).write_text(json.dumps({'snapshot': client.get('/api/authz/me').json(),
            'catalog': service.catalog_snapshot(db), 'samples': samples,
            'sse_entry': authz.RUN, 'sse': stream.text}, ensure_ascii=False))
