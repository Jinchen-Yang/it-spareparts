"""Tool families extend discovery and dispatch without runtime edits."""
import json
from pathlib import Path

import pytest

from app import security
from app.agent import maintenance_tools, tools
from app.agent.tool_catalog import ToolCatalog


def schema(name):
    return {"type": "function", "function": {"name": name, "description": "Synthetic read",
            "parameters": {"type": "object", "properties": {}}}}


def test_new_family_is_discovered_and_dispatched_with_existing_audit(monkeypatch):
    family = ToolCatalog()

    @family.register(schema("read_extension"))
    def read_extension(db, args, ctx):
        return {"actor": ctx.user_id, "value": args["value"]}

    composed = ToolCatalog()
    composed.include(maintenance_tools.catalog)
    composed.include(family)
    specs, handlers = composed.export()
    assert "read_extension" in {item["function"]["name"] for item in specs}
    audit = []
    monkeypatch.setattr(security, "record_access_log", lambda *args: audit.append(args))
    monkeypatch.setattr(tools, "_REGISTRY", handlers)
    ctx = security.UserContext(user_id="extension-user", role="admin")
    assert tools.dispatch(None, "read_extension", {"value": 7}, ctx) == {
        "actor": "extension-user", "value": 7}
    assert audit[0][1] == "agent_tool:read_extension"


def test_duplicate_family_fails_atomically():
    first, second = ToolCatalog(), ToolCatalog()
    first.register(schema("existing"))(lambda *_: {})
    second.register(schema("new"))(lambda *_: {})
    second.register(schema("existing"))(lambda *_: {})
    with pytest.raises(ValueError, match="duplicate"):
        first.include(second)
    assert set(first.export()[1]) == {"existing"}
    with pytest.raises(ValueError, match="duplicate"):
        first.register(schema("existing"))(lambda *_: {})


@pytest.mark.parametrize("spec", [None, {}, {"type": "function", "function": None},
    {"type": "function", "function": {"name": "bad", "description": "bad", "parameters": None}}, {"type": "function", "function": {}},
    {"type": "function", "function": {"name": "bad", "description": "bad"}}])
def test_malformed_definition_rejected(spec):
    with pytest.raises(ValueError):
        ToolCatalog().register(spec)


def test_schema_mutation_cannot_change_registered_definition():
    catalog = ToolCatalog()
    spec = schema("read_one")
    catalog.register(spec)(lambda *_: {})
    spec["function"]["name"] = "changed"
    exported, handlers = catalog.export()
    exported[0]["function"]["parameters"]["properties"]["injected"] = {}
    assert catalog.export()[0] == [schema("read_one")]
    assert set(handlers) == {"read_one"}


def test_existing_tool_contracts_remain_unchanged():
    fixture = Path(__file__).parent / "fixtures" / "agent_tool_schemas_before_modularity.json"
    expected = json.loads(fixture.read_text())
    assert tools.TOOLS == list(expected.values())
    assert set(tools._REGISTRY) == set(expected)
    assert set(maintenance_tools.catalog.export()[1]) == {
        "get_maintenance_board", "get_maintenance_projects", "get_maintenance_lines"}


@pytest.mark.parametrize("name", ["", " ", "bad name", "bad.name", "维保", "x" * 65])
def test_provider_invalid_tool_names_fail_before_composition(name):
    with pytest.raises(ValueError, match="name"):
        ToolCatalog().register(schema(name))


@pytest.mark.parametrize("parameters", [
    {"type": "object", "properties": []},
    {"type": "object", "properties": {}, "required": "project"},
    {"type": "object", "properties": {}, "required": [1]},
    {"type": "object", "properties": {}, "required": ["missing"]},
    {"type": "object", "properties": {"project": {}}, "required": ["project", "project"]},
])
def test_invalid_parameter_shape_fails_before_composition(parameters):
    spec = schema("bad_parameters")
    spec["function"]["parameters"] = parameters
    with pytest.raises(ValueError, match="parameters|required|properties"):
        ToolCatalog().register(spec)


def test_async_handlers_fail_before_composition():
    async def asynchronous(db, args, ctx):
        return {}

    class AsyncCallable:
        async def __call__(self, db, args, ctx):
            return {}

    async def async_generator(db, args, ctx):
        yield {}

    catalog = ToolCatalog()
    for handler in (asynchronous, AsyncCallable(), async_generator):
        with pytest.raises(TypeError, match="synchronous"):
            catalog.register(schema("async_read"))(handler)
    assert catalog.export() == ([], {})


def test_export_is_a_startup_snapshot_not_hot_registration():
    catalog = ToolCatalog()
    catalog.register(schema("first"))(lambda *_: {})
    specs, handlers = catalog.export()
    catalog.register(schema("later"))(lambda *_: {})
    assert [spec["function"]["name"] for spec in specs] == ["first"]
    assert set(handlers) == {"first"}
    assert set(catalog.export()[1]) == {"first", "later"}


@pytest.mark.parametrize("top,expected", [(None, 20), (0, 20), (-1, -1), (-100, -100), (1, 1), (20, 20), (50, 50), (51, 50)])
def test_maintenance_project_limit_preserves_existing_slicing(monkeypatch, top, expected):
    # This extraction preserves legacy slicing, including negative values.
    # Changing validation semantics requires a separate reviewed business change.
    rows = [{"project": str(index)} for index in range(100)]
    monkeypatch.setattr(maintenance_tools.maintenance_cost, "projects_aggregate",
                        lambda *args, **kwargs: {"rows": rows})
    monkeypatch.setattr(security, "record_access_log", lambda *args: None)
    ctx = security.UserContext(user_id="limit-reviewer", role="admin")
    result = tools.dispatch(None, "get_maintenance_projects", {"top": top}, ctx)
    assert result["rows"] == rows[:expected]


def test_invalid_project_limit_preserves_safe_dispatch_error(monkeypatch):
    def unexpected_read(*args, **kwargs):
        pytest.fail("invalid numeric limit must fail before business reads")

    monkeypatch.setattr(maintenance_tools.maintenance_cost, "projects_aggregate", unexpected_read)
    monkeypatch.setattr(security, "record_access_log", lambda *args: None)
    ctx = security.UserContext(user_id="limit-reviewer", role="admin")
    result = tools.dispatch(None, "get_maintenance_projects", {"top": "invalid"}, ctx)
    assert result == {"error": "工具执行失败，请换个方式或稍后重试", "retriable": True, "kind": "internal"}
