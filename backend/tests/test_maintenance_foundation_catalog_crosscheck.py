"""Independent extension/dispatch counterexamples after the Foundation full gate.

Only the model transport and access-log sink are replaced in runtime tests.
Registration, composition, argument parsing, dispatch and result serialization run
through their real implementations; database authorization has a separate review.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from functools import partial
import json
from pathlib import Path

import pytest

from app import security
from app.agent import maintenance_tools, provider, runtime, tools
from app.agent.tool_catalog import ToolCatalog


def _schema(name):
    return {"type": "function", "function": {
        "name": name, "description": "Independent synthetic read",
        "parameters": {"type": "object", "properties": {
            "filters": {"type": "array", "items": {
                "type": "string", "enum": ["open", "closed"]}},
        }, "required": ["filters"]},
    }}


def _ctx(actor):
    return security.UserContext(user_id=actor, role="readonly", is_authenticated=True)


def _install(monkeypatch, catalog):
    specs, handlers = catalog.export()
    monkeypatch.setattr(tools, "TOOLS", specs)
    monkeypatch.setattr(tools, "_REGISTRY", handlers)
    audits = []
    monkeypatch.setattr(security, "record_access_log", lambda *args: audits.append(args))
    return specs, audits


def test_delayed_binding_cannot_overwrite_an_intervening_family():
    composed, family = ToolCatalog(), ToolCatalog()
    delayed = composed.register(_schema("read_late"))

    def authoritative(db, args, ctx):
        return {"owner": "family"}

    family.register(_schema("read_late"))(authoritative)
    composed.include(family)
    before = composed.export()
    with pytest.raises(ValueError, match="duplicate tool name: read_late"):
        delayed(lambda *_: {"owner": "delayed"})
    assert composed.export() == before
    assert composed.export()[1]["read_late"] is authoritative


@pytest.mark.parametrize("bad_kind", ["noncallable", "partial_coroutine", "bound_async_generator"])
def test_rejected_handler_does_not_reserve_a_name_or_poison_its_binder(bad_kind):
    catalog = ToolCatalog()
    bind = catalog.register(_schema("read_retry"))

    async def coroutine(db, args, ctx, *, unused):
        return {}

    class AsyncReader:
        async def read(self, db, args, ctx):
            yield {}

    bad = {"noncallable": object(), "partial_coroutine": partial(coroutine, unused=1),
           "bound_async_generator": AsyncReader().read}[bad_kind]
    with pytest.raises(TypeError):
        bind(bad)
    assert catalog.export() == ([], {})

    def recovered(db, args, ctx):
        return {"actor": ctx.user_id}

    assert bind(recovered) is recovered
    assert catalog.export()[1] == {"read_retry": recovered}


def test_nested_family_composition_dispatches_by_declared_name_not_python_name(monkeypatch):
    bundle, composed = ToolCatalog(), ToolCatalog()
    names = ["Read-" + "x" * 59, "read-" + "x" * 59]
    handlers = []
    for index, name in enumerate(names):
        family = ToolCatalog()

        def read(db, args, ctx, *, index=index):
            return {"family": index, "actor": ctx.user_id, "filters": args["filters"]}

        handlers.append(read)
        family.register(_schema(name))(read)
        bundle.include(family)
    composed.include(tools.catalog)
    composed.include(bundle)
    specs, audits = _install(monkeypatch, composed)
    assert [spec["function"]["name"] for spec in specs][-2:] == names
    assert handlers[0].__name__ == handlers[1].__name__ == "read"
    actor = _ctx("case-sensitive-reader")
    for index, name in enumerate(names):
        assert tools.dispatch(None, name, {"filters": ["open"]}, actor) == {
            "family": index, "actor": actor.user_id, "filters": ["open"]}
    assert [entry[1] for entry in audits] == [f"agent_tool:{name}" for name in names]


def test_pending_schema_and_sibling_startup_snapshots_cannot_mutate_live_adapter(monkeypatch):
    catalog = ToolCatalog()
    source = _schema("read_snapshot")
    expected = deepcopy(source)
    bind = catalog.register(source)
    # A declaration builder is reused before decorators are finally bound.
    source["function"]["name"] = "reused_builder"
    source["function"]["parameters"]["required"].clear()
    source["function"]["parameters"]["properties"]["filters"]["items"]["enum"].append("leak")

    def read(db, args, ctx):
        return {"actor": ctx.user_id}

    bind(read)
    live_specs, _ = _install(monkeypatch, catalog)
    sibling_specs, sibling_handlers = catalog.export()
    sibling_specs[0]["function"]["parameters"]["properties"]["filters"]["items"]["enum"].clear()
    sibling_specs.reverse()
    sibling_handlers["read_snapshot"] = lambda *_: {"actor": "wrong"}
    sibling_handlers["new_entry"] = read
    assert live_specs == [expected]
    assert catalog.export() == ([expected], {"read_snapshot": read})
    assert tools.dispatch(None, "read_snapshot", {}, _ctx("actual")) == {"actor": "actual"}
    assert tools.dispatch(None, "new_entry", {}, _ctx("actual")) == {
        "error": "未知工具: new_entry"}


def test_original_schemas_still_point_to_their_original_handlers_after_composition():
    # Fixed-main fixture independently extracted from 7a9fd209. Unlike a schema
    # equality test, this catches attaching a valid declaration to a wrong reader.
    expected = json.loads((Path(__file__).parent / "fixtures" /
                           "agent_tool_schemas_before_modularity.json").read_text())
    schemas, handlers = tools.catalog.export()
    assert schemas == list(expected.values())
    for name in expected:
        owner = maintenance_tools if name.startswith("get_maintenance_") else tools
        assert handlers[name] is getattr(owner, f"_{name}")


@pytest.mark.parametrize("streaming", [False, True])
def test_one_model_turn_continues_after_result_encoding_failure_and_unknown_tool(monkeypatch, streaming):
    family = ToolCatalog()
    seen = []

    @family.register(_schema("read_encoding"))
    def read(db, args, ctx):
        seen.append((ctx, deepcopy(args)))
        if args.get("cycle"):
            cycle = {"private": "DO_NOT_RETURN_PRIVATE_RESULT"}
            cycle["cycle"] = cycle
            return cycle
        return {"actor": ctx.user_id, "amount": Decimal("7.13"), "date": date(2026, 10, 23),
                "received": args}

    catalog = ToolCatalog()
    catalog.include(tools.catalog)
    catalog.include(family)
    specs, audits = _install(monkeypatch, catalog)
    actor = _ctx("multi-call-reader")
    calls = [
        provider.ToolCall(id="encoding-failure", name="read_encoding", arguments='{"cycle":true}'),
        provider.ToolCall(id="unknown", name="read_unregistered", arguments='{"x":1}'),
        provider.ToolCall(id="malformed", name="read_encoding", arguments='["not-an-object"]'),
        provider.ToolCall(id="recovered", name="read_encoding", arguments='{"filters":["open"]}'),
        provider.ToolCall(id="legacy", name="get_skill", arguments='{"skill":"absent"}'),
    ]
    provider_rounds = []

    def fake_provider(messages, declarations=None):
        provider_rounds.append(deepcopy(messages))
        assert declarations == specs
        if len(provider_rounds) == 1:
            yield "result", provider.ChatResult(content="", tool_calls=calls)
            return
        results = [message for message in messages if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == [call.id for call in calls]
        decoded = [json.loads(message["content"]) for message in results]
        assert decoded[0] == {"error": "工具执行失败，请换个方式或稍后重试",
                              "retriable": True, "kind": "internal"}
        assert decoded[1] == {"error": "未知工具: read_unregistered"}
        for index, received in [(2, {}), (3, {"filters": ["open"]})]:
            assert decoded[index] == {"actor": actor.user_id, "amount": "7.13", "date": "2026-10-23",
                                      "received": received}
        assert "技能不存在" in decoded[4]["error"]
        assert "DO_NOT_RETURN_PRIVATE_RESULT" not in json.dumps(results)
        yield "result", provider.ChatResult(content="recovered", tool_calls=[])

    monkeypatch.setattr(provider, "chat_stream", fake_provider)
    messages = [{"role": "user", "content": "read several synthetic sources"}]
    if streaming:
        events = list(runtime.run_stream(None, messages, actor))
        assert [event["ok"] for event in events if event["type"] == "tool_done"] == [False, False, True, True, False]
        result = events[-1]
    else:
        result = runtime.run(None, messages, actor)
    assert result["answer"] == "recovered"
    assert [item["name"] for item in result["tool_calls"]] == [call.name for call in calls]
    assert len(provider_rounds) == 2
    assert seen == [(actor, {"cycle": True}), (actor, {}), (actor, {"filters": ["open"]})]
    assert [entry[1] for entry in audits] == [f"agent_tool:{call.name}" for call in calls]


def test_interleaved_streams_use_each_request_context_with_one_shared_catalog(monkeypatch):
    catalog = ToolCatalog()
    seen = []

    @catalog.register(_schema("read_actor"))
    def read(db, args, ctx):
        seen.append(ctx)
        return {"actor": ctx.user_id, "requested": args["actor"]}

    _, audits = _install(monkeypatch, catalog)

    def fake_provider(messages, declarations=None):
        actor = next(message["content"] for message in messages if message["role"] == "user")
        results = [message for message in messages if message["role"] == "tool"]
        if not results:
            yield "result", provider.ChatResult(content="", tool_calls=[provider.ToolCall(
                id=f"call-{actor}", name="read_actor", arguments=json.dumps({"actor": actor}))])
        else:
            assert len(results) == 1 and results[0]["tool_call_id"] == f"call-{actor}"
            assert json.loads(results[0]["content"]) == {"actor": actor, "requested": actor}
            yield "result", provider.ChatResult(content=actor, tool_calls=[])

    monkeypatch.setattr(provider, "chat_stream", fake_provider)
    first, second = _ctx("first-request"), _ctx("second-request")
    first_stream = runtime.run_stream(None, [{"role": "user", "content": first.user_id}], first)
    second_stream = runtime.run_stream(None, [{"role": "user", "content": second.user_id}], second)
    assert next(first_stream)["type"] == "tool"
    assert next(second_stream)["type"] == "tool"
    # Both requests have yielded their tool before either handler executes.
    second_events = list(second_stream)
    first_events = list(first_stream)
    assert seen == [second, first]
    assert [entry[0] for entry in audits] == [second, first]
    assert second_events[-1]["answer"] == second.user_id
    assert first_events[-1]["answer"] == first.user_id
