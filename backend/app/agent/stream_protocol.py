"""The closed Agent stream wire. Execution code supplies actual authorization context."""
from copy import deepcopy
import json
import re
from uuid import uuid4

from fastapi import HTTPException

from app.authz.context import canonical_json

MAX_EVENT_BYTES = 1024 * 1024
ERROR_CATEGORIES = {
    **dict.fromkeys(('AUTHN_SESSION_INVALID', 'AUTHZ_FORBIDDEN', 'AUTHZ_CONTEXT_STALE',
                     'AUTHZ_ARTIFACT_STALE'), 'authorization'),
    **dict.fromkeys(('AUTHZ_RESOURCE_UNAVAILABLE', 'AUTHZ_ENGINE_UNAVAILABLE',
                     'CHAT_PERSISTENCE_FAILED'), 'server'),
    'CHAT_PROVIDER_UNAVAILABLE': 'provider',
    'CHAT_REPLAY_GAP': 'protocol', 'CHAT_STREAM_PROTOCOL_ERROR': 'protocol',
}
TERMINAL = frozenset(('done', 'error', 'no_active'))
_PAYLOAD_KEYS = {
    'delta': {'text'}, 'thinking': {'text'}, 'tool': {'name', 'args'},
    'tool_done': {'name', 'ok'}, 'title': {'title'},
    'done': {'tool_calls', 'stopped', 'configured'},
    'error': {'code', 'category', 'message'}, 'no_active': set(),
}


def new_run_id():
    return uuid4().hex


def error_payload(code, message=None):
    if code not in ERROR_CATEGORIES:
        code = 'CHAT_STREAM_PROTOCOL_ERROR'
    return {'type': 'error', 'code': code, 'category': ERROR_CATEGORIES[code],
            'message': message or '此轮操作未能完成，请按当前权限重新操作'}


def exception_payload(exc):
    if isinstance(exc, ValueError):
        return error_payload('CHAT_STREAM_PROTOCOL_ERROR')
    if isinstance(exc, HTTPException):
        code = exc.detail.get('code') if isinstance(exc.detail, dict) else None
        if code in ERROR_CATEGORIES:
            return error_payload(code)
        if code and code.startswith('AUTHN_'):
            return error_payload('AUTHN_SESSION_INVALID')
        if code and code.startswith('AUTHZ_'):
            return error_payload('AUTHZ_ENGINE_UNAVAILABLE' if exc.status_code >= 500 else 'AUTHZ_FORBIDDEN')
    return error_payload('CHAT_PROVIDER_UNAVAILABLE')


def normalize_payload(value):
    """Validate trusted producer output too; default only documented legacy omissions."""
    if type(value) is not dict or type(value.get('type')) is not str or value['type'] not in _PAYLOAD_KEYS:
        raise ValueError('Unknown Agent event')
    result = deepcopy(value)
    kind = result['type']
    if kind == 'done':
        result.setdefault('configured', True)
        result.setdefault('stopped', False)
    expected = {'type'} | _PAYLOAD_KEYS[kind]
    if kind == 'done' and 'answer' in result:
        expected.add('answer')
    if set(result) != expected:
        raise ValueError('Invalid Agent event fields')
    for key in ('text', 'title', 'name', 'answer', 'message'):
        if key in result and type(result[key]) is not str:
            raise ValueError('Invalid Agent event text')
    for key in ('ok', 'configured', 'stopped'):
        if key in result and type(result[key]) is not bool:
            raise ValueError('Invalid Agent event flag')
    if kind == 'tool' and type(result['args']) is not dict:
        raise ValueError('Tool arguments must be an object')
    if kind == 'done':
        if type(result['tool_calls']) is not list or any(
            type(call) is not dict or set(call) != {'name', 'args'}
            or type(call['name']) is not str or type(call['args']) is not dict
            for call in result['tool_calls']):
            raise ValueError('Invalid tool trace')
    if kind == 'error' and (type(result['code']) is not str or
                           ERROR_CATEGORIES.get(result['code']) != result['category']):
        raise ValueError('Invalid stream error category')
    canonical_json(result)  # reject NaN, Infinity and arbitrary Python values
    return result


def frame(payload, run_id, seq):
    if (run_id is not None and (type(run_id) is not str or not re.fullmatch('[0-9a-f]{32}', run_id))
            or type(seq) is not int or not 1 <= seq <= 2**53 - 1):
        raise ValueError('Invalid stream identity')
    payload = normalize_payload(payload)
    if run_id is None and (seq != 1 or payload['type'] not in ('error', 'no_active')):
        raise ValueError('Only an empty attach can omit run identity')
    if payload['type'] == 'no_active' and run_id is not None:
        raise ValueError('An actual run cannot be no_active')
    result = {**payload, 'run_id': run_id, 'seq': seq}
    encode(result)  # size before buffering or publication
    return result


def ready(session_id, run_id, context, *, replay_complete=True):
    if session_id is not None and (type(session_id) is not int or not 0 < session_id <= 2**53 - 1):
        raise ValueError('Invalid session identity')
    if run_id is not None and (type(run_id) is not str or not re.fullmatch('[0-9a-f]{32}', run_id)):
        raise ValueError('Invalid run identity')
    if type(replay_complete) is not bool or type(context) is not dict:
        raise ValueError('Invalid ready observation')
    if set(context) != {'schema_version', 'catalog_version', 'authorization_epoch', 'context', 'resources'}:
        raise ValueError('Ready requires the actual authorization wire')
    return {'type': 'ready', 'schema_version': 1, 'session_id': session_id,
            'run_id': run_id, 'authorization_context': deepcopy(context),
            'replay_complete': replay_complete}


def encode(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(data.encode('utf-8')) > MAX_EVENT_BYTES:
        raise ValueError('Agent event exceeds the protocol limit')
    return 'data: ' + data + '\n\n'
