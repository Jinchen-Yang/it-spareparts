"""HTTP client security contracts for primary and Vision model providers."""

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.agent import limits, provider, tools


def _private_provider_settings(**overrides):
    values = {
        "environment": "test",
        "agent_allow_loopback_http": False,
        "agent_allow_unattested_private_for_development": True,
        "agent_model_context_egress_enabled": True,
        "agent_external_file_egress_enabled": False,
        "enable_agent": True,
        "llm_api_key": "test-primary-key",
        "llm_provider": "openai_compatible",
        "llm_base_url": "https://primary.private.test/v1",
        "llm_model": "primary-model",
        "llm_approved_models": "primary-model,primary-model-v2",
        "llm_timeout_seconds": 2,
        "llm_max_retries": 1,
        "llm_max_tokens": None,
        "llm_max_tool_iters": 8,
        "llm_trust_zone": "private",
        "llm_private_base_urls": "https://primary.private.test",
        "llm_approved_external_base_urls": "",
        "llm_extra_body_dict": lambda: None,
        "vision_api_key": "test-vision-key",
        "vision_base_url": "https://vision.private.test/compatible-mode/v1",
        "vision_model": "vision-model",
        "vision_approved_models": "vision-model,vision-model-v2",
        "vision_timeout_seconds": 2,
        "vision_max_pages": 1,
        "vision_trust_zone": "private",
        "vision_private_base_urls": "https://vision.private.test",
        "vision_approved_external_base_urls": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _transport_contract(settings, profile):
    lease = tools.capture_runtime_policy_lease(settings)
    if profile == "primary":
        snapshot = lease.primary
        api_key = lease.primary_api_key
    else:
        snapshot = lease.vision
        api_key = lease.vision_api_key
    payload = {
        "model": snapshot.model,
        "messages": [{"role": "user", "content": "bounded"}],
    }
    return {
        "snapshot": snapshot,
        "expected_api_key": api_key,
        "request_contract": provider._request_contract(profile, payload),
        "expected_runtime_policy_fingerprint": lease.fingerprint,
        "attempt_authorizer": lambda: True,
    }, json.dumps(payload, separators=(",", ":")).encode()


def _allow_attempt() -> bool:
    return True


def _sdk_headers(settings, profile):
    snapshot = (
        tools.capture_runtime_policy_lease(settings).primary
        if profile == "primary"
        else tools.capture_runtime_policy_lease(settings).vision
    )
    api_key = settings.llm_api_key if profile == "primary" else settings.vision_api_key
    return {
        "authorization": f"Bearer {api_key}",
        "accept-encoding": "identity",
        "connection": "keep-alive",
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": f"OpenAI/Python {provider._OPENAI_PACKAGE_VERSION}",
        "x-stainless-lang": "python",
        "x-stainless-package-version": provider._OPENAI_PACKAGE_VERSION,
        "x-stainless-os": provider._SDK_OS,
        "x-stainless-arch": provider._SDK_ARCH,
        "x-stainless-runtime": provider._SDK_RUNTIME,
        "x-stainless-runtime-version": provider._SDK_RUNTIME_VERSION,
        "x-stainless-async": "false",
        "openai-organization": "",
        "openai-project": "",
        "x-stainless-retry-count": "0",
        "x-stainless-read-timeout": str(float(snapshot.timeout_seconds)),
    }


@pytest.mark.parametrize("status_code", [307, 308])
def test_provider_http_client_never_follows_redirects(monkeypatch, status_code):
    requests: list[str] = []
    settings = _private_provider_settings(
        llm_base_url="https://approved.example/v1",
        llm_private_base_urls="https://approved.example",
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    contract_kwargs, body = _transport_contract(settings, "primary")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(status_code, headers={"location": "https://attacker.invalid/"})

    with provider._secure_http_client(
        profile="primary",
        max_request_bytes=1024,
        **contract_kwargs,
        transport=httpx.MockTransport(respond),
    ) as client:
        response = client.post(
            "https://approved.example/v1/chat/completions",
            content=body,
            headers=_sdk_headers(settings, "primary"),
        )

    assert response.status_code == status_code
    assert requests == ["https://approved.example/v1/chat/completions"]


def test_provider_http_client_ignores_environment_proxy(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib hook name
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"direct")

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    origin = f"http://127.0.0.1:{server.server_port}"
    settings = _private_provider_settings(
        environment="dev",
        agent_allow_loopback_http=True,
        llm_base_url=f"{origin}/v1",
        llm_private_base_urls=origin,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    contract_kwargs, body = _transport_contract(settings, "primary")
    try:
        with provider._secure_http_client(
            profile="primary",
            max_request_bytes=1024,
            **contract_kwargs,
        ) as client:
            response = client.post(
                f"{origin}/v1/chat/completions",
                content=body,
                headers=_sdk_headers(settings, "primary"),
                timeout=2,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status_code == 200
    assert response.text == "direct"


@pytest.mark.parametrize(
    ("profile", "allowed_url"),
    [
        ("primary", "https://primary.private.test/v1/chat/completions"),
        ("vision", "https://vision.private.test/compatible-mode/v1/chat/completions"),
    ],
)
def test_profile_transport_allows_only_live_exact_sdk_target(
    monkeypatch,
    profile,
    allowed_url,
):
    settings = _private_provider_settings()
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    contract_kwargs, body = _transport_contract(settings, profile)
    delegate_calls: list[str] = []

    def delegate(request: httpx.Request) -> httpx.Response:
        delegate_calls.append(str(request.url))
        return httpx.Response(200, request=request, json={"ok": True})

    if profile == "primary":
        denied_urls = [
            "https://other.private.test/v1/chat/completions",
            "https://primary.private.test/v11/chat/completions",
            "https://primary.private.test/v1/../admin/chat/completions",
            "https://primary.private.test/v1/chat/completions?next=attacker",
            "https://user@primary.private.test/v1/chat/completions",
            "https://primary.private.test/v1/%2e%2e/admin/chat/completions",
        ]
    else:
        denied_urls = [
            "https://other.private.test/compatible-mode/v1/chat/completions",
            "https://vision.private.test/compatible-mode/v11/chat/completions",
        ]

    with provider._secure_http_client(
        transport=httpx.MockTransport(delegate),
        profile=profile,
        max_request_bytes=1024,
        **contract_kwargs,
    ) as client:
        headers = _sdk_headers(settings, profile)
        assert client.post(allowed_url, content=body, headers=headers).status_code == 200
        for denied_url in denied_urls:
            with pytest.raises(provider.WireEgressDenied):
                client.post(denied_url, content=b"must-not-reach-delegate", headers=headers)
        if profile == "primary":
            settings.agent_model_context_egress_enabled = False
        else:
            settings.vision_trust_zone = "unknown"
        with pytest.raises(provider.WireEgressDenied):
            client.post(allowed_url, content=b"revoked-before-attempt", headers=headers)

    assert delegate_calls == [allowed_url]


def test_profile_transport_checks_policy_before_reading_denied_body(monkeypatch):
    settings = _private_provider_settings(llm_trust_zone="unknown")
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    contract_kwargs, _body = _transport_contract(settings, "primary")
    body_reads = 0
    delegate_calls = 0

    class SentinelStream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal body_reads
            body_reads += 1
            yield b"customer-secret"

    def delegate(_request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(200)

    guarded = provider._GuardedTransport(
        httpx.MockTransport(delegate),
        max_request_bytes=1024,
        **contract_kwargs,
    )
    request = httpx.Request(
        "POST",
        "https://primary.private.test/v1/chat/completions",
        stream=SentinelStream(),
    )

    with pytest.raises(provider.WireEgressDenied):
        guarded.handle_request(request)

    assert body_reads == 0
    assert delegate_calls == 0
    guarded.close()


def test_provider_transport_rejects_request_content_length_mismatch_before_delegate(monkeypatch):
    settings = _private_provider_settings()
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    contract_kwargs, body = _transport_contract(settings, "primary")
    delegate_calls = 0

    def delegate(_request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(200)

    guarded = provider._GuardedTransport(
        httpx.MockTransport(delegate),
        max_request_bytes=1024,
        **contract_kwargs,
    )
    headers = _sdk_headers(settings, "primary")
    headers["content-length"] = str(len(body) + 1)
    request = httpx.Request(
        "POST",
        "https://primary.private.test/v1/chat/completions",
        content=body,
        headers=headers,
    )

    with pytest.raises(provider.WireEgressDenied):
        guarded.handle_request(request)
    assert delegate_calls == 0
    guarded.close()


@pytest.mark.parametrize(
    ("retry_counts", "allowed_calls"),
    [
        ([1], 0),
        ([0, 0], 1),
        ([0, 1, 2], 2),
    ],
)
def test_provider_transport_enforces_own_strict_attempt_sequence(
    monkeypatch,
    retry_counts,
    allowed_calls,
):
    settings = _private_provider_settings(llm_max_retries=1)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    contract_kwargs, body = _transport_contract(settings, "primary")
    delegate_calls = 0

    def delegate(request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(500, request=request, content=b"retry")

    guarded = provider._GuardedTransport(
        httpx.MockTransport(delegate),
        max_request_bytes=1024,
        **contract_kwargs,
    )
    for index, retry_count in enumerate(retry_counts):
        headers = _sdk_headers(settings, "primary")
        headers["x-stainless-retry-count"] = str(retry_count)
        request = httpx.Request(
            "POST",
            "https://primary.private.test/v1/chat/completions",
            content=body,
            headers=headers,
        )
        if index < allowed_calls:
            response = guarded.handle_request(request)
            response.close()
        else:
            with pytest.raises(provider.WireEgressDenied):
                guarded.handle_request(request)
            break

    assert delegate_calls == allowed_calls
    guarded.close()


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("x-stainless-os", "HEADER-CANARY-OS-8472"),
        ("x-stainless-arch", "HEADER-CANARY-ARCH-8472"),
        ("x-stainless-runtime", "HEADER-CANARY-RUNTIME-8472"),
        ("x-stainless-runtime-version", "HEADER-CANARY-VERSION-8472"),
        ("connection", "HEADER-CANARY-CONNECTION-8472"),
    ],
)
def test_provider_transport_rejects_data_in_allowlisted_header_before_delegate(
    monkeypatch,
    header,
    value,
):
    settings = _private_provider_settings()
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    contract_kwargs, body = _transport_contract(settings, "primary")
    delegate_calls = 0

    def delegate(_request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(200)

    headers = _sdk_headers(settings, "primary")
    headers[header] = value
    request = httpx.Request(
        "POST",
        "https://primary.private.test/v1/chat/completions",
        content=body,
        headers=headers,
    )
    guarded = provider._GuardedTransport(
        httpx.MockTransport(delegate),
        max_request_bytes=1024,
        **contract_kwargs,
    )

    with pytest.raises(provider.WireEgressDenied):
        guarded.handle_request(request)
    assert delegate_calls == 0
    guarded.close()


@pytest.mark.parametrize(
    "response_headers",
    [
        {"content-encoding": "gzip"},
        {"content-length": "999999999"},
    ],
)
def test_provider_transport_rejects_encoded_or_declared_oversized_response_before_sdk_parse(
    monkeypatch,
    response_headers,
):
    settings = _private_provider_settings()
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    monkeypatch.setattr(limits, "PRIMARY_PROVIDER_RESPONSE_MAX_BYTES", 64)
    contract_kwargs, body = _transport_contract(settings, "primary")

    def delegate(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers=response_headers,
            stream=httpx.ByteStream(b"bounded"),
        )

    with provider._secure_http_client(
        profile="primary",
        max_request_bytes=1024,
        **contract_kwargs,
        transport=httpx.MockTransport(delegate),
    ) as client:
        with pytest.raises(provider.WireResponseBudgetExceeded):
            client.post(
                "https://primary.private.test/v1/chat/completions",
                content=body,
                headers=_sdk_headers(settings, "primary"),
            )


def test_provider_transport_caps_unknown_reasoning_wire_bytes_and_total_deadline(
    monkeypatch,
):
    settings = _private_provider_settings(llm_timeout_seconds=2)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    monkeypatch.setattr(limits, "PRIMARY_PROVIDER_RESPONSE_MAX_BYTES", 8)
    contract_kwargs, body = _transport_contract(settings, "primary")
    ticks = iter([0.0, 0.0, 0.0, 3.0])
    last_tick = 3.0

    def monotonic():
        nonlocal last_tick
        try:
            last_tick = next(ticks)
        except StopIteration:
            pass
        return last_tick

    monkeypatch.setattr(provider.time, "monotonic", monotonic)

    class ReasoningOnlyStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"r"
            yield b"r" * 8

    def delegate(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, stream=ReasoningOnlyStream())

    with provider._secure_http_client(
        profile="primary",
        max_request_bytes=1024,
        **contract_kwargs,
        transport=httpx.MockTransport(delegate),
    ) as client:
        with pytest.raises(provider.WireResponseBudgetExceeded):
            client.post(
                "https://primary.private.test/v1/chat/completions",
                content=body,
                headers=_sdk_headers(settings, "primary"),
            )


@pytest.mark.parametrize("revocation", ["trust_zone", "allowlist"])
def test_primary_sdk_retry_rechecks_live_policy_before_second_delegate_send(
    monkeypatch,
    revocation,
):
    settings = _private_provider_settings()
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    delegate_body_sizes: list[int] = []

    def delegate(request: httpx.Request) -> httpx.Response:
        delegate_body_sizes.append(len(request.content))
        # Simulate an administrator revoking the profile while the SDK handles a retryable 5xx.
        if revocation == "trust_zone":
            settings.llm_trust_zone = "unknown"
        else:
            settings.llm_private_base_urls = ""
        return httpx.Response(
            500,
            request=request,
            json={"error": {"message": "retryable", "type": "server_error"}},
        )

    original_secure_client = provider._secure_http_client

    def mock_secure_client(**kwargs):
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    monkeypatch.setattr(provider, "_secure_http_client", mock_secure_client)

    with pytest.raises(provider.ModelEgressDenied):
        provider.chat(
            [{"role": "user", "content": "customer context"}],
            _attempt_authorizer=_allow_attempt,
        )

    # The first attempt was sent; the revoked retry was stopped before the delegate saw any bytes.
    assert len(delegate_body_sizes) == 1
    assert delegate_body_sizes[0] > 0


def test_primary_sdk_retry_rechecks_principal_authorizer_before_second_delegate_send(
    monkeypatch,
):
    settings = _private_provider_settings()
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    delegate_calls = 0
    authorization_attempts = 0
    allowed = True

    def authorize_attempt() -> bool:
        nonlocal authorization_attempts
        authorization_attempts += 1
        return allowed

    def delegate(request: httpx.Request) -> httpx.Response:
        nonlocal allowed, delegate_calls
        delegate_calls += 1
        allowed = False
        return httpx.Response(
            500,
            request=request,
            json={"error": {"message": "retryable", "type": "server_error"}},
        )

    original_secure_client = provider._secure_http_client

    def mock_secure_client(**kwargs):
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    monkeypatch.setattr(provider, "_secure_http_client", mock_secure_client)

    with pytest.raises(provider.ModelEgressDenied):
        provider.chat(
            [{"role": "user", "content": "customer context"}],
            _attempt_authorizer=authorize_attempt,
        )

    assert delegate_calls == 1
    assert authorization_attempts == 2


@pytest.mark.parametrize("first_decision", ["false", "exception"])
def test_primary_sdk_retry_cannot_recover_after_authorizer_denial(
    monkeypatch,
    first_decision,
):
    settings = _private_provider_settings(llm_max_retries=1)
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    authorization_attempts = 0
    delegate_calls = 0

    def authorize_attempt() -> bool:
        nonlocal authorization_attempts
        authorization_attempts += 1
        if authorization_attempts == 1:
            if first_decision == "exception":
                raise RuntimeError("private authorization detail")
            return False
        return True

    def delegate(_request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(200)

    original_secure_client = provider._secure_http_client

    def mock_secure_client(**kwargs):
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    monkeypatch.setattr(provider, "_secure_http_client", mock_secure_client)

    with pytest.raises(
        provider.ModelEgressDenied,
        match="^primary model egress denied$",
    ):
        provider.chat(
            [{"role": "user", "content": "customer context"}],
            _attempt_authorizer=authorize_attempt,
        )

    # The SDK performs its configured retry and obtains a fresh decision, but the first denial
    # permanently poisoned this logical call before any delegate/network send.
    assert authorization_attempts == 2
    assert delegate_calls == 0


@pytest.mark.parametrize("entrypoint", ["chat", "chat_stream"])
def test_primary_provider_missing_attempt_authorizer_fails_closed_before_client(
    monkeypatch,
    entrypoint,
):
    monkeypatch.setattr(
        provider,
        "_client",
        lambda **_kwargs: pytest.fail("missing authorizer created a provider client"),
    )

    with pytest.raises(provider.ModelEgressDenied):
        result = getattr(provider, entrypoint)([{"role": "user", "content": "secret"}])
        if entrypoint == "chat_stream":
            list(result)


@pytest.mark.parametrize("drift", ["config", "model", "policy"])
def test_primary_sdk_retry_binds_same_origin_call_to_initial_policy_fingerprint(
    monkeypatch,
    drift,
):
    settings = _private_provider_settings()
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    delegate_body_sizes: list[int] = []

    def delegate(request: httpx.Request) -> httpx.Response:
        delegate_body_sizes.append(len(request.content))
        # Every mutation leaves the same exact HTTPS origin independently authorized. It must
        # still invalidate this logical call so an SDK retry cannot silently cross policy epochs.
        if drift == "config":
            settings.agent_external_file_egress_enabled = True
        elif drift == "model":
            settings.llm_model = "primary-model-v2"
        else:
            monkeypatch.setattr(tools, "RUNTIME_POLICY_VERSION", "future-policy")
        return httpx.Response(
            500,
            request=request,
            json={"error": {"message": "retryable", "type": "server_error"}},
        )

    original_secure_client = provider._secure_http_client

    def mock_secure_client(**kwargs):
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    monkeypatch.setattr(provider, "_secure_http_client", mock_secure_client)

    with pytest.raises(provider.ModelEgressDenied):
        provider.chat(
            [{"role": "user", "content": "customer context"}],
            _attempt_authorizer=_allow_attempt,
        )

    # Only the initial request body crossed the delegate. The still-authorized retry was denied
    # by the call-bound fingerprint before the guarded transport inspected or sent its body.
    assert len(delegate_body_sizes) == 1
    assert delegate_body_sizes[0] > 0


def test_primary_sdk_wire_budget_maps_to_stable_error_before_delegate(monkeypatch):
    settings = _private_provider_settings()
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    delegate_calls = 0
    original_secure_client = provider._secure_http_client

    def delegate(_request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(200)

    def mock_secure_client(**kwargs):
        kwargs["max_request_bytes"] = 64
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    monkeypatch.setattr(provider, "_secure_http_client", mock_secure_client)

    with pytest.raises(provider.ModelPayloadBudgetExceeded):
        provider.chat(
            [{"role": "user", "content": "bounded input"}],
            _attempt_authorizer=_allow_attempt,
        )

    assert delegate_calls == 0


def test_vision_sdk_maps_live_policy_revocation_before_delegate(tmp_path, monkeypatch):
    settings = _private_provider_settings()
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    delegate_calls = 0
    original_secure_client = provider._secure_http_client
    original_img_data_url = provider._img_data_url

    def delegate(_request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(200)

    def mock_secure_client(**kwargs):
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    def encode_then_revoke(path: Path) -> str:
        encoded = original_img_data_url(path)
        settings.vision_private_base_urls = ""
        return encoded

    monkeypatch.setattr(provider, "_secure_http_client", mock_secure_client)
    monkeypatch.setattr(provider, "_img_data_url", encode_then_revoke)
    image = tmp_path / "page.png"
    image.write_bytes(b"safe-test-image")

    with pytest.raises(provider.VisionEgressDenied):
        provider.vision_extract(
            [image], "extract", _attempt_authorizer=_allow_attempt
        )

    assert delegate_calls == 0


def test_vision_call_binds_initial_policy_fingerprint_before_file_projection(
    tmp_path,
    monkeypatch,
):
    settings = _private_provider_settings()
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    delegate_calls = 0
    original_secure_client = provider._secure_http_client
    original_img_data_url = provider._img_data_url

    def delegate(_request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(200)

    def mock_secure_client(**kwargs):
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    def encode_then_drift(path: Path) -> str:
        encoded = original_img_data_url(path)
        settings.vision_model = "vision-model-v2"
        return encoded

    monkeypatch.setattr(provider, "_secure_http_client", mock_secure_client)
    monkeypatch.setattr(provider, "_img_data_url", encode_then_drift)
    image = tmp_path / "page.png"
    image.write_bytes(b"safe-test-image")

    with pytest.raises(provider.VisionEgressDenied):
        provider.vision_extract(
            [image], "extract", _attempt_authorizer=_allow_attempt
        )

    assert delegate_calls == 0


def test_vision_projection_revocation_denies_first_delegate_send(tmp_path, monkeypatch):
    settings = _private_provider_settings()
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    delegate_calls = 0
    allowed = True
    original_secure_client = provider._secure_http_client
    original_img_data_url = provider._img_data_url

    def delegate(_request: httpx.Request) -> httpx.Response:
        nonlocal delegate_calls
        delegate_calls += 1
        return httpx.Response(200)

    def mock_secure_client(**kwargs):
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    def project_then_revoke(path: Path) -> str:
        nonlocal allowed
        projected = original_img_data_url(path)
        allowed = False
        return projected

    monkeypatch.setattr(provider, "_secure_http_client", mock_secure_client)
    monkeypatch.setattr(provider, "_img_data_url", project_then_revoke)
    image = tmp_path / "page.png"
    image.write_bytes(b"safe-test-image")

    with pytest.raises(provider.VisionEgressDenied):
        provider.vision_extract(
            [image],
            "extract",
            _attempt_authorizer=lambda: allowed,
        )

    assert delegate_calls == 0


def test_vision_missing_attempt_authorizer_denies_before_file_read(monkeypatch):
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: pytest.fail("missing authorizer read customer bytes"),
    )

    with pytest.raises(provider.VisionEgressDenied):
        provider.vision_extract([Path("customer.png")], "extract")


@pytest.mark.parametrize("entrypoint", ["chat", "chat_stream"])
def test_primary_provider_boundary_denies_before_client_creation(monkeypatch, entrypoint):
    denied = _private_provider_settings(llm_trust_zone="unknown")
    monkeypatch.setattr(tools, "get_settings", lambda: denied)
    monkeypatch.setattr(
        provider,
        "_client",
        lambda **_kwargs: pytest.fail("denied context created a provider client"),
    )

    with pytest.raises(provider.ModelEgressDenied):
        result = getattr(provider, entrypoint)(
            [{"role": "user", "content": "secret"}],
            _attempt_authorizer=_allow_attempt,
        )
        if entrypoint == "chat_stream":
            list(result)


@pytest.mark.parametrize("model", ["", "x" * 129, "bad\nmodel", "unlisted-model"])
def test_unapproved_primary_model_fails_before_client_creation(monkeypatch, model):
    settings = _private_provider_settings(llm_model=model)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    monkeypatch.setattr(
        provider,
        "_client",
        lambda **_kwargs: pytest.fail("unapproved model created a provider client"),
    )

    with pytest.raises(provider.ModelEgressDenied):
        provider.chat(
            [{"role": "user", "content": "secret"}],
            _attempt_authorizer=_allow_attempt,
        )


@pytest.mark.parametrize(
    "variable",
    [
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_CUSTOM_HEADERS",
        "OPENAI_LOG",
        "SSLKEYLOGFILE",
    ],
)
def test_provider_ambient_environment_fails_before_client_or_bytes(
    monkeypatch,
    caplog,
    variable,
):
    canary = f"AMBIENT-{variable}-CANARY-8472"
    monkeypatch.setenv(variable, canary)
    monkeypatch.setattr(
        provider,
        "_client",
        lambda **_kwargs: pytest.fail("ambient provider setting created a client"),
    )

    with pytest.raises(provider.ModelEgressDenied):
        provider.chat(
            [{"role": "user", "content": "secret"}],
            _attempt_authorizer=_allow_attempt,
        )
    assert canary not in caplog.text


def test_secure_ssl_context_ignores_sslkeylogfile_without_creating_file(tmp_path, monkeypatch):
    keylog = tmp_path / "provider.keys"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))

    context = provider._secure_ssl_context()

    assert context.verify_mode == provider.ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.keylog_filename is None
    assert not keylog.exists()


@pytest.mark.parametrize("entrypoint", ["chat", "chat_stream"])
def test_primary_provider_boundary_denies_oversized_context_before_client(
    monkeypatch,
    entrypoint,
):
    monkeypatch.setattr(tools, "primary_model_call_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tools, "primary_model_payload_allowed", lambda _messages: False)
    monkeypatch.setattr(
        provider,
        "_client",
        lambda **_kwargs: pytest.fail("oversized context created a provider client"),
    )

    with pytest.raises(provider.ModelPayloadBudgetExceeded):
        result = getattr(provider, entrypoint)(
            [{"role": "user", "content": "bounded"}],
            _attempt_authorizer=_allow_attempt,
        )
        if entrypoint == "chat_stream":
            list(result)


def test_vision_boundary_denies_before_file_read_or_client_creation(monkeypatch):
    import openai

    denied = _private_provider_settings(vision_trust_zone="unknown")
    monkeypatch.setattr(tools, "get_settings", lambda: denied)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: pytest.fail("denied Vision context read customer bytes"),
    )
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **_kwargs: pytest.fail("denied Vision context created a provider client"),
    )

    with pytest.raises(provider.VisionEgressDenied):
        provider.vision_extract(
            [Path("customer.png")], "extract", _attempt_authorizer=_allow_attempt
        )


def test_prod_unattested_private_vision_denies_before_render_read_or_client(monkeypatch):
    import openai

    denied = _private_provider_settings(
        environment="prod",
        agent_allow_unattested_private_for_development=True,
    )
    monkeypatch.setattr(tools, "get_settings", lambda: denied)
    monkeypatch.setattr(
        provider,
        "_render_pdf_pages",
        lambda *_args, **_kwargs: pytest.fail("unattested Vision rendered customer PDF"),
    )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: pytest.fail("unattested Vision read customer bytes"),
    )
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **_kwargs: pytest.fail("unattested Vision created a provider client"),
    )

    with pytest.raises(provider.VisionEgressDenied):
        provider.vision_extract(
            [Path("customer.pdf")], "extract", _attempt_authorizer=_allow_attempt
        )


@pytest.mark.parametrize("raises", [False, True])
def test_chat_closes_client_on_success_and_error(monkeypatch, raises):
    class Completions:
        @staticmethod
        def create(**_kwargs):
            if raises:
                raise RuntimeError("provider failed")
            message = SimpleNamespace(content="ok", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
        close_calls=0,
    )
    client.close = lambda: setattr(client, "close_calls", client.close_calls + 1)
    settings = SimpleNamespace(
        llm_model="model",
        llm_max_tokens=None,
        llm_extra_body_dict=lambda: None,
    )
    monkeypatch.setattr(provider, "_client", lambda **_kwargs: (client, settings))

    if raises:
        with pytest.raises(RuntimeError):
            provider.chat(
                [{"role": "user", "content": "hello"}],
                _attempt_authorizer=_allow_attempt,
            )
    else:
        assert provider.chat(
            [{"role": "user", "content": "hello"}],
            _attempt_authorizer=_allow_attempt,
        ).content == "ok"

    assert client.close_calls == 1


@pytest.mark.parametrize("raises", [False, True])
def test_chat_stream_closes_client_after_consumption_or_error(monkeypatch, raises):
    class Stream:
        def __iter__(self):
            if raises:
                raise RuntimeError("stream failed")
            return iter([])

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: Stream())
        ),
        close_calls=0,
    )
    client.close = lambda: setattr(client, "close_calls", client.close_calls + 1)
    settings = SimpleNamespace(
        llm_model="model",
        llm_max_tokens=None,
        llm_extra_body_dict=lambda: None,
    )
    monkeypatch.setattr(provider, "_client", lambda **_kwargs: (client, settings))

    if raises:
        with pytest.raises(RuntimeError):
            list(provider.chat_stream(
                [{"role": "user", "content": "hello"}],
                _attempt_authorizer=_allow_attempt,
            ))
    else:
        assert list(provider.chat_stream(
            [{"role": "user", "content": "hello"}],
            _attempt_authorizer=_allow_attempt,
        ))[-1][0] == "result"

    assert client.close_calls == 1


def test_chat_stream_consumes_but_never_emits_provider_reasoning(monkeypatch, caplog):
    canary = "PROVIDER-CHUNK-REASONING-CANARY-8472"
    contents = ["safe ", "<th", f"ink>{canary}", "</thi", "nk>answer"]
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            reasoning_content=canary if index == 0 else None,
            model_extra={"reasoning_content": canary} if index == 0 else {},
            content=content,
            tool_calls=[],
        ))])
        for index, content in enumerate(contents)
    ]
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: iter(chunks))
        ),
        close_calls=0,
    )
    client.close = lambda: setattr(client, "close_calls", client.close_calls + 1)
    settings = SimpleNamespace(
        llm_model="model",
        llm_max_tokens=None,
        llm_extra_body_dict=lambda: None,
    )
    monkeypatch.setattr(provider, "_client", lambda **_kwargs: (client, settings))

    events = list(provider.chat_stream(
        [{"role": "user", "content": "hello"}],
        _attempt_authorizer=_allow_attempt,
    ))

    assert "".join(payload for kind, payload in events if kind == "delta") == "safe answer"
    assert not any(kind == "reasoning" for kind, _payload in events)
    assert canary not in json.dumps(events, ensure_ascii=False, default=str)
    assert "<think>" not in json.dumps(events, ensure_ascii=False, default=str)
    assert canary not in caplog.text
    assert client.close_calls == 1


