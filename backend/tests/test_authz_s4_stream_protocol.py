"""Closed producer wire checks protect every subscriber, before hub buffering."""
import pytest

from app.agent import stream_protocol as wire


@pytest.mark.parametrize('payload', [
    {'type': []}, {'type': 'delta', 'text': 'x', 'extra': 'secret'},
    {'type': 'tool', 'name': 'tool', 'args': {'n': float('nan')}},
    {'type': 'tool_done', 'name': 'tool', 'ok': 1},
    {'type': 'done', 'tool_calls': [{'name': 'x', 'args': []}]},
    {'type': 'error', 'code': [], 'category': 'server', 'message': 'x'},
    {'type': 'error', 'code': 'AUTHZ_FORBIDDEN', 'category': 'provider', 'message': 'x'},
    {'type': 'delta', 'text': '汉' * (wire.MAX_EVENT_BYTES // 3)},
])
def test_malformed_producer_event_is_not_accepted(payload):
    with pytest.raises(ValueError):
        wire.frame(payload, 'a' * 32, 1)


def test_payload_is_copied_before_multiple_subscribers_can_observe_it():
    original = {'type': 'tool', 'name': 'search', 'args': {'rows': [{'query': 'captured'}]}}
    published = wire.frame(original, 'a' * 32, 1)
    original['args']['rows'][0]['query'] = 'changed'
    assert published['args']['rows'][0]['query'] == 'captured'


@pytest.mark.parametrize('run_id,seq,payload', [
    (None, 1, {'type': 'delta', 'text': 'x'}),
    (None, 2, wire.error_payload('AUTHZ_FORBIDDEN')),
    ('a' * 32, 1, {'type': 'no_active'}),
    ('a' * 32, True, {'type': 'delta', 'text': 'x'}),
    ('a' * 32, 2**53, {'type': 'delta', 'text': 'x'}),
])
def test_null_run_and_sequence_are_not_ambiguous(run_id, seq, payload):
    with pytest.raises(ValueError):
        wire.frame(payload, run_id, seq)
