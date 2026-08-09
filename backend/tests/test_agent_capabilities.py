"""Agent Capability Kernel security contracts (#219)."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.datastructures import UploadFile

from app import auth, config, security
from app.agent import provider, runtime, tools
from app.api import agent as agent_api, chat_sessions
from app.services import agent_files


def _sys_ctx(user_id: str = "alice", role: str = "sales") -> security.UserContext:
    return security.UserContext(
        user_id=user_id,
        role=role,
        is_authenticated=True,
        authn="sys_user",
    )


def test_egress_configuration_defaults_fail_closed():
    fields = config.Settings.model_fields
    assert fields["llm_trust_zone"].default == "unknown"
    assert fields["llm_private_base_urls"].default == ""
    assert fields["agent_model_context_egress_enabled"].default is False
    assert fields["agent_external_file_egress_enabled"].default is False


def test_upload_audit_records_only_extension_and_size(monkeypatch):
    secret = "CUSTOMER-SECRET-FILENAME-8472"
    events: list[tuple] = []
    monkeypatch.setattr(
        agent_api,
        "record_access_log",
        lambda ctx, action, resource, filters=None: events.append(
            (action, resource, filters)
        ),
    )
    content = b"safe local text"
    upload = UploadFile(file=BytesIO(content), filename=f"{secret}.txt")

    result = asyncio.run(agent_api.upload(upload, role="sales", ctx=_sys_ctx()))

    assert result["ext"] == "txt"
    assert events == [("upload", "agent_file", {"ext": "txt", "size_bytes": len(content)})]
    assert secret not in str(events)


def test_chat_access_audit_records_structure_without_prompt(monkeypatch):
    secret = "CUSTOMER-CHAT-PROMPT-SECRET-8472"
    events: list[tuple] = []
    monkeypatch.setattr(
        agent_api,
        "record_access_log",
        lambda ctx, action, resource, filters=None: events.append(
            (action, resource, filters)
        ),
    )
    monkeypatch.setattr(provider, "is_configured", lambda: False)
    req = agent_api.ChatRequest(messages=[
        agent_api.ChatMessage(role="user", content="first"),
        agent_api.ChatMessage(role="assistant", content="answer"),
        agent_api.ChatMessage(role="user", content=secret),
    ])
    ctx = _sys_ctx()

    agent_api.chat(req, db=None, role="sales", ctx=ctx)
    agent_api.chat_stream(req, db=None, role="sales", ctx=ctx)

    assert events == [
        (
            "chat",
            "agent",
            {
                "message_count": 3,
                "last_message_chars": len(secret),
                "endpoint": "chat",
                "stream": False,
            },
        ),
        (
            "chat_stream",
            "agent",
            {
                "message_count": 3,
                "last_message_chars": len(secret),
                "endpoint": "chat_stream",
                "stream": True,
            },
        ),
    ]
    assert secret not in str(events)


def test_persisted_chat_audit_records_structure_without_prompt(monkeypatch):
    secret = "SESSION-CHAT-PROMPT-SECRET-8472"
    events: list[tuple] = []
    monkeypatch.setattr(
        chat_sessions,
        "_owned_or_404",
        lambda db, ident, session_id: SimpleNamespace(id=session_id, title="safe"),
    )
    monkeypatch.setattr(chat_sessions, "acquire_session", lambda session_id: None)
    monkeypatch.setattr(
        chat_sessions,
        "record_access_log",
        lambda ctx, action, resource, filters=None: events.append(
            (action, resource, filters)
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        chat_sessions.chat_stream(
            42,
            chat_sessions.SendMessageRequest(message=secret),
            db=None,
            ident={"sub": "alice", "authn": "sys_user"},
            ctx=_sys_ctx(),
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert events == [(
        "chat_stream",
        "agent",
        {
            "message_count": 1,
            "last_message_chars": len(secret),
            "endpoint": "session_chat_stream",
            "stream": True,
            "session_id": 42,
        },
    )]
    assert secret not in str(events)


def test_user_context_preserves_verified_authentication_provenance(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    monkeypatch.setattr(
        auth,
        "verify_token_db",
        lambda token, db: {
            "sub": "alice",
            "role": "sales",
            "name": "Alice",
            "perms": {"page_chat": True},
            "authn": "sys_user",
        },
    )
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token")

    ctx = security.get_current_user_context(creds, db=None)

    assert ctx.authn == "sys_user"
    assert ctx.user_id == "alice"


def test_policy_fingerprint_is_stable_across_registration_order():
    baseline = tools.capability_policy_fingerprint(tools.TOOL_SPECS)

    assert tools.CAPABILITY_POLICY_VERSION == "v1"
    assert tools.CAPABILITY_POLICY_FINGERPRINT == baseline
    assert len(baseline) == 64
    assert tools.capability_policy_fingerprint(tuple(reversed(tools.TOOL_SPECS))) == baseline


def test_policy_fingerprint_changes_when_policy_metadata_changes():
    first = tools.TOOL_SPECS[0]
    changed = replace(first, sensitivity=tools.DataSensitivity.INTERNAL)
    changed_specs = (changed, *tools.TOOL_SPECS[1:])

    assert (
        tools.capability_policy_fingerprint(changed_specs)
        != tools.CAPABILITY_POLICY_FINGERPRINT
    )


def test_policy_fingerprint_changes_when_permission_policy_changes():
    first = tools.TOOL_SPECS[0]
    permission = tools._PagePermission("page_inventory")
    changed = replace(
        first,
        permission=permission,
        permission_id=permission.policy_id,
    )

    assert (
        tools.capability_policy_fingerprint((changed, *tools.TOOL_SPECS[1:]))
        != tools.CAPABILITY_POLICY_FINGERPRINT
    )


def test_policy_fingerprint_changes_when_function_parameters_change():
    first = tools.TOOL_SPECS[0]
    changed_schema = deepcopy(first.schema)
    changed_schema["function"]["parameters"]["properties"]["new_filter"] = {
        "type": "string",
    }
    changed = replace(first, schema=changed_schema)

    assert (
        tools.capability_policy_fingerprint((changed, *tools.TOOL_SPECS[1:]))
        != tools.CAPABILITY_POLICY_FINGERPRINT
    )


def test_policy_fingerprint_covers_budget_handler_version_and_stable_subjects(monkeypatch):
    first = tools.TOOL_SPECS[0]
    changed_budget = replace(
        first,
        budget=replace(first.budget, max_payload_bytes=first.budget.max_payload_bytes + 1),
    )
    changed_version = replace(first, implementation_version="audit-test-v2")

    def replacement_handler(db, args, ctx):
        return {"ok": True}

    changed_handler = replace(first, handler=replacement_handler)

    assert (
        tools.capability_policy_fingerprint((changed_budget, *tools.TOOL_SPECS[1:]))
        != tools.CAPABILITY_POLICY_FINGERPRINT
    )
    assert (
        tools.capability_policy_fingerprint((changed_version, *tools.TOOL_SPECS[1:]))
        != tools.CAPABILITY_POLICY_FINGERPRINT
    )
    assert (
        tools.capability_policy_fingerprint((changed_handler, *tools.TOOL_SPECS[1:]))
        != tools.CAPABILITY_POLICY_FINGERPRINT
    )

    monkeypatch.setattr(
        tools,
        "STABLE_SUBJECT_EFFECTS",
        frozenset({tools.ToolEffect.FILE_READ}),
    )
    assert tools.capability_policy_fingerprint() != tools.CAPABILITY_POLICY_FINGERPRINT


def test_runtime_policy_fingerprint_is_normalized_and_secret_free():
    base = SimpleNamespace(
        llm_trust_zone="private",
        llm_base_url="HTTP://GPU0.TAILNET:80/v1",
        llm_private_base_urls="http://gpu0.tailnet/, http://backup.tailnet:8000",
        agent_model_context_egress_enabled=True,
        agent_external_file_egress_enabled=False,
        llm_api_key="SECRET-A",
    )
    equivalent = SimpleNamespace(
        **{
            **base.__dict__,
            "llm_base_url": "http://gpu0.tailnet/another/path",
            "llm_private_base_urls": "http://backup.tailnet:8000 http://GPU0.TAILNET:80",
            "llm_api_key": "SECRET-B",
        }
    )
    changed = SimpleNamespace(**{**base.__dict__, "agent_external_file_egress_enabled": True})
    changed_zone = SimpleNamespace(**{**base.__dict__, "llm_trust_zone": "approved_external"})
    changed_origin = SimpleNamespace(
        **{**base.__dict__, "llm_base_url": "http://another.tailnet:8000/v1"}
    )
    changed_allowlist = SimpleNamespace(
        **{**base.__dict__, "llm_private_base_urls": "http://gpu0.tailnet"}
    )

    fingerprint = tools.runtime_policy_fingerprint(base)

    assert len(fingerprint) == 64
    assert tools.runtime_policy_fingerprint(equivalent) == fingerprint
    assert tools.runtime_policy_fingerprint(changed) != fingerprint
    assert tools.runtime_policy_fingerprint(changed_zone) != fingerprint
    assert tools.runtime_policy_fingerprint(changed_origin) != fingerprint
    assert tools.runtime_policy_fingerprint(changed_allowlist) != fingerprint
    assert "SECRET-A" not in fingerprint


def test_private_provider_requires_exact_normalized_origin_allowlist(monkeypatch):
    ctx = security.UserContext(user_id=None, role="phase1_full_access")
    settings = SimpleNamespace(
        llm_trust_zone="private",
        llm_base_url="https://api.deepseek.com/v1",
        llm_private_base_urls="",
        agent_model_context_egress_enabled=True,
        agent_external_file_egress_enabled=False,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)

    assert tools.tools_for(ctx) == []
    assert (
        tools.dispatch(None, "search_parts", {"query": "PN"}, ctx)["kind"]
        == "model_context_egress_denied"
    )

    settings.llm_base_url = "http://gpu0.tailnet:8000/v1"
    settings.llm_private_base_urls = "http://GPU0.TAILNET:8000/"
    assert "search_parts" in {schema["function"]["name"] for schema in tools.tools_for(ctx)}


@pytest.mark.parametrize(
    "base_url",
    [
        "http://user:password@gpu0.tailnet:8000/v1",
        "http://gpu0.tailnet:8000/v1?token=secret",
        "http://gpu0.tailnet:8000/v1#fragment",
        "http://10.0.0.5:8000/v1",
    ],
)
def test_private_provider_rejects_unsafe_or_unlisted_origins(monkeypatch, base_url):
    settings = SimpleNamespace(
        llm_trust_zone="private",
        llm_base_url=base_url,
        llm_private_base_urls="http://gpu0.tailnet:8000",
        agent_model_context_egress_enabled=True,
        agent_external_file_egress_enabled=False,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)

    assert tools.tools_for(security.UserContext(user_id=None, role="phase1_full_access")) == []


@pytest.mark.parametrize(
    "allowlist",
    [
        "http://user:password@gpu0.tailnet:8000",
        "http://gpu0.tailnet:8000?token=secret",
        "http://gpu0.tailnet:8000#fragment",
        "http://gpu0.tailnet:8000/v1",
    ],
)
def test_private_provider_rejects_unsafe_allowlist_entries(monkeypatch, allowlist):
    settings = SimpleNamespace(
        llm_trust_zone="private",
        llm_base_url="http://gpu0.tailnet:8000/v1",
        llm_private_base_urls=allowlist,
        agent_model_context_egress_enabled=True,
        agent_external_file_egress_enabled=False,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)

    assert tools.tools_for(security.UserContext(user_id=None, role="phase1_full_access")) == []


def test_tool_arguments_are_validated_before_handler(monkeypatch):
    monkeypatch.setattr(
        tools.part_resolver,
        "resolve",
        lambda *_args, **_kwargs: pytest.fail("invalid arguments must not reach handler"),
    )
    ctx = security.UserContext(user_id=None, role="phase1_full_access")

    undeclared = tools.dispatch(None, "search_parts", {"query": "PN", "write": True}, ctx)
    oversized = tools.dispatch(None, "search_parts", {"query": "X" * 501}, ctx)
    over_limit = tools.dispatch(None, "search_parts", {"query": "PN", "limit": 21}, ctx)

    assert undeclared == {
        "error": "工具参数不符合安全约束",
        "kind": "validation_error",
        "code": "AGENT_TOOL_ARGS_INVALID",
        "retriable": False,
    }
    for result in (oversized, over_limit):
        assert result["kind"] == "validation_error"
        assert result["code"] == "AGENT_TOOL_BUDGET_EXCEEDED"
        assert result["retriable"] is False


def test_all_capabilities_reject_undeclared_arguments_before_dispatch(monkeypatch):
    monkeypatch.setattr(tools, "_external_file_egress_enabled", lambda: True)
    ctx = _sys_ctx(role="admin")

    for spec in tools.TOOL_SPECS:
        result = tools.dispatch(None, spec.name, {"__undeclared": True}, ctx)
        assert result["code"] == "AGENT_TOOL_ARGS_INVALID", spec.name
        assert result["retriable"] is False, spec.name


def test_blank_multi_page_pdf_is_detected_from_real_content_only(monkeypatch, tmp_path):
    class BlankPage:
        @staticmethod
        def extract_text():
            return ""

        @staticmethod
        def extract_tables():
            return []

    class FakePdf:
        pages = [BlankPage() for _ in range(30)]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    import pdfplumber

    monkeypatch.setattr(pdfplumber, "open", lambda *_args, **_kwargs: FakePdf())

    text, scanned = agent_files._read_pdf(tmp_path / "blank.pdf")

    assert "[第30页]" in text
    assert scanned is True


def test_existing_tools_have_explicit_effects_sensitivity_and_egress():
    """Every model-visible tool comes from one typed, explicitly classified source."""
    expected_names = {schema["function"]["name"] for schema in tools.TOOLS}
    expected_effects = {
        "search_parts": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_part_overview": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "inspect_file": frozenset({tools.ToolEffect.FILE_READ}),
        "read_file_rows": frozenset({tools.ToolEffect.FILE_READ}),
        "lookup_prices_bulk": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "write_excel": frozenset({
            tools.ToolEffect.FILE_READ,
            tools.ToolEffect.ARTIFACT_CREATE,
        }),
        "read_document": frozenset({tools.ToolEffect.FILE_READ}),
        "read_document_with_vision": frozenset({tools.ToolEffect.FILE_READ}),
        "write_report": frozenset({tools.ToolEffect.ARTIFACT_CREATE}),
        "list_recent_purchases": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_profit_ranking": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_purchase_analysis": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_inventory": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_maintenance_board": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_maintenance_projects": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_maintenance_lines": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_cancellation_stats": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "list_skills": frozenset({tools.ToolEffect.BUSINESS_READ}),
        "get_skill": frozenset({tools.ToolEffect.BUSINESS_READ}),
    }
    expected_egress = {name: tools.EgressEffect.MODEL_CONTEXT for name in expected_names}
    expected_egress["read_document_with_vision"] = tools.EgressEffect.EXTERNAL_PROVIDER
    customer_file_tools = {
        "inspect_file",
        "read_file_rows",
        "write_excel",
        "read_document",
        "read_document_with_vision",
    }
    internal_tools = {"list_skills", "get_skill"}
    expected_sensitivity = {
        name: (
            tools.DataSensitivity.CUSTOMER_FILE
            if name in customer_file_tools
            else tools.DataSensitivity.INTERNAL
            if name in internal_tools
            else tools.DataSensitivity.BUSINESS_CONFIDENTIAL
        )
        for name in expected_names
    }
    expected_permissions = {
        "search_parts": "page:page_parts",
        "get_part_overview": "page:page_parts",
        "inspect_file": "allow",
        "read_file_rows": "allow",
        "lookup_prices_bulk": "page:page_parts",
        "write_excel": "allow",
        "read_document": "allow",
        "read_document_with_vision": "allow",
        "write_report": "allow",
        "list_recent_purchases": "page:page_purchases",
        "get_profit_ranking": "page:page_profit:deny_scoped_sales",
        "get_purchase_analysis": "page:page_purchases",
        "get_inventory": "page:page_inventory",
        "get_maintenance_board": "page:page_maintenance:deny_scoped_sales",
        "get_maintenance_projects": "page:page_maintenance",
        "get_maintenance_lines": "page:page_maintenance",
        "get_cancellation_stats": "page:page_purchases",
        "list_skills": "allow",
        "get_skill": "allow",
    }

    assert len(expected_names) == 19
    assert {spec.name for spec in tools.TOOL_SPECS} == expected_names
    assert {spec.name: spec.effects for spec in tools.TOOL_SPECS} == expected_effects
    assert {spec.name: spec.egress for spec in tools.TOOL_SPECS} == expected_egress
    assert {spec.name: spec.sensitivity for spec in tools.TOOL_SPECS} == expected_sensitivity
    assert {spec.name: spec.permission_id for spec in tools.TOOL_SPECS} == expected_permissions
    assert set(tools._REGISTRY) == expected_names
    assert all(isinstance(spec.effects, frozenset) and spec.effects for spec in tools.TOOL_SPECS)
    assert all(
        all(effect in tools.ALLOWED_TOOL_EFFECTS for effect in spec.effects)
        for spec in tools.TOOL_SPECS
    )
    assert all(spec.egress in tools.ALLOWED_EGRESS_EFFECTS for spec in tools.TOOL_SPECS)
    assert all(
        spec.sensitivity in tools.ALLOWED_DATA_SENSITIVITIES
        for spec in tools.TOOL_SPECS
    )
    assert all(callable(spec.permission) for spec in tools.TOOL_SPECS)
    assert all(isinstance(spec.enabled, bool) for spec in tools.TOOL_SPECS)
    assert all(isinstance(spec.budget, tools.ToolBudget) for spec in tools.TOOL_SPECS)
    assert all(callable(spec.validator) for spec in tools.TOOL_SPECS)
    assert all(spec.implementation_version for spec in tools.TOOL_SPECS)
    assert isinstance(tools._REGISTRY, MappingProxyType)
    assert isinstance(tools._SPEC_BY_NAME, MappingProxyType)
    for spec in tools.TOOL_SPECS:
        assert spec.name == spec.schema["function"]["name"]
        assert tools._REGISTRY[spec.name] is spec.handler

    assert not hasattr(tools._REGISTRY, "__setitem__")
    assert not hasattr(tools._SPEC_BY_NAME, "__setitem__")


def test_tool_budgets_cover_required_resource_dimensions():
    budgets = {spec.name: spec.budget for spec in tools.TOOL_SPECS}

    assert budgets["search_parts"].max_query_chars == tools._QUERY_CHARS_MAX
    assert budgets["get_part_overview"].max_pn_chars == tools._PN_CHARS_MAX
    assert budgets["search_parts"].max_limit == tools._SEARCH_LIMIT_MAX
    assert budgets["list_recent_purchases"].max_days == tools._RECENT_DAYS_MAX
    assert budgets["get_maintenance_lines"].max_page is not None
    assert budgets["read_file_rows"].max_rows == tools._READ_ROWS_MAX
    assert budgets["lookup_prices_bulk"].max_items == tools._BULK_MAX
    assert budgets["write_excel"].max_cells == agent_files._MAX_WRITE_CELLS
    assert budgets["inspect_file"].max_sheets == agent_files._MAX_INSPECT_SHEETS
    assert budgets["write_report"].max_output_name_chars is not None


def test_tools_for_returns_defensive_schema_copies():
    ctx = security.UserContext(user_id=None, role="phase1_full_access")
    first = tools.tools_for(ctx)
    original_name = first[0]["function"]["name"]
    first[0]["function"]["name"] = "attacker_mutated_name"
    first[0]["function"]["parameters"]["properties"].clear()

    second = tools.tools_for(ctx)
    by_name = {item["function"]["name"]: item for item in second}

    assert original_name in by_name
    assert by_name[original_name]["function"]["parameters"]["properties"]
    assert tools._SPEC_BY_NAME[original_name].name == original_name


def test_dispatch_executes_toolspec_handler_without_registry_lookup(monkeypatch):
    monkeypatch.setattr(tools, "_REGISTRY", None)
    monkeypatch.setattr(
        tools.part_resolver,
        "resolve",
        lambda *_args, **_kwargs: {"source": "immutable-spec"},
    )
    ctx = security.UserContext(user_id=None, role="phase1_full_access")

    assert tools.dispatch(None, "search_parts", {"query": "PN"}, ctx) == {
        "source": "immutable-spec"
    }


@pytest.mark.parametrize("tool_name", ["read_document", "write_excel", "write_report"])
def test_file_and_artifact_capabilities_require_stable_subject(monkeypatch, tool_name):
    service_name = {
        "read_document": "read_document",
        "write_excel": "write_excel",
        "write_report": "write_report",
    }[tool_name]
    monkeypatch.setattr(
        agent_files,
        service_name,
        lambda *_args, **_kwargs: pytest.fail("denied capability reached handler"),
    )
    shared = security.UserContext(
        user_id="admin",
        role="admin",
        is_authenticated=True,
        authn="shared",
    )
    visible = {schema["function"]["name"] for schema in tools.tools_for(shared)}

    result = tools.dispatch(None, tool_name, {}, shared)

    assert tool_name not in visible
    assert result["kind"] == "capability_denied"


def test_stable_subject_can_see_local_file_and_artifact_capabilities():
    visible = {schema["function"]["name"] for schema in tools.tools_for(_sys_ctx())}

    assert {"read_document", "write_excel", "write_report"} <= visible


def test_denied_capability_is_hidden_and_cannot_be_dispatched(monkeypatch):
    """Hiding a schema is UX; dispatch must independently enforce the same policy."""
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    ctx = security.UserContext(
        user_id="sales-1",
        role="sales",
        permissions={"page_parts": True, "page_inventory": False},
        is_authenticated=True,
    )
    monkeypatch.setattr(
        tools.inventory,
        "list_dynamic",
        lambda *_args, **_kwargs: pytest.fail("denied capability reached handler"),
    )

    visible = {schema["function"]["name"] for schema in tools.tools_for(ctx)}
    assert "search_parts" in visible
    assert "get_inventory" not in visible

    result = tools.dispatch(None, "get_inventory", {}, ctx)
    assert result["kind"] == "capability_denied"
    assert "无权限" in result["error"]


def test_runtime_only_sends_context_allowed_schemas_to_model(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    ctx = security.UserContext(
        user_id="sales-1",
        role="sales",
        permissions={"page_parts": True, "page_inventory": False},
        is_authenticated=True,
    )
    captured: list[dict] = []

    def fake_chat_stream(messages, schemas=None):
        captured.extend(schemas or [])
        yield "result", provider.ChatResult(content="ok", tool_calls=[])

    monkeypatch.setattr(provider, "chat_stream", fake_chat_stream)

    assert runtime.run(None, [{"role": "user", "content": "hi"}], ctx)["answer"] == "ok"
    names = {schema["function"]["name"] for schema in captured}
    assert "search_parts" in names
    assert "get_inventory" not in names


@pytest.mark.parametrize(
    ("trust_zone", "model_context_enabled"),
    [("unknown", True), ("private", False), ("approved_external", False)],
)
def test_model_context_requires_known_zone_and_explicit_opt_in(
    monkeypatch,
    trust_zone,
    model_context_enabled,
):
    settings = SimpleNamespace(
        llm_trust_zone=trust_zone,
        llm_base_url="http://agent-private.test:8000/v1",
        llm_private_base_urls="http://agent-private.test:8000",
        agent_model_context_egress_enabled=model_context_enabled,
        agent_external_file_egress_enabled=False,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    ctx = security.UserContext(user_id=None, role="phase1_full_access")
    monkeypatch.setattr(
        tools.part_resolver,
        "resolve",
        lambda *_args, **_kwargs: pytest.fail("denied capability reached handler"),
    )

    visible = {schema["function"]["name"] for schema in tools.tools_for(ctx)}
    result = tools.dispatch(None, "search_parts", {"query": "PN"}, ctx)

    assert "search_parts" not in visible
    assert result["kind"] == "model_context_egress_denied"


def test_registry_projection_cannot_be_extended_to_bypass_capability_policy():
    assert "rogue_business_write" not in tools._REGISTRY
    ctx = security.UserContext(user_id=None, role="phase1_full_access")

    result = tools.dispatch(None, "rogue_business_write", {}, ctx)
    assert result["kind"] == "capability_denied"


@pytest.mark.parametrize(
    "bad_effects",
    [None, frozenset(), frozenset({"business_write"}), {tools.ToolEffect.BUSINESS_READ}],
)
def test_unclassified_or_business_write_effect_is_rejected(bad_effects):
    original = tools._SPEC_BY_NAME["search_parts"]
    poisoned = replace(original, effects=bad_effects)
    ctx = security.UserContext(user_id=None, role="phase1_full_access")

    assert tools._allowed(poisoned, ctx) is False
    with pytest.raises(ValueError):
        tools.capability_policy_fingerprint((poisoned,))


def test_write_excel_composite_effects_are_audited(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        security,
        "record_access_log",
        lambda ctx, action, resource, filters=None: events.append(filters or {}),
    )
    monkeypatch.setattr(agent_files, "write_excel", lambda *_args: {"ok": True})
    ctx = _sys_ctx()

    assert tools.dispatch(None, "write_excel", {"cells": []}, ctx) == {"ok": True}
    assert events[0]["effects"] == ["artifact_create", "file_read"]
    assert events[0]["sensitivity"] == "customer_file"


def test_undeclared_egress_is_rejected():
    original = tools._SPEC_BY_NAME["search_parts"]
    poisoned = replace(original, egress=None)
    ctx = _sys_ctx(role="phase1_full_access")

    assert tools._allowed(poisoned, ctx) is False
    with pytest.raises(ValueError):
        tools.capability_policy_fingerprint((poisoned,))


@pytest.mark.parametrize("bad_sensitivity", [None, "customer_file", "public"])
def test_undeclared_sensitivity_is_rejected(bad_sensitivity):
    original = tools._SPEC_BY_NAME["search_parts"]
    poisoned = replace(original, sensitivity=bad_sensitivity)
    ctx = _sys_ctx(role="phase1_full_access")

    assert tools._allowed(poisoned, ctx) is False
    with pytest.raises(ValueError):
        tools.capability_policy_fingerprint((poisoned,))


def test_approved_external_provider_needs_file_authorization_for_customer_files(monkeypatch):
    settings = SimpleNamespace(
        llm_trust_zone="approved_external",
        llm_base_url="https://api.deepseek.com/v1",
        llm_private_base_urls="",
        agent_model_context_egress_enabled=True,
        agent_external_file_egress_enabled=False,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    ctx = _sys_ctx(role="phase1_full_access")
    monkeypatch.setattr(
        agent_files,
        "read_document",
        lambda *_args, **_kwargs: pytest.fail("denied capability reached handler"),
    )

    visible = {schema["function"]["name"] for schema in tools.tools_for(ctx)}
    result = tools.dispatch(None, "read_document", {"file_id": "secret"}, ctx)

    assert "search_parts" in visible
    assert "read_document" not in visible
    assert result["kind"] == "sensitivity_egress_denied"


def test_private_provider_can_use_customer_file_tools_without_external_file_flag(monkeypatch):
    settings = SimpleNamespace(
        llm_trust_zone="private",
        llm_base_url="http://gpu0.tailnet:8000/v1",
        llm_private_base_urls="http://gpu0.tailnet:8000",
        agent_model_context_egress_enabled=True,
        agent_external_file_egress_enabled=False,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_files, "read_document", lambda *_args: {"ok": True})
    ctx = _sys_ctx(role="phase1_full_access")

    visible = {schema["function"]["name"] for schema in tools.tools_for(ctx)}

    assert "read_document" in visible
    assert tools.dispatch(None, "read_document", {"file_id": "local"}, ctx) == {"ok": True}
    assert "read_document_with_vision" not in visible


def test_approved_external_provider_can_use_customer_files_after_explicit_authorization(
    monkeypatch,
):
    settings = SimpleNamespace(
        llm_trust_zone="approved_external",
        llm_base_url="https://api.deepseek.com/v1",
        llm_private_base_urls="",
        agent_model_context_egress_enabled=True,
        agent_external_file_egress_enabled=True,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_files, "read_document", lambda *_args: {"ok": True})
    ctx = _sys_ctx(role="phase1_full_access")

    visible = {schema["function"]["name"] for schema in tools.tools_for(ctx)}

    assert "read_document" in visible
    assert tools.dispatch(None, "read_document", {"file_id": "approved"}, ctx) == {"ok": True}


def test_local_read_flags_image_and_external_tool_denial_never_calls_vision(
    db,
    monkeypatch,
):
    """Local inspection is safe; the explicit Vision capability is hidden and denied."""
    upload = agent_files.save_upload(b"untrusted image bytes", "customer.png", "alice")
    ctx = _sys_ctx()
    called = False

    def forbidden_vision(images, hint):
        nonlocal called
        called = True
        return "should never run"

    monkeypatch.setattr(provider, "vision_extract", forbidden_vision)
    monkeypatch.setattr(
        tools,
        "_external_file_egress_enabled",
        lambda: False,
        raising=False,
    )

    local = tools.dispatch(None, "read_document", {"file_id": upload["file_id"]}, ctx)
    visible = {schema["function"]["name"] for schema in tools.tools_for(ctx)}
    denied = tools.dispatch(
        None,
        "read_document_with_vision",
        {"file_id": upload["file_id"]},
        ctx,
    )

    assert local["requires_vision"] is True
    assert local["vision_used"] is False
    assert "read_document" in visible
    assert "read_document_with_vision" not in visible
    assert denied["kind"] == "external_egress_denied"
    assert called is False


def test_external_egress_denial_does_not_block_local_document_parsing(db, monkeypatch):
    upload = agent_files.save_upload(b"local-only text", "notes.txt", "alice")
    ctx = _sys_ctx()
    monkeypatch.setattr(tools, "_external_file_egress_enabled", lambda: False)
    monkeypatch.setattr(
        provider,
        "vision_extract",
        lambda *_args, **_kwargs: pytest.fail("local text must not call Vision"),
    )

    result = tools.dispatch(None, "read_document", {"file_id": upload["file_id"]}, ctx)

    assert result["content"] == "local-only text"
    assert result["vision_used"] is False


def test_text_pdf_stays_local_when_external_egress_is_disabled(db, monkeypatch):
    upload = agent_files.save_upload(b"fake pdf", "text.pdf", "alice")
    ctx = _sys_ctx()
    monkeypatch.setattr(tools, "_external_file_egress_enabled", lambda: False)
    monkeypatch.setattr(agent_files, "_read_pdf", lambda path: ("local PDF text", False))
    monkeypatch.setattr(
        provider,
        "vision_extract",
        lambda *_args, **_kwargs: pytest.fail("text PDF must not call Vision"),
    )

    result = tools.dispatch(None, "read_document", {"file_id": upload["file_id"]}, ctx)

    assert result["content"] == "local PDF text"
    assert result["requires_vision"] is False


def test_authorized_external_vision_capability_is_visible_and_calls_provider(db, monkeypatch):
    upload = agent_files.save_upload(b"image bytes", "customer.png", "alice")
    ctx = _sys_ctx()
    calls: list[tuple] = []

    monkeypatch.setattr(tools, "_external_file_egress_enabled", lambda: True)
    monkeypatch.setattr(
        provider,
        "vision_extract",
        lambda images, hint: calls.append((images, hint)) or "recognized text",
    )

    visible = {schema["function"]["name"] for schema in tools.tools_for(ctx)}
    result = tools.dispatch(
        None,
        "read_document_with_vision",
        {"file_id": upload["file_id"]},
        ctx,
    )

    assert "read_document_with_vision" in visible
    assert result["content"] == "recognized text"
    assert result["vision_used"] is True
    assert len(calls) == 1


def test_authorized_vision_without_provider_config_remains_pending(db, monkeypatch):
    upload = agent_files.save_upload(b"image bytes", "customer.png", "alice")
    monkeypatch.setattr(tools, "_external_file_egress_enabled", lambda: True)

    def unconfigured(*_args, **_kwargs):
        raise provider.VisionNotConfigured("secret provider detail")

    monkeypatch.setattr(provider, "vision_extract", unconfigured)

    result = tools.dispatch(
        None,
        "read_document_with_vision",
        {"file_id": upload["file_id"]},
        _sys_ctx(),
    )

    assert result["requires_vision"] is True
    assert result["vision_used"] is False
    assert "未配置视觉模型" in result["content"]
    assert "secret provider detail" not in result["content"]


def test_external_vision_capability_preserves_file_owner_acl(db, monkeypatch):
    upload = agent_files.save_upload(b"image bytes", "customer.png", "alice")
    bob = _sys_ctx("bob")
    monkeypatch.setattr(tools, "_external_file_egress_enabled", lambda: True)
    monkeypatch.setattr(
        provider,
        "vision_extract",
        lambda *_args, **_kwargs: pytest.fail("non-owner must not call Vision"),
    )

    result = tools.dispatch(
        None,
        "read_document_with_vision",
        {"file_id": upload["file_id"]},
        bob,
    )

    assert result == tools._NO_ACCESS


def test_allowed_dispatch_audit_contains_shape_but_no_argument_values(monkeypatch, caplog):
    secret = "CUSTOMER-SECRET-NORMAL-8472"
    access_events: list[tuple] = []

    def capture_access(ctx, action, resource, filters=None):
        access_events.append((action, resource, filters))

    monkeypatch.setattr(security, "record_access_log", capture_access)
    monkeypatch.setattr(agent_files, "write_report", lambda *_args, **_kwargs: {"ok": True})
    ctx = _sys_ctx()

    result = tools.dispatch(
        None,
        "write_report",
        {"title": secret, "headers": [secret], "rows": [[secret]]},
        ctx,
    )

    assert result == {"ok": True}
    assert access_events[0][2]["arg_keys"] == ["headers", "rows", "title"]
    assert access_events[0][2]["collection_counts"] == {"headers": 1, "rows": 1}
    assert access_events[0][2]["effects"] == ["artifact_create"]
    assert access_events[0][2]["sensitivity"] == "business_confidential"
    assert secret not in str(access_events)
    assert secret not in caplog.text


def test_denied_dispatch_audit_does_not_log_untrusted_name_or_values(monkeypatch, caplog):
    secret = "https://secret.invalid/?api_key=DENIED-8472"
    access_events: list[tuple] = []

    monkeypatch.setattr(
        security,
        "record_access_log",
        lambda ctx, action, resource, filters=None: access_events.append(
            (action, resource, filters)
        ),
    )
    ctx = security.UserContext(user_id="alice", role="sales", is_authenticated=True)

    result = tools.dispatch(None, secret, {"api_key": secret}, ctx)

    assert result["kind"] == "capability_denied"
    assert access_events[0][0] == "agent_tool_denied:unknown"
    assert access_events[0][2] == {"arg_count": 1, "arg_keys": []}
    assert secret not in str(access_events)
    assert secret not in caplog.text


def test_exception_dispatch_logs_type_but_no_args_or_exception_message(monkeypatch):
    secret = "SUPPLIER-SECRET-EXCEPTION-8472"
    access_events: list[tuple] = []
    log_events: list[tuple] = []

    def explode(db, query, **kwargs):
        raise RuntimeError(f"upstream included {secret}")

    monkeypatch.setattr(
        security,
        "record_access_log",
        lambda ctx, action, resource, filters=None: access_events.append(
            (action, resource, filters)
        ),
    )
    monkeypatch.setattr(tools.part_resolver, "resolve", explode)
    monkeypatch.setattr(
        tools._log,
        "error",
        lambda message, *args, **kwargs: log_events.append((message, args, kwargs)),
    )
    ctx = security.UserContext(user_id=None, role="phase1_full_access")

    result = tools.dispatch(None, "search_parts", {"query": secret}, ctx)

    assert result["kind"] == "internal"
    assert log_events == [
        ("agent tool failed name=%s exception_type=%s", ("search_parts", "RuntimeError"), {})
    ]
    assert secret not in str(access_events)
    assert secret not in str(log_events)