def test_bounded_tool_calls_rejects_count_and_utf8_argument_budgets():
    safe = provider.ToolCall(id="c", name="search_parts", arguments="{}")
    too_many = [safe] * (limits.MAX_TOOL_CALLS_PER_RESPONSE + 1)
    oversized = provider.ToolCall(
        id="c",
        name="write_report",
        arguments="界" * (limits.MAX_TOOL_ARGUMENT_BYTES_PER_CALL // 3 + 1),
    )

    with pytest.raises(provider.ToolCallBudgetExceeded):
        provider.bounded_tool_calls(too_many)
    with pytest.raises(provider.ToolCallBudgetExceeded):
        provider.bounded_tool_calls([oversized])


def test_stream_rejects_too_many_unique_tool_calls_and_closes_client(monkeypatch):
    tool_calls = [
        SimpleNamespace(
            index=index,
            id=f"c{index}",
            function=SimpleNamespace(name="search_parts", arguments="{}"),
        )
        for index in range(limits.MAX_TOOL_CALLS_PER_RESPONSE + 1)
    ]
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        content=None,
        tool_calls=tool_calls,
    ))])
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: iter([chunk]))
        ),
        close_calls=0,
    )
    client.close = lambda: setattr(client, "close_calls", client.close_calls + 1)
    settings = SimpleNamespace(
        llm_model="model",
        llm_max_tokens=None,
        llm_extra_body_dict=lambda: None,
    )
    monkeypatch.setattr(provider, "_client", lambda **_kwargs: (client, settings))

    with pytest.raises(provider.ToolCallBudgetExceeded):
        list(provider.chat_stream(
            [{"role": "user", "content": "hello"}],
            _attempt_authorizer=_allow_attempt,
        ))

    assert client.close_calls == 1


