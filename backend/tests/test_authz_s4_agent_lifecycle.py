"""Real PostgreSQL/PDP/worker boundaries; only the external model is synthetic."""
import json
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import provider
from app.api import chat_sessions
from app.authz.provenance import AgentAccess, current_access
from app.models.chat import ChatMessage
from app.models.system import SysUser
from app.services import agent_authorization as authz, chat_store
from tests.test_authz_s4_agent import agent  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


def frames(response):
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


def test_actual_worker_records_tool_sources_and_releases_actor_before_llm(db, agent, monkeypatch):
    client, user, identity = agent
    sid = client.post('/api/agent/sessions', json={'title': 'existing'}).json()['id']
    observed = []
    def model(messages, tools=None):
        access = current_access()
        assert access is not None and access.snapshot()["principal_id"] == user.id
        assert access.execution_contracts[0].entry_id == authz.RUN
        with Session(db.get_bind()) as other:
            # A real independent connection proves no actor SHARE survives into LLM I/O.
            other.scalar(select(SysUser).where(SysUser.id == user.id).with_for_update(nowait=True))
        observed.append((threading.current_thread().name, access.snapshot()))
        if len(observed) == 1:
            yield 'result', provider.ChatResult(None, [provider.ToolCall('inv', 'get_inventory', '{}')])
        else:
            assert any(m['role'] == 'tool' for m in messages)
            yield 'delta', 'authorized answer'
            yield 'result', provider.ChatResult('authorized answer')
    monkeypatch.setattr(provider, 'is_configured', lambda: True)
    monkeypatch.setattr(provider, 'chat_stream', model)
    result = frames(client.post(f'/api/agent/sessions/{sid}/chat/stream', json={'message': 'inventory'}))
    assert result[0]['type'] == 'ready'
    assert result[-1]['type'] == 'done', result
    assert len(observed) == 2 and all(name.startswith('agent-session-') for name, _ in observed)
    assert observed[0][1]['resources'] == []
    assert [(r['resource_id'], r['actions']) for r in observed[1][1]['resources']] == [('inventory', ['read'])]
    row = db.scalar(select(ChatMessage).where(ChatMessage.session_id == sid, ChatMessage.role == 'assistant'))
    assert row.content == 'authorized answer'
    assert [(r['resource_id'], r['actions']) for r in row.authorization_provenance['resources']] == [('inventory', ['read'])]
    assert not chat_sessions.is_generating(sid)
    assert current_access() is None


def test_checkpoint_failure_is_terminal_server_error_not_done(db, agent, monkeypatch):
    client, _, _ = agent
    sid = client.post('/api/agent/sessions', json={'title': 'existing'}).json()['id']
    def model(*args, **kwargs):
        yield 'delta', 'partial'
        yield 'result', provider.ChatResult('partial')
    def fail_save(*args, **kwargs):
        raise RuntimeError('synthetic storage fault with private details')
    monkeypatch.setattr(provider, 'is_configured', lambda: True)
    monkeypatch.setattr(provider, 'chat_stream', model)
    monkeypatch.setattr(chat_store, 'save_assistant_progress', fail_save)
    result = frames(client.post(f'/api/agent/sessions/{sid}/chat/stream', json={'message': 'hello'}))
    assert result[-1]['type'] == 'error' and result[-1]['code'] == 'CHAT_PERSISTENCE_FAILED', result
    assert result[-1]['category'] == 'server'
    assert not any(frame['type'] == 'done' for frame in result)
    assert 'private details' not in json.dumps(result)
    assert db.scalar(select(ChatMessage.id).where(ChatMessage.session_id == sid, ChatMessage.role == 'assistant')) is None
    assert not chat_sessions.is_generating(sid)


def test_worker_rechecks_epoch_after_llm_chunk_and_does_not_save_stale_answer(db, agent, monkeypatch):
    client, user, _ = agent
    sid = client.post('/api/agent/sessions', json={'title': 'existing'}).json()['id']
    def model(*args, **kwargs):
        yield 'delta', 'first'
        with Session(db.get_bind()) as writer:
            actor = writer.get(SysUser, user.id)
            actor.token_version += 1
            writer.commit()
        yield 'delta', 'MUST-NOT-PUBLISH'
        yield 'result', provider.ChatResult('MUST-NOT-SAVE')
    monkeypatch.setattr(provider, 'is_configured', lambda: True)
    monkeypatch.setattr(provider, 'chat_stream', model)
    result = frames(client.post(f'/api/agent/sessions/{sid}/chat/stream', json={'message': 'hello'}))
    assert result[-1]['type'] == 'error' and result[-1]['category'] == 'authorization', result
    assert 'MUST-NOT' not in json.dumps(result)
    assert not any(frame['type'] == 'done' for frame in result)
    assert db.scalar(select(ChatMessage.id).where(ChatMessage.session_id == sid, ChatMessage.role == 'assistant')) is None


