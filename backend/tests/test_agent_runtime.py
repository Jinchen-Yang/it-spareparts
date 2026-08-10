"""Agent runtime PR-2：run/run_stream 合一(RUNTIME-1) + cancel 一等参数(RUNTIME-4) +
韧性配置(RUNTIME-3 max_tokens/max_retries、RUNTIME-6 extra_body 启动期校验)。

用假 provider.chat_stream 驱动，不依赖 LLM key / DB。"""
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import config, security
from app.agent import limits, provider, runtime, tools
from app.config import Settings

_CTX = security.UserContext(user_id=None, role="phase1_full_access")
_MSGS = [{"role": "user", "content": "hi"}]
_TOOL_ARG_SENTINEL = "CUSTOMER-RUNTIME-SENTINEL-8472"


@pytest.fixture(autouse=True)
def _explicit_non_rbac_runtime_unit_mode(monkeypatch):
    """Identity refresh is covered with real DB users in capability integration tests."""
    monkeypatch.setattr(config, "ENABLE_RBAC", False)


def _two_round_stream():
    """第1轮：旁白 + 工具调用；第2轮：最终答复（无工具）。"""
    calls = {"n": 0}

    def fake(messages, tools_=None, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield "delta", "我先查一下"
            yield "result", provider.ChatResult(
                content="我先查一下",
                tool_calls=[provider.ToolCall(id="c1", name="search_parts",
                                              arguments=json.dumps({
                                                  "query": _TOOL_ARG_SENTINEL,
                                              }))])
        else:
            yield "delta", "最终答复"
            yield "result", provider.ChatResult(content="最终答复", tool_calls=[])
    return fake


def _assert_value_free_search_trace(trace: list[dict]) -> None:
    assert len(trace) == 1
    entry = trace[0]
    assert entry["name"] == "search_parts"
    assert entry["args"]["arg_keys"] == ["query"]
    assert entry["args"]["arg_count"] == 1
    assert entry["args"]["string_lengths"] == {
        "query": len(_TOOL_ARG_SENTINEL),
    }
    assert _TOOL_ARG_SENTINEL not in json.dumps(trace, ensure_ascii=False)


def test_run_takes_final_answer_only(monkeypatch, caplog):
    monkeypatch.setattr(provider, "chat_stream", _two_round_stream())
    monkeypatch.setattr(tools, "dispatch", lambda db, n, a, c, **_kwargs: {"ok": True})
    out = runtime.run(None, _MSGS, _CTX)
    # 非流式只取最终答复，不含中间旁白（与旧实现口径一致）
    assert out["answer"] == "最终答复"
    _assert_value_free_search_trace(out["tool_calls"])
    assert _TOOL_ARG_SENTINEL not in caplog.text


def test_run_stream_emits_event_sequence_without_raw_tool_values(monkeypatch, caplog):
    monkeypatch.setattr(provider, "chat_stream", _two_round_stream())
    monkeypatch.setattr(tools, "dispatch", lambda db, n, a, c, **_kwargs: {"ok": True})
    evs = list(runtime.run_stream(None, _MSGS, _CTX))
    assert [e["type"] for e in evs] == ["delta", "tool", "tool_done", "delta", "done"]
    tool_done = next(e for e in evs if e["type"] == "tool_done")
    assert tool_done["ok"] is True
    tool_started = next(e for e in evs if e["type"] == "tool")
    assert tool_started["args_are_shape"] is True
    assert tool_started["args"]["arg_keys"] == ["query"]
    done = evs[-1]
    assert done["answer"] == "我先查一下\n\n最终答复"
    assert done["answer"] == "".join(
        event["text"] for event in evs if event["type"] == "delta"
    )
    _assert_value_free_search_trace(done["tool_calls"])
    assert _TOOL_ARG_SENTINEL not in json.dumps(evs, ensure_ascii=False)
    assert _TOOL_ARG_SENTINEL not in caplog.text


def test_runtime_trace_drops_format_valid_but_unowned_artifact_ids(monkeypatch):
    artifact_id = "a" * 12
    monkeypatch.setattr(provider, "chat_stream", _two_round_stream())
    monkeypatch.setattr(
        tools,
        "dispatch",
        lambda db, n, a, c, **_kwargs: {
            "ok": True,
            "artifact_id": artifact_id,
            "debug_reference": _TOOL_ARG_SENTINEL,
        },
    )

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    # A top-level Artifact ID is part of the source authority for that model-visible result.
    # An unowned ID therefore fails the whole release instead of merely hiding its public link.
    assert not any(event["type"] == "tool_done" for event in events)
    assert events[-1] == runtime.capability_revoked_error_event()
    assert _TOOL_ARG_SENTINEL not in json.dumps(events, ensure_ascii=False)


def test_stateless_run_and_stream_refresh_identity_at_final_artifact_projector(monkeypatch):
    artifact_id = "f" * 12

    def forged_loop(*_args, **_kwargs):
        yield {
            "type": "tool",
            "name": "write_report",
            "args": {},
            "args_are_shape": True,
        }
        yield {
            "type": "tool_done",
            "name": "write_report",
            "ok": True,
            "artifact_ids": [artifact_id],
        }
        yield {
            "type": "done",
            "answer": "safe",
            "tool_calls": [{
                "name": "write_report",
                "args": {},
                "args_are_shape": True,
                "artifact_ids": [artifact_id],
            }],
        }

    monkeypatch.setattr(runtime, "_agent_loop", forged_loop)
    monkeypatch.setattr(tools, "refresh_runtime_context", lambda _db, _ctx: None)

    result = runtime.run(None, _MSGS, _CTX)
    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert artifact_id not in json.dumps(result, ensure_ascii=False)
    assert artifact_id not in json.dumps(events, ensure_ascii=False)
    assert result["tool_calls"][0]["name"] == "write_report"
    assert events[-1]["type"] == "done"


def test_nonstream_model_gate_denial_never_calls_provider(monkeypatch):
    monkeypatch.setattr(runtime, "primary_model_call_allowed", lambda: False)
    monkeypatch.setattr(
        provider,
        "chat_stream",
        lambda *_args, **_kwargs: pytest.fail("denied context reached provider"),
    )

    result = runtime.run(None, _MSGS, _CTX)

    assert result["kind"] == "model_context_egress_denied"
    assert result["code"] == "AGENT_MODEL_EGRESS_DENIED"
    assert result["retriable"] is False
    assert result["tool_calls"] == []


def test_stream_model_gate_denial_never_calls_provider(monkeypatch):
    monkeypatch.setattr(runtime, "primary_model_call_allowed", lambda: False)
    monkeypatch.setattr(
        provider,
        "chat_stream",
        lambda *_args, **_kwargs: pytest.fail("denied context reached provider"),
    )

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert events == [{
        "type": "error",
        "message": runtime.MODEL_EGRESS_DENIED_MESSAGE,
        "kind": "model_context_egress_denied",
        "code": "AGENT_MODEL_EGRESS_DENIED",
        "retriable": False,
    }]


def test_error_projector_rebuilds_allowlisted_and_unknown_events_without_telemetry():
    canary = "RUNTIME-ERROR-TELEMETRY-CANARY-8472"

    known = runtime.project_error_event({
        "type": "error",
        "message": canary,
        "kind": canary,
        "code": runtime.MODEL_EGRESS_DENIED_CODE,
        "retriable": True,
        "args": canary,
        "result": canary,
    })
    unknown = runtime.project_error_event({
        "type": "error",
        "message": canary,
        "kind": canary,
        "code": canary,
        "retriable": canary,
        "debug": canary,
    })

    assert known == {
        "type": "error",
        "message": runtime.MODEL_EGRESS_DENIED_MESSAGE,
        "kind": "model_context_egress_denied",
        "code": runtime.MODEL_EGRESS_DENIED_CODE,
        "retriable": False,
    }
    assert unknown == {
        "type": "error",
        "message": runtime.GENERIC_ERROR_MESSAGE,
        "kind": runtime.GENERIC_ERROR_KIND,
        "code": runtime.GENERIC_ERROR_CODE,
        "retriable": True,
    }
    assert canary not in json.dumps([known, unknown], ensure_ascii=False)


def test_public_projector_rebuilds_terminal_answer_from_exact_visible_deltas():
    canary = "FORGED-DONE-ANSWER-CANARY-8472"
    projector = runtime.PublicEventProjector()
    events = []
    events.extend(projector.project({"type": "delta", "text": "safe <th"}))
    events.extend(projector.project({"type": "delta", "text": "ink>hidden</think> answer"}))
    events.extend(projector.project({"type": "done", "answer": canary, "tool_calls": []}))

    visible = "".join(event["text"] for event in events if event["type"] == "delta")
    done = events[-1]
    assert visible == "safe  answer"
    assert done["answer"] == visible
    assert canary not in json.dumps(events, ensure_ascii=False)


def test_public_projector_tiny_delta_work_and_answer_parts_are_hard_bounded(monkeypatch):
    monkeypatch.setattr(limits, "FIRST_STREAM_DELTA_BATCH_BYTES", 16)
    monkeypatch.setattr(limits, "STREAM_DELTA_BATCH_BYTES", 16)
    monkeypatch.setattr(limits, "MAX_PUBLIC_DELTA_EVENTS", 100)
    projector = runtime.PublicEventProjector()
    events = []

    for _ in range(1_616):
        events.extend(projector.project({"type": "delta", "text": "x"}))

    assert events[-1] == runtime.model_output_budget_error_event()
    assert len(projector._visible_parts) <= limits.MAX_PUBLIC_DELTA_EVENTS
    assert len(projector._pending_delta_parts) <= limits.STREAM_DELTA_BATCH_BYTES


def test_public_projector_artifact_ids_require_explicit_current_authorizer():
    own_id = "a" * 12
    other_id = "b" * 12
    event = {
        "type": "tool_done",
        "name": "write_report",
        "ok": True,
        "artifact_ids": [own_id, other_id],
    }

    default_projector = runtime.PublicEventProjector()
    default_projector.project({"type": "tool", "name": "write_report", "args": {}})
    default = default_projector.project(event)[0]
    admitted_projector = runtime.PublicEventProjector(
        lambda artifact_id: artifact_id == own_id
    )
    admitted_projector.project({"type": "tool", "name": "write_report", "args": {}})
    admitted = admitted_projector.project(event)[0]

    assert default["artifact_ids"] == []
    assert admitted["artifact_ids"] == [own_id]


def test_public_projector_rejects_unmatched_or_repeated_tool_done_events():
    unmatched = runtime.PublicEventProjector().project({
        "type": "tool_done",
        "name": "search_parts",
        "ok": True,
    })
    projector = runtime.PublicEventProjector()
    projector.project({"type": "tool", "name": "search_parts", "args": {}})
    first = projector.project({"type": "tool_done", "name": "search_parts", "ok": True})
    repeated = projector.project({"type": "tool_done", "name": "search_parts", "ok": True})

    assert unmatched == [runtime.tool_call_budget_error_event()]
    assert first[0]["type"] == "tool_done"
    assert repeated == [runtime.tool_call_budget_error_event()]


def test_model_gate_is_rechecked_before_each_provider_call(monkeypatch):
    decisions = iter((True, True, True, False))
    provider_calls = {"count": 0}

    def allowed():
        return next(decisions)

    def one_tool_round(messages, tools_=None, **_kwargs):
        provider_calls["count"] += 1
        yield "result", provider.ChatResult(
            content=None,
            tool_calls=[provider.ToolCall(
                id="c1",
                name="search_parts",
                arguments='{"query":"PN"}',
            )],
        )

    monkeypatch.setattr(runtime, "primary_model_call_allowed", allowed)
    monkeypatch.setattr(provider, "chat_stream", one_tool_round)
    monkeypatch.setattr(tools, "dispatch", lambda *_args, **_kwargs: {"ok": True})

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert provider_calls["count"] == 1
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "AGENT_MODEL_EGRESS_DENIED"


def test_model_gate_is_rechecked_before_iteration_limit_final_call(monkeypatch):
    decisions = iter((True, True, True, False))
    provider_calls = {"count": 0}

    def one_tool_round(messages, tools_=None, **_kwargs):
        provider_calls["count"] += 1
        yield "result", provider.ChatResult(
            content=None,
            tool_calls=[provider.ToolCall(
                id="c1",
                name="search_parts",
                arguments='{"query":"PN"}',
            )],
        )

    lease = replace(tools.capture_runtime_policy_lease(), max_tool_iters=1)
    monkeypatch.setattr(tools, "capture_runtime_policy_lease", lambda *_args: lease)
    monkeypatch.setattr(tools, "runtime_policy_lease_current", lambda _lease: True)
    monkeypatch.setattr(runtime, "primary_model_call_allowed", lambda: next(decisions))
    monkeypatch.setattr(provider, "chat_stream", one_tool_round)
    monkeypatch.setattr(tools, "dispatch", lambda *_args, **_kwargs: {"ok": True})

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert provider_calls["count"] == 1
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "AGENT_MODEL_EGRESS_DENIED"


def test_provider_boundary_policy_race_returns_stable_non_retriable_error(monkeypatch):
    def denied_at_network_boundary(*_args, **_kwargs):
        raise provider.ModelEgressDenied("must not leak")
        yield  # pragma: no cover - keeps this a generator like the real provider

    monkeypatch.setattr(runtime, "primary_model_call_allowed", lambda: True)
    monkeypatch.setattr(provider, "chat_stream", denied_at_network_boundary)

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "AGENT_MODEL_EGRESS_DENIED"
    assert events[-1]["retriable"] is False
    assert "must not leak" not in json.dumps(events, ensure_ascii=False)


def test_per_response_tool_call_cap_stops_all_handlers(monkeypatch, caplog):
    canary = "TOOL-CALL-CAP-CANARY-8472"
    calls = [
        provider.ToolCall(
            id=f"c{index}",
            name="search_parts",
            arguments=json.dumps({"query": canary}),
        )
        for index in range(limits.MAX_TOOL_CALLS_PER_RESPONSE + 1)
    ]
    dispatch_count = 0

    def fake(_messages, _schemas=None, **_kwargs):
        yield "result", provider.ChatResult(content=None, tool_calls=calls)

    def dispatch(*_args, **_kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        return {"ok": True}

    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(tools, "dispatch", dispatch)

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert dispatch_count == 0
    assert events == [runtime.tool_call_budget_error_event()]
    assert canary not in json.dumps(events, ensure_ascii=False)
    assert canary not in caplog.text


def test_total_tool_call_cap_stops_handler_at_exact_run_budget(monkeypatch):
    dispatch_count = 0

    def fake(_messages, _schemas=None, **_kwargs):
        yield "result", provider.ChatResult(
            content=None,
            tool_calls=[
                provider.ToolCall(
                    id=f"c{index}",
                    name="search_parts",
                    arguments='{"query":"PN"}',
                )
                for index in range(limits.MAX_TOOL_CALLS_PER_RESPONSE)
            ],
        )

    def dispatch(*_args, **_kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        return {"ok": True}

    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(tools, "dispatch", dispatch)

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert dispatch_count == limits.MAX_TOOL_CALLS_PER_RUN
    assert events[-1] == runtime.tool_call_budget_error_event()


def test_runtime_rejects_oversized_visible_response_before_sse(monkeypatch):
    def fake(_messages, _schemas=None, **_kwargs):
        yield "delta", "x" * (limits.MAX_VISIBLE_RESPONSE_BYTES + 1)
        yield "result", provider.ChatResult(content="unreachable", tool_calls=[])

    monkeypatch.setattr(provider, "chat_stream", fake)

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert events == [runtime.model_output_budget_error_event()]


def test_runtime_caps_cumulative_visible_output_across_tool_rounds(monkeypatch):
    dispatch_count = 0
    chunk = "x" * (limits.MAX_VISIBLE_RUN_BYTES // 4 - 16)

    def fake(_messages, _schemas=None, **_kwargs):
        yield "delta", chunk
        yield "result", provider.ChatResult(
            content=chunk,
            tool_calls=[provider.ToolCall(
                id="c",
                name="search_parts",
                arguments='{"query":"PN"}',
            )],
        )

    def dispatch(*_args, **_kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        return {"ok": True}

    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(tools, "dispatch", dispatch)

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert dispatch_count == 4
    assert events[-1] == runtime.model_output_budget_error_event()


def test_max_plain_chat_has_at_most_64_delta_reauthorizations(monkeypatch):
    ctx = security.UserContext(
        user_id="alice",
        role="sales",
        permissions={"page_chat": True},
        is_authenticated=True,
        authn="sys_user",
        token_version=0,
    )
    refresh_count = 0
    piece = "x" * 1024
    piece_count = limits.MAX_VISIBLE_RUN_BYTES // len(piece)

    def refresh(_db, _ctx):
        nonlocal refresh_count
        refresh_count += 1
        return ctx

    def fake(_messages, _schemas=None, **_kwargs):
        for _ in range(piece_count):
            yield "delta", piece
        yield "result", provider.ChatResult(
            content=piece * piece_count,
            tool_calls=[],
        )

    monkeypatch.setattr(tools, "refresh_runtime_context", refresh)
    monkeypatch.setattr(provider, "chat_stream", fake)

    events = list(runtime.run_stream(None, _MSGS, ctx))
    deltas = [event for event in events if event["type"] == "delta"]

    assert len(deltas) == limits.MAX_PUBLIC_DELTA_EVENTS == 64
    # One fresh identity read per released delta, plus initial, provider preflight and terminal
    # release. The response cannot amplify per-token DB reads.
    assert refresh_count == len(deltas) + 3
    assert refresh_count <= 128
    assert events[-1]["type"] == "done"


def test_page_chat_revoked_before_initial_model_call_never_reaches_provider(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    denied = security.UserContext(
        user_id="alice",
        role="sales",
        permissions={"page_chat": False},
        is_authenticated=True,
        authn="sys_user",
        token_version=0,
    )
    monkeypatch.setattr(tools, "refresh_runtime_context", lambda _db, _ctx: denied)
    monkeypatch.setattr(
        provider,
        "chat_stream",
        lambda *_args, **_kwargs: pytest.fail("revoked page reached provider"),
    )

    events = list(runtime.run_stream(None, _MSGS, denied))

    assert events == [runtime.capability_revoked_error_event()]


def test_page_chat_revoked_between_rounds_stops_before_next_provider_call(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    allowed = security.UserContext(
        user_id="alice",
        role="sales",
        permissions={"page_chat": True},
        is_authenticated=True,
        authn="sys_user",
        token_version=0,
    )
    denied = replace(allowed, permissions={"page_chat": False})
    refresh_count = 0
    provider_calls = 0

    def refresh(_db, _ctx):
        nonlocal refresh_count
        refresh_count += 1
        # Initial + first provider preflight + tool/public/post-handler/release checks pass. The
        # next iteration observes revocation before provider call 2.
        return allowed if refresh_count <= 6 else denied

    def fake(_messages, _schemas=None, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls > 1:
            pytest.fail("revoked page reached another provider round")
        yield "result", provider.ChatResult(
            content=None,
            tool_calls=[provider.ToolCall(
                id="c1",
                name="search_parts",
                arguments='{"query":"PN"}',
            )],
        )

    monkeypatch.setattr(tools, "refresh_runtime_context", refresh)
    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(tools, "dispatch", lambda *_args, **_kwargs: {"ok": True})

    events = list(runtime.run_stream(None, _MSGS, allowed))

    assert provider_calls == 1
    assert events[-1] == runtime.capability_revoked_error_event()


def test_oversized_tool_result_never_enters_next_model_context(monkeypatch, caplog):
    canary = "TOOL-RESULT-EGRESS-CANARY-8472"
    provider_calls = 0

    def fake(_messages, _schemas=None, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        yield "result", provider.ChatResult(
            content=None,
            tool_calls=[provider.ToolCall(
                id="c",
                name="search_parts",
                arguments='{"query":"PN"}',
            )],
        )

    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(
        tools,
        "dispatch",
        lambda *_args, **_kwargs: {
            "data": canary + "x" * limits.BUSINESS_RESULT_MAX_BYTES,
        },
    )

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert provider_calls == 1
    assert events[-1] == runtime.egress_payload_budget_error_event()
    assert canary not in json.dumps(events, ensure_ascii=False)
    assert canary not in caplog.text


def test_tool_result_policy_is_rechecked_after_handler_before_context_append(
    monkeypatch,
    caplog,
):
    canary = "POST-HANDLER-POLICY-DRIFT-CANARY-8472"
    allowed = True
    provider_calls = 0

    def fake(_messages, _schemas=None, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls > 1:
            pytest.fail("revoked tool result reached next provider call")
        yield "result", provider.ChatResult(
            content=None,
            tool_calls=[provider.ToolCall(
                id="c",
                name="search_parts",
                arguments='{"query":"PN"}',
            )],
        )

    def dispatch(*_args, **_kwargs):
        nonlocal allowed
        allowed = False
        return {"data": canary}

    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(tools, "dispatch", dispatch)
    monkeypatch.setattr(tools, "tool_result_egress_allowed", lambda _name: allowed)

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert provider_calls == 1
    assert events[-1]["code"] == runtime.MODEL_EGRESS_DENIED_CODE
    assert canary not in json.dumps(events, ensure_ascii=False)
    assert canary not in caplog.text


@pytest.mark.parametrize(
    ("tool_name", "arguments", "revocation"),
    [
        ("read_document", lambda file_id: {"file_id": file_id}, "owner_mismatch"),
        (
            "read_file_rows",
            lambda file_id: {"file_id": file_id, "start_row": 1, "max_rows": 1},
            "deleted",
        ),
        (
            "read_document_with_vision",
            lambda file_id: {"file_id": file_id},
            "identity_expired",
        ),
    ],
)
def test_released_file_owner_revocation_denies_next_provider_attempt(
    monkeypatch,
    tool_name,
    arguments,
    revocation,
):
    file_id = "a" * 12
    ctx = security.UserContext(
        user_id="alice",
        role="admin",
        is_authenticated=True,
        authn="sys_user",
        token_version=0,
    )
    owner = "alice"
    deleted = False
    identity_expired = False
    provider_calls = 0
    delegate_calls = 0

    def fake(_messages, _schemas=None, **kwargs):
        nonlocal deleted, delegate_calls, identity_expired, owner, provider_calls
        provider_calls += 1
        authorize_attempt = kwargs["_attempt_authorizer"]
        if provider_calls == 1:
            assert authorize_attempt() is True
            delegate_calls += 1
            yield "result", provider.ChatResult(
                content=None,
                tool_calls=[provider.ToolCall(
                    id="c",
                    name=tool_name,
                    arguments=json.dumps(arguments(file_id)),
                )],
            )
            return
        if revocation == "owner_mismatch":
            owner = "bob"
        elif revocation == "deleted":
            deleted = True
        else:
            identity_expired = True
        assert authorize_attempt() is False
        raise provider.ModelEgressDenied("provider request egress denied")

    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(
        tools,
        "dispatch",
        lambda *_args, **_kwargs: {"file_id": file_id, "content": "customer data"},
    )
    monkeypatch.setattr(
        tools,
        "capability_result_release_allowed",
        lambda _name, _ctx: True,
    )
    monkeypatch.setattr(
        tools,
        "serialize_tool_result_for_model",
        lambda _name, result: json.dumps(result),
    )
    def owner_of(_file_id):
        if deleted:
            raise tools.agent_files.FileError("deleted")
        return owner

    monkeypatch.setattr(tools.agent_files, "owner_of", owner_of)
    monkeypatch.setattr(
        tools,
        "refresh_runtime_context",
        lambda _db, _ctx: None if identity_expired else ctx,
    )

    events = list(runtime.run_stream(None, _MSGS, ctx))

    assert provider_calls == 2
    assert delegate_calls == 1
    assert events[-1]["code"] == runtime.MODEL_EGRESS_DENIED_CODE


def test_oversized_initial_context_never_reaches_provider(monkeypatch):
    provider_calls = 0

    def fake(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        yield "result", provider.ChatResult(content="unreachable", tool_calls=[])

    monkeypatch.setattr(provider, "chat_stream", fake)
    messages = [{
        "role": "user",
        "content": "x" * limits.CONVERSATION_CONTEXT_MAX_BYTES,
    }]

    events = list(runtime.run_stream(None, messages, _CTX))

    assert provider_calls == 0
    assert events == [runtime.egress_payload_budget_error_event()]


@pytest.mark.parametrize(
    "raw_arguments",
    [
        '{"query":"MALFORMED-ARGS-CANARY-8472"',
        "[]",
        "NaN",
        '{"limit":1,"limit":2}',
        "[" * 2_000 + "]" * 2_000,
        "9" * 10_000,
    ],
)
def test_malformed_optional_tool_arguments_fail_closed_without_handler_or_raw_echo(
    monkeypatch,
    raw_arguments,
):
    provider_calls = 0
    second_context = None

    def fake(messages, _schemas=None, **_kwargs):
        nonlocal provider_calls, second_context
        provider_calls += 1
        if provider_calls == 1:
            yield "result", provider.ChatResult(
                content=None,
                tool_calls=[provider.ToolCall(
                    id="c",
                    name="get_inventory",
                    arguments=raw_arguments,
                )],
            )
        else:
            second_context = json.dumps(messages, ensure_ascii=False)
            yield "result", provider.ChatResult(content="safe", tool_calls=[])

    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(
        tools.inventory,
        "list_dynamic",
        lambda *_args, **_kwargs: pytest.fail("malformed optional args reached handler"),
    )

    events = list(runtime.run_stream(None, _MSGS, _CTX))

    assert provider_calls == 2
    assert second_context is not None
    assert "AGENT_TOOL_ARGS_INVALID" in second_context
    assert '"arguments": "{}"' in second_context
    if "CANARY" in raw_arguments:
        assert raw_arguments not in second_context
    assert events[-1]["type"] == "done"


def test_run_stream_cancel_stops_promptly(monkeypatch):
    """cancel 置位后，循环在 chunk 间收束并发 stopped done（不等整轮跑完）。"""
    cancel = threading.Event()

    def fake(messages, tools_=None, **_kwargs):
        yield "delta", "部分"
        cancel.set()              # 模拟流中途用户点"停止"
        yield "delta", "更多"
        yield "result", provider.ChatResult(content="部分更多", tool_calls=[])
    monkeypatch.setattr(provider, "chat_stream", fake)

    evs = list(runtime.run_stream(None, _MSGS, _CTX, cancel=cancel))
    assert evs[-1]["type"] == "done" and evs[-1].get("stopped") is True
    # 取消后不应再有 tool 事件（没跑到工具就收束）
    assert not any(e["type"] == "tool" for e in evs)


# ---------- 韧性配置 ----------
def test_extra_body_validation_and_dict():
    s = Settings(llm_extra_body='{"thinking": {"type": "disabled"}}')
    assert s.llm_extra_body_dict() == {"thinking": {"type": "disabled"}}
    assert Settings(llm_extra_body="{}").llm_extra_body_dict() is None   # 空对象→不透传
    # 非法 JSON / 非 dict → 启动期拒绝（不再静默回退成 None）
    with pytest.raises(Exception):
        Settings(llm_extra_body="not-json")
    with pytest.raises(Exception):
        Settings(llm_extra_body="[1, 2]")


@pytest.mark.parametrize(
    "body",
    [
        '{"model":"override"}',
        '{"messages":[]}',
        '{"tools":[]}',
        '{"stream":false}',
        '{"unknown":true}',
        '{"thinking":{"type":"enabled"}}',
    ],
)
def test_extra_body_reserved_or_unapproved_options_fail_closed(body):
    with pytest.raises(Exception):
        Settings(llm_extra_body=body)


def test_create_kwargs_max_tokens_optional():
    snapshot = tools.capture_runtime_policy_lease().primary
    without_limit, _ = provider._create_kwargs(
        replace(snapshot, max_tokens=None), [], None, stream=None
    )
    with_limit, _ = provider._create_kwargs(
        replace(snapshot, max_tokens=1024), [], None, stream=None
    )
    assert "max_tokens" not in without_limit
    assert with_limit["max_tokens"] == 1024


# ---------- 供应商思考链必须丢弃 ----------
def test_default_extra_body_disables_thinking():
    # 减少供应商生成的敏感 telemetry；后端强制丢弃仍是真正安全边界。
    assert Settings(llm_extra_body="").llm_extra_body_dict() == {"thinking": {"type": "disabled"}}


def test_run_stream_discards_provider_reasoning_from_sse_trace_and_logs(monkeypatch, caplog):
    canary = "PROVIDER-REASONING-CANARY-8472"

    def fake(messages, tools_=None, **_kwargs):
        yield "reasoning", canary
        yield "delta", "建议 "
        yield "delta", "<th"
        yield "delta", f"ink>{canary}"
        yield "delta", "</thi"
        yield "delta", "nk>报 2200"
        yield "result", provider.ChatResult(
            content=f"建议 <think>{canary}</think>报 2200",
            tool_calls=[],
        )
    monkeypatch.setattr(provider, "chat_stream", fake)

    evs = list(runtime.run_stream(None, _MSGS, _CTX))

    assert not any(event["type"] == "thinking" for event in evs)
    assert canary not in json.dumps(evs, ensure_ascii=False)
    assert "<think>" not in json.dumps(evs, ensure_ascii=False)
    assert canary not in caplog.text
    done = evs[-1]
    assert done["type"] == "done" and done["answer"] == "建议 报 2200"


def test_run_ignores_thinking_in_answer(monkeypatch):
    def fake(messages, tools_=None, **_kwargs):
        yield "reasoning", "内部思考不该进答复"
        yield "delta", "最终答复"
        yield "result", provider.ChatResult(content="最终答复", tool_calls=[])
    monkeypatch.setattr(provider, "chat_stream", fake)
    out = runtime.run(None, _MSGS, _CTX)   # 非流式只取 done.answer
    assert out["answer"] == "最终答复" and "思考" not in out["answer"]