def test_stream_rejects_oversized_visible_content_before_emitting_it(monkeypatch):
    content = "x" * (limits.MAX_VISIBLE_RESPONSE_BYTES + 1)
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        content=content,
        tool_calls=[],
    ))])
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: iter([chunk]))
        ),
        close_calls=0,
    )
    client.close = lambda: setattr(client, "close_calls", client.close_calls + 1)
    settings = SimpleNamespace(
        llm_model="model",
        llm_max_tokens=None,
        llm_extra_body_dict=lambda: None,
    )
    monkeypatch.setattr(provider, "_client", lambda **_kwargs: (client, settings))

    with pytest.raises(provider.ModelOutputBudgetExceeded):
        list(provider.chat_stream(
            [{"role": "user", "content": "hello"}],
            _attempt_authorizer=_allow_attempt,
        ))

    assert client.close_calls == 1


def test_stream_assembles_many_tiny_tool_argument_chunks_once_and_exactly(monkeypatch):
    piece_count = 20_000

    def chunks():
        for index in range(piece_count):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(
                    index=0,
                    id="call-1" if index == 0 else None,
                    function=SimpleNamespace(
                        name="search_parts" if index == 0 else None,
                        arguments="x",
                    ),
                )],
            ))])

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: chunks())
        ),
        close_calls=0,
    )
    client.close = lambda: setattr(client, "close_calls", client.close_calls + 1)
    monkeypatch.setattr(provider, "_client", lambda **_kwargs: (client, None))

    events = list(provider.chat_stream(
        [{"role": "user", "content": "hello"}],
        _attempt_authorizer=_allow_attempt,
    ))
    result = next(payload for kind, payload in events if kind == "result")

    assert result.tool_calls[0].arguments == "x" * piece_count
    assert client.close_calls == 1