def test_capped_attach_emits_zero_content_and_explicit_replay_gap(db, agent, monkeypatch):
    client, _, identity = agent
    sid = client.post('/api/agent/sessions', json={}).json()['id']
    producer = AgentAccess(db.get_bind(), identity)
    producer.bind_execution(authz.ExecutionContract(authz.RUN, {'session_id': sid, 'message': 'hello'}))
    hub = chat_sessions.acquire_session(sid)
    hub.access = producer
    monkeypatch.setattr(chat_sessions, '_BUFFER_EVENT_CAP', 1)
    try:
        hub.publish({'type': 'delta', 'text': 'private partial'})
        hub.publish({'type': 'done', 'tool_calls': []})
        hub.finish()
        result = frames(client.post(f'/api/agent/sessions/{sid}/chat/attach'))
        assert [frame['type'] for frame in result] == ['ready', 'error']
        assert result[0]['replay_complete'] is False
        assert result[1]['seq'] == 1 and result[1]['code'] == 'CHAT_REPLAY_GAP'
        assert 'private partial' not in json.dumps(result)
    finally:
        chat_sessions.release_session(sid)


@pytest.mark.parametrize('endpoint', ['stateless', 'no_active'])
def test_revoke_after_response_preflight_still_has_ready_then_typed_error(db, agent, monkeypatch, endpoint):
    from app.api import agent as api_agent
    client, user, _ = agent
    module = api_agent if endpoint == 'stateless' else chat_sessions
    sid = client.post('/api/agent/sessions', json={}).json()['id']
    original = module.StreamingResponse
    def revoke(*args, **kwargs):
        with Session(db.get_bind()) as writer:
            actor = writer.get(SysUser, user.id)
            actor.token_version += 1
            writer.commit()
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'StreamingResponse', revoke)
    response = (client.post('/api/agent/chat/stream', json={'messages': [{'role': 'user', 'content': 'hello'}]})
                if endpoint == 'stateless' else client.post(f'/api/agent/sessions/{sid}/chat/attach'))
    result = frames(response)
    assert [frame['type'] for frame in result] == ['ready', 'error']
    assert result[1]['seq'] == 1 and result[1]['category'] == 'authorization'
    if endpoint == 'no_active':
        assert result[0]['run_id'] is None and result[1]['run_id'] is None


def test_actual_generated_file_locator_roundtrips_through_worker_and_history(db, agent, monkeypatch):
    """Real file/tool/worker; the external model follows the published safe-locator contract."""
    import os
    from pathlib import Path
    client, _, _ = agent
    sid = client.post('/api/agent/sessions', json={'title': 'file locator'}).json()['id']
    observed = {}
    def model(messages, tools=None):
        assert '[结果文件](/api/agent/files/<file_id>)' in messages[0]['content']
        descriptions = {tool['function']['name']: tool['function']['description'] for tool in tools}
        assert '/api/agent/files/<file_id>' in descriptions['write_excel']
        assert '/api/agent/files/<file_id>' in descriptions['write_report']
        tool_messages = [message for message in messages if message['role'] == 'tool']
        if not tool_messages:
            yield 'result', provider.ChatResult(None, [provider.ToolCall('file', 'write_report',
                json.dumps({'headers': ['标识', '数量'], 'rows': [['S4-QA-ONLY', 2]]}))])
        else:
            value = json.loads(tool_messages[-1]['content'])
            assert set(value) == {'file_id', 'ext'}, value
            observed.update(value)
            text = f"[结果文件](/api/agent/files/{value['file_id']})"
            yield 'delta', text
            yield 'result', provider.ChatResult(text)
    monkeypatch.setattr(provider, 'is_configured', lambda: True)
    monkeypatch.setattr(provider, 'chat_stream', model)
    response = client.post(f'/api/agent/sessions/{sid}/chat/stream', json={'message': 'generate synthetic report'})
    result = frames(response)
    assert result[-1]['type'] == 'done', result
    assert any(item['type'] == 'tool_done' and item['name'] == 'write_report' and item['ok'] for item in result)
    fid = observed['file_id']
    link = f'[结果文件](/api/agent/files/{fid})'
    history = client.get(f'/api/agent/sessions/{sid}/messages').json()
    assert history['items'][-1]['content'] == link
    assert client.get(f'/api/agent/files/{fid}/preview').status_code == 200
    assert client.get(f'/api/agent/files/{fid}').content.startswith(b'PK')
    if destination := os.getenv('S4_AGENT_REPORT_WIRE_EXPORT'):
        Path(destination).write_text(json.dumps({'file_id': fid, 'content': link, 'sse': response.text}, ensure_ascii=False, indent=2) + '\n')