def test_vision_budgets_exact_data_url_json_before_client(tmp_path, monkeypatch):
    import openai

    vision_edges = tools._vision_egress()
    monkeypatch.setattr(
        tools,
        "_vision_egress",
        lambda: (replace(vision_edges[0], max_bytes=100), vision_edges[1]),
    )
    monkeypatch.setattr(tools, "vision_provider_call_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **_kwargs: pytest.fail("oversized serialized Vision request created a client"),
    )
    settings = SimpleNamespace(
        vision_api_key="configured",
        vision_base_url="https://vision.example/v1",
        vision_model="vision-model",
        vision_max_pages=1,
        vision_timeout_seconds=2,
    )
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    image = tmp_path / "page.png"
    image.write_bytes(b"small-image")

    with pytest.raises(provider.VisionPayloadBudgetExceeded):
        provider.vision_extract(
            [image],
            "hint-included-in-json",
            _attempt_authorizer=_allow_attempt,
        )


def test_pdf_render_rejects_oversized_page_before_bitmap_allocation(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    render_calls = 0
    render_dir = tmp_path / "isolated-render"

    class Page:
        @staticmethod
        def get_width():
            return 100_000

        @staticmethod
        def get_height():
            return 100_000

        @staticmethod
        def render(**_kwargs):
            nonlocal render_calls
            render_calls += 1
            raise AssertionError("oversized page reached bitmap allocation")

    class Document:
        def __len__(self):
            return 1

        def __getitem__(self, _index):
            return Page()

        @staticmethod
        def close():
            return None

    def make_render_dir(**_kwargs):
        render_dir.mkdir()
        return str(render_dir)

    monkeypatch.setattr(pdfium, "PdfDocument", lambda _path: Document())
    monkeypatch.setattr(provider.tempfile, "mkdtemp", make_render_dir)

    with pytest.raises(provider.VisionPayloadBudgetExceeded):
        provider._render_pdf_pages(tmp_path / "customer.pdf", 1)

    assert render_calls == 0
    assert not render_dir.exists()


def test_pdf_render_uses_unique_isolated_paths(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    class Image:
        @staticmethod
        def save(path):
            Path(path).write_bytes(b"png")

    class Bitmap:
        @staticmethod
        def to_pil():
            return Image()

    class Page:
        get_width = staticmethod(lambda: 100)
        get_height = staticmethod(lambda: 100)
        render = staticmethod(lambda **_kwargs: Bitmap())

    class Document:
        def __len__(self):
            return 1

        def __getitem__(self, _index):
            return Page()

        close = staticmethod(lambda: None)

    monkeypatch.setattr(pdfium, "PdfDocument", lambda _path: Document())
    first = provider._render_pdf_pages(tmp_path / "customer.pdf", 1)
    second = provider._render_pdf_pages(tmp_path / "customer.pdf", 1)

    assert first[0].parent != second[0].parent
    assert first[0].parent != tmp_path
    for path in [*first, *second]:
        parent = path.parent
        path.unlink()
        parent.rmdir()


def test_vision_sdk_wire_guard_rejects_before_transport_send(tmp_path, monkeypatch):
    delegate_calls: list[int] = []
    original_secure_client = provider._secure_http_client

    def delegate(request: httpx.Request) -> httpx.Response:
        delegate_calls.append(len(request.content))
        return httpx.Response(200, json={
            "id": "response",
            "object": "chat.completion",
            "created": 1,
            "model": "vision-model",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "unreachable"},
            }],
        })

    def guarded_client(**kwargs):
        kwargs["max_request_bytes"] = 256
        return original_secure_client(
            transport=httpx.MockTransport(delegate),
            **kwargs,
        )

    monkeypatch.setattr(provider, "_secure_http_client", guarded_client)
    settings = _private_provider_settings(
        vision_api_key="configured",
        vision_base_url="https://vision.example/v1",
        vision_private_base_urls="https://vision.example",
    )
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    image = tmp_path / "page.png"
    image.write_bytes(b"small-image-that-expands-inside-a-data-url")

    with pytest.raises(provider.VisionPayloadBudgetExceeded):
        provider.vision_extract(
            [image],
            "wire request includes this hint and JSON envelope",
            _attempt_authorizer=_allow_attempt,
        )

    assert delegate_calls == []


def test_vision_caps_ocr_at_provider_seam_and_closes_client(tmp_path, monkeypatch):
    import openai

    vision_edges = tools._vision_egress()
    monkeypatch.setattr(
        tools,
        "_vision_egress",
        lambda: (vision_edges[0], replace(vision_edges[1], max_bytes=16)),
    )
    monkeypatch.setattr(tools, "vision_provider_call_allowed", lambda *_args, **_kwargs: True)
    message = SimpleNamespace(content="x" * 17)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(
                choices=[SimpleNamespace(message=message)]
            )
        )),
        close_calls=0,
    )
    client.close = lambda: setattr(client, "close_calls", client.close_calls + 1)
    monkeypatch.setattr(openai, "OpenAI", lambda **_kwargs: client)
    settings = SimpleNamespace(
        vision_api_key="configured",
        vision_base_url="https://vision.example/v1",
        vision_model="vision-model",
        vision_max_pages=1,
        vision_timeout_seconds=2,
    )
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    image = tmp_path / "page.png"
    image.write_bytes(b"safe-test-image")

    with pytest.raises(provider.VisionPayloadBudgetExceeded):
        provider.vision_extract(
            [image], "extract", _attempt_authorizer=_allow_attempt
        )

    assert client.close_calls == 1


@pytest.mark.parametrize("raises", [False, True])
def test_vision_closes_client_on_success_and_error(tmp_path, monkeypatch, raises):
    import openai

    class Completions:
        @staticmethod
        def create(**_kwargs):
            if raises:
                raise RuntimeError("vision failed")
            message = SimpleNamespace(content="recognized")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
        close_calls=0,
    )
    client.close = lambda: setattr(client, "close_calls", client.close_calls + 1)
    settings = SimpleNamespace(
        vision_api_key="not-a-real-key",
        vision_base_url="https://vision.example/v1",
        vision_model="vision-model",
        vision_max_pages=1,
        vision_timeout_seconds=2,
    )
    monkeypatch.setattr(provider, "get_settings", lambda: settings)
    monkeypatch.setattr(openai, "OpenAI", lambda **_kwargs: client)
    image = tmp_path / "page.png"
    image.write_bytes(b"safe-test-image-placeholder")

    if raises:
        with pytest.raises(RuntimeError):
            provider.vision_extract(
                [image], "extract", _attempt_authorizer=_allow_attempt
            )
    else:
        assert provider.vision_extract(
            [image], "extract", _attempt_authorizer=_allow_attempt
        ) == "recognized"

    assert client.close_calls == 1
