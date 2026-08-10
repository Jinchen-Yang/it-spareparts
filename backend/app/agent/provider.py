"""LLM 客户端抽象 —— "可切换"的落点。

默认 openai_compatible：用 openai SDK 对接一切 OpenAI 兼容端点
（DeepSeek / 通义 Qwen 兼容模式 / Kimi / GLM …），换厂商只改 .env 的
LLM_BASE_URL + LLM_MODEL + LLM_API_KEY，业务代码零改动。

DeepSeek 上下文缓存为磁盘级自动命中：把固定的 system + tools 放在消息最前
即可享缓存折扣，无需显式 cache_control。将来接 Anthropic：在 chat() 加
provider 分支（anthropic SDK + cache_control），接口保持不变。
"""
import base64
import hashlib
import importlib.metadata
import json
import logging
import math
import mimetypes
import os
import platform
import re
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import httpx

from app.agent import limits
from app.config import get_settings

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_OPENAI_PACKAGE_VERSION = importlib.metadata.version("openai")
_SDK_LOGGERS = ("openai", "httpx", "httpcore")
_SDK_OS = platform.system()
_SDK_ARCH = {
    "x86_64": "x64",
    "AMD64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}.get(platform.machine(), platform.machine())
_SDK_RUNTIME = platform.python_implementation()
_SDK_RUNTIME_VERSION = platform.python_version()


def _pin_provider_sdk_logging() -> None:
    """SDK request metadata is never an application telemetry surface."""
    for name in _SDK_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.propagate = False
        logger.disabled = True


_pin_provider_sdk_logging()

# Provider-controlled tool-call metadata is untrusted input. These bounds are deliberately
# independent from per-capability validators: they stop response assembly before a malicious
# provider can accumulate an unbounded arguments string or fan one round out into many reads.
MAX_TOOL_CALLS_PER_RESPONSE = limits.MAX_TOOL_CALLS_PER_RESPONSE
MAX_TOOL_ARGUMENT_BYTES_PER_CALL = limits.MAX_TOOL_ARGUMENT_BYTES_PER_CALL
MAX_TOOL_ARGUMENT_BYTES_PER_RESPONSE = limits.MAX_TOOL_ARGUMENT_BYTES_PER_RESPONSE
MAX_TOOL_CALL_ID_CHARS = limits.MAX_TOOL_CALL_ID_CHARS
MAX_TOOL_NAME_CHARS = limits.MAX_TOOL_NAME_CHARS
MAX_VISIBLE_RESPONSE_BYTES = limits.MAX_VISIBLE_RESPONSE_BYTES


def _suffix_prefix_length(value: str, token: str) -> int:
    lowered = value.lower()
    for size in range(min(len(value), len(token) - 1), 0, -1):
        if lowered.endswith(token[:size]):
            return size
    return 0


class ReasoningContentFilter:
    """Incrementally remove exact `<think>...</think>` blocks across chunk boundaries.

    While inside a block, content is discarded immediately except for the shortest suffix needed
    to recognize a split closing tag. An unclosed block is discarded at EOF. This is intentionally
    a narrow accepted-provider contract; untagged chain-of-thought cannot be distinguished from a
    user-facing answer and such provider profiles must not be admitted.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    def feed(self, value: object) -> str:
        if not isinstance(value, str) or not value:
            return ""
        self._buffer += value
        visible: list[str] = []
        while self._buffer:
            lowered = self._buffer.lower()
            if self._in_think:
                close_at = lowered.find(_THINK_CLOSE)
                if close_at >= 0:
                    self._buffer = self._buffer[close_at + len(_THINK_CLOSE):]
                    self._in_think = False
                    continue
                keep = _suffix_prefix_length(self._buffer, _THINK_CLOSE)
                self._buffer = self._buffer[-keep:] if keep else ""
                break

            open_at = lowered.find(_THINK_OPEN)
            close_at = lowered.find(_THINK_CLOSE)
            if close_at >= 0 and (open_at < 0 or close_at < open_at):
                # A provider stream that starts mid-reasoning is not trusted as visible text.
                self._buffer = self._buffer[close_at + len(_THINK_CLOSE):]
                continue
            if open_at >= 0:
                visible.append(self._buffer[:open_at])
                self._buffer = self._buffer[open_at + len(_THINK_OPEN):]
                self._in_think = True
                continue

            keep = max(
                _suffix_prefix_length(self._buffer, _THINK_OPEN),
                _suffix_prefix_length(self._buffer, _THINK_CLOSE),
            )
            safe_end = len(self._buffer) - keep
            visible.append(self._buffer[:safe_end])
            self._buffer = self._buffer[safe_end:]
            break
        return "".join(visible)

    def finish(self) -> str:
        tail = "" if self._in_think else self._buffer
        self._buffer = ""
        self._in_think = False
        return tail


def sanitize_model_text(text: object) -> str:
    sanitizer = ReasoningContentFilter()
    return f"{sanitizer.feed(text)}{sanitizer.finish()}"


def _strip_think(text: str) -> str:
    """去掉推理模型的 <think>…</think> 块；无该块则原样返回。"""
    return sanitize_model_text(text).strip()


class LLMNotConfigured(Exception):
    """未配置 LLM_API_KEY。"""


class VisionNotConfigured(Exception):
    """未配置 VISION_API_KEY（图片/扫描件识别需要）。"""


class ModelEgressDenied(Exception):
    """Live primary-model destination policy denied this call before HTTP egress."""


class VisionEgressDenied(Exception):
    """Live Vision destination policy denied customer-file egress before file encoding."""


class ModelPayloadBudgetExceeded(Exception):
    """Conversation projection exceeded its declared edge byte ceiling."""


class VisionPayloadBudgetExceeded(Exception):
    """Vision binary projection exceeded its declared edge byte ceiling."""


class ModelOutputBudgetExceeded(Exception):
    """Provider-visible answer exceeded the fixed per-response byte ceiling."""


class ToolCallBudgetExceeded(Exception):
    """Provider tool-call metadata exceeded a fixed, value-free safety budget."""


class WirePayloadBudgetExceeded(Exception):
    """HTTP request body exceeded a fixed pre-send budget; carries no request content."""


class WireEgressDenied(Exception):
    """One actual HTTP attempt failed its live destination policy check."""


class WireResponseBudgetExceeded(Exception):
    """Provider response exceeded a fixed wire-byte/chunk/deadline budget."""


@dataclass(frozen=True, slots=True)
class ProviderRequestContract:
    """Value-free fingerprint of one exact SDK request projection."""

    profile: str
    model: str
    messages_fingerprint: str
    tools_fingerprint: str | None
    stream: bool | None
    max_tokens: int | None
    options_json: str
    keys: frozenset[str]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _json_fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _request_contract(profile: str, payload: dict) -> ProviderRequestContract:
    """Build the exact expected body contract without retaining messages/tool schemas."""
    if profile not in {"primary", "vision"} or not isinstance(payload, dict):
        raise ValueError("invalid provider request contract")
    model = payload.get("model")
    messages = payload.get("messages")
    if not isinstance(model, str) or not isinstance(messages, list):
        raise ValueError("invalid provider request contract")
    tools_present = "tools" in payload
    tools_value = payload.get("tools")
    tools_fingerprint = (
        _json_fingerprint(tools_value) if tools_value is not None else None
    )
    stream = payload.get("stream") if "stream" in payload else None
    max_tokens = payload.get("max_tokens") if "max_tokens" in payload else None
    if stream is not None and not isinstance(stream, bool):
        raise ValueError("invalid provider request contract")
    if max_tokens is not None and (
        not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0
    ):
        raise ValueError("invalid provider request contract")
    option_keys = {"thinking", "enable_thinking"} & payload.keys()
    options = {key: payload[key] for key in sorted(option_keys)}
    allowed_keys = {"model", "messages"}
    if tools_present:
        allowed_keys.add("tools")
    if stream is not None:
        allowed_keys.add("stream")
    if max_tokens is not None:
        allowed_keys.add("max_tokens")
    allowed_keys.update(option_keys)
    if set(payload) != allowed_keys:
        raise ValueError("invalid provider request contract")
    return ProviderRequestContract(
        profile=profile,
        model=model,
        messages_fingerprint=_json_fingerprint(messages),
        tools_fingerprint=tools_fingerprint,
        stream=stream,
        max_tokens=max_tokens,
        options_json=_canonical_json(options).decode("ascii"),
        keys=frozenset(allowed_keys),
    )


def _load_json_object_without_duplicates(content: bytes) -> dict | None:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _wire_payload_matches(content: bytes, contract: ProviderRequestContract) -> bool:
    value = _load_json_object_without_duplicates(content)
    if value is None or set(value) != set(contract.keys):
        return False
    try:
        observed = _request_contract(contract.profile, value)
    except (TypeError, ValueError):
        return False
    return observed == contract


_CONTROLLED_REQUEST_HEADERS = frozenset({
    "host",
    "accept-encoding",
    "connection",
    "authorization",
    "accept",
    "content-type",
    "user-agent",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-async",
    "openai-organization",
    "openai-project",
    "x-stainless-retry-count",
    "x-stainless-read-timeout",
    "content-length",
})


def _controlled_headers_allowed(
    request: httpx.Request,
    *,
    snapshot,
    expected_api_key: str,
) -> bool:
    pairs = request.headers.multi_items()
    names = [name.lower() for name, _value in pairs]
    if len(names) != len(set(names)) or not set(names).issubset(_CONTROLLED_REQUEST_HEADERS):
        return False
    headers = request.headers
    if (
        headers.get("authorization") != f"Bearer {expected_api_key}"
        or headers.get("accept") != "application/json"
        or headers.get("content-type") != "application/json"
        or headers.get("openai-organization", "") != ""
        or headers.get("openai-project", "") != ""
        or headers.get("accept-encoding") != "identity"
        or headers.get("x-stainless-lang") != "python"
        or headers.get("x-stainless-package-version") != _OPENAI_PACKAGE_VERSION
        or headers.get("x-stainless-os") != _SDK_OS
        or headers.get("x-stainless-arch") != _SDK_ARCH
        or headers.get("x-stainless-runtime") != _SDK_RUNTIME
        or headers.get("x-stainless-runtime-version") != _SDK_RUNTIME_VERSION
        or headers.get("user-agent") != f"OpenAI/Python {_OPENAI_PACKAGE_VERSION}"
        or headers.get("x-stainless-async") != "false"
        or headers.get("connection") != "keep-alive"
    ):
        return False
    try:
        expected_host = urlsplit(snapshot.origin).netloc
        retry_count = int(headers.get("x-stainless-retry-count", "-1"))
        read_timeout = float(headers.get("x-stainless-read-timeout", "nan"))
        content_length = int(headers.get("content-length", "-1"))
    except (TypeError, ValueError):
        return False
    if (
        headers.get("host") != expected_host
        or not 0 <= retry_count <= snapshot.max_retries
        or headers.get("x-stainless-retry-count") != str(retry_count)
        or read_timeout != snapshot.timeout_seconds
        or headers.get("x-stainless-read-timeout") != str(float(snapshot.timeout_seconds))
        or content_length < 0
    ):
        return False
    try:
        header_bytes = sum(
            len(name.encode("ascii", "strict")) + len(value.encode("ascii", "strict")) + 4
            for name, value in pairs
        )
    except UnicodeEncodeError:
        return False
    if header_bytes > limits.MAX_PROVIDER_REQUEST_HEADER_BYTES:
        return False
    for name, value in pairs:
        if (
            len(name) > 64
            or len(value) > 512
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        ):
            return False
    return True


def _secure_ssl_context() -> ssl.SSLContext:
    """Build a CA-verified TLS context without consulting SSLKEYLOGFILE."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=certifi.where())
    if context.keylog_filename is not None:
        raise RuntimeError("provider TLS key logging must remain disabled")
    return context


class _BoundedResponseStream(httpx.SyncByteStream):
    """Count every raw response byte/chunk before SDK parsing or decompression.

    ``Accept-Encoding: identity`` and rejection of any encoded response make the raw-byte limit
    the decompressed-byte limit too.  The monotonic deadline stops a provider from holding a
    worker forever with a reasoning-only, one-byte-at-a-time stream.  A blocking read can overrun
    the deadline by at most the independently fixed SDK read timeout.
    """

    def __init__(
        self,
        stream: httpx.SyncByteStream,
        *,
        max_bytes: int,
        max_chunks: int,
        deadline: float,
        consume_bytes=None,
    ) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._max_chunks = max_chunks
        self._deadline = deadline
        self._consume_bytes = consume_bytes
        self._bytes = 0
        self._chunks = 0

    def __iter__(self):
        iterator = iter(self._stream)
        while True:
            if time.monotonic() > self._deadline:
                raise WireResponseBudgetExceeded("provider response budget exceeded")
            try:
                chunk = next(iterator)
            except StopIteration:
                return
            self._chunks += 1
            self._bytes += len(chunk)
            if (
                time.monotonic() > self._deadline
                or self._chunks > self._max_chunks
                or self._bytes > self._max_bytes
                or (
                    self._consume_bytes is not None
                    and not self._consume_bytes(len(chunk))
                )
            ):
                raise WireResponseBudgetExceeded("provider response budget exceeded")
            yield chunk

    def close(self) -> None:
        self._stream.close()


class _GuardedTransport(httpx.BaseTransport):
    """Revalidate destination policy and payload size before every delegate send.

    OpenAI-compatible SDK retries pass through ``handle_request`` again. Policy is deliberately
    checked before this transport touches ``request.content``; the SDK may already have serialized
    its request, but a revoked attempt reaches neither this transport's body inspection nor the
    delegate/network transport.
    """

    def __init__(
        self,
        delegate: httpx.BaseTransport,
        *,
        snapshot,
        expected_api_key: str,
        request_contract: ProviderRequestContract,
        max_request_bytes: int,
        expected_runtime_policy_fingerprint: str,
        attempt_authorizer,
    ) -> None:
        if (
            not isinstance(expected_runtime_policy_fingerprint, str)
            or len(expected_runtime_policy_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in expected_runtime_policy_fingerprint)
        ):
            raise ValueError("invalid runtime policy fingerprint")
        from app.agent import tools

        if (
            type(snapshot) is not tools.ProviderProfileSnapshot
            or request_contract.profile
            != (
                "primary"
                if snapshot.destination is tools.EgressDestination.PRIMARY_MODEL
                else "vision"
            )
            or not isinstance(expected_api_key, str)
            or not expected_api_key
        ):
            raise ValueError("invalid provider profile snapshot")
        self._delegate = delegate
        self._snapshot = snapshot
        self._profile = request_contract.profile
        self._expected_api_key = expected_api_key
        self._request_contract = request_contract
        self._attempt_authorizer = attempt_authorizer
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = (
            limits.PRIMARY_PROVIDER_RESPONSE_MAX_BYTES
            if self._profile == "primary"
            else limits.VISION_PROVIDER_RESPONSE_MAX_BYTES
        )
        self._expected_runtime_policy_fingerprint = expected_runtime_policy_fingerprint
        self._attempt_lock = threading.Lock()
        # One authorization denial permanently poisons this logical provider call.  The SDK may
        # retry transport exceptions; a later principal refresh must never resurrect a call whose
        # earlier wire attempt was denied.  We still invoke the authorizer on every SDK attempt so
        # the caller can observe/audit each fresh decision without allowing network recovery.
        self._authorization_denied = False
        self._attempts = 0
        self._cumulative_request_bytes = 0
        self._cumulative_response_bytes = 0

    def _consume_response_bytes(self, size: int) -> bool:
        with self._attempt_lock:
            self._cumulative_response_bytes += size
            return self._cumulative_response_bytes <= (
                self._max_response_bytes * (self._snapshot.max_retries + 1)
            )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Principal/capability authorization is intentionally evaluated at the actual wire seam,
        # including every SDK retry. It runs before this transport touches request.content; the SDK
        # may already have serialized the body, but a revoked attempt reaches zero delegate sends.
        try:
            attempt_allowed = (
                callable(self._attempt_authorizer)
                and self._attempt_authorizer() is True
            )
        except Exception:  # noqa: BLE001 -- authorization failures always deny without values
            attempt_allowed = False
        with self._attempt_lock:
            if not attempt_allowed:
                self._authorization_denied = True
            authorization_denied = self._authorization_denied
        if authorization_denied:
            raise WireEgressDenied("provider request egress denied")
        # Lazy import avoids provider <-> capability-registry import cycles. This call resolves the
        # current Settings object on each retry and validates the SDK's actual URL, not merely the
        # base URL observed before client creation.
        from app.agent import tools

        selected = tools.get_settings()
        live_api_key_field = "llm_api_key" if self._profile == "primary" else "vision_api_key"
        if (
            tools.runtime_policy_fingerprint(selected)
            != self._expected_runtime_policy_fingerprint
            or not self._snapshot.admitted
            or str(getattr(selected, live_api_key_field, "")) != self._expected_api_key
            or not tools.provider_http_request_allowed(
                self._profile,
                str(request.url),
                settings=selected,
            )
            or request.method != "POST"
            or str(request.url) != f"{self._snapshot.base_url}/chat/completions"
            or not _controlled_headers_allowed(
                request,
                snapshot=self._snapshot,
                expected_api_key=self._expected_api_key,
            )
        ):
            raise WireEgressDenied("provider request egress denied")
        try:
            retry_header = int(request.headers.get("x-stainless-retry-count", "-1"))
        except (TypeError, ValueError):
            raise WireEgressDenied("provider request egress denied") from None
        with self._attempt_lock:
            expected_attempt = self._attempts
            if (
                expected_attempt > self._snapshot.max_retries
                or retry_header != expected_attempt
            ):
                raise WireEgressDenied("provider request egress denied")
            self._attempts += 1
        try:
            content = request.content
        except httpx.RequestNotRead:
            chunks: list[bytes] = []
            total = 0
            for chunk in request.stream:
                total += len(chunk)
                if total > self._max_request_bytes:
                    raise WirePayloadBudgetExceeded(
                        "provider request payload budget exceeded"
                    )
                chunks.append(chunk)
            content = b"".join(chunks)
            request.stream = httpx.ByteStream(content)
        if len(content) > self._max_request_bytes:
            raise WirePayloadBudgetExceeded("provider request payload budget exceeded")
        try:
            declared_length = int(request.headers.get("content-length", "-1"))
        except (TypeError, ValueError):
            declared_length = -1
        if declared_length != len(content):
            raise WireEgressDenied("provider request contract denied")
        with self._attempt_lock:
            self._cumulative_request_bytes += len(content)
            if self._cumulative_request_bytes > (
                self._max_request_bytes * (self._snapshot.max_retries + 1)
            ):
                raise WirePayloadBudgetExceeded("provider request payload budget exceeded")
        if not _wire_payload_matches(content, self._request_contract):
            raise WireEgressDenied("provider request contract denied")
        response_started_at = time.monotonic()
        response = self._delegate.handle_request(request)
        deadline = response_started_at + self._snapshot.timeout_seconds
        try:
            encodings = response.headers.get_list("content-encoding")
            lengths = response.headers.get_list("content-length")
            if encodings and encodings != ["identity"]:
                raise WireResponseBudgetExceeded("provider response budget exceeded")
            if len(lengths) > 1:
                raise WireResponseBudgetExceeded("provider response budget exceeded")
            if lengths:
                content_length = int(lengths[0])
                if content_length < 0 or content_length > self._max_response_bytes:
                    raise WireResponseBudgetExceeded("provider response budget exceeded")
            if time.monotonic() > deadline:
                raise WireResponseBudgetExceeded("provider response budget exceeded")
            response.stream = _BoundedResponseStream(
                response.stream,
                max_bytes=self._max_response_bytes,
                max_chunks=limits.MAX_PROVIDER_RESPONSE_CHUNKS,
                deadline=deadline,
                consume_bytes=self._consume_response_bytes,
            )
            return response
        except (TypeError, ValueError, WireResponseBudgetExceeded):
            response.close()
            raise WireResponseBudgetExceeded("provider response budget exceeded") from None

    def close(self) -> None:
        self._delegate.close()


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 字符串（按 OpenAI 约定）


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


def bounded_tool_calls(value: object) -> list[ToolCall]:
    """Validate and copy at most one bounded provider response worth of tool calls.

    The exception deliberately carries no provider value so API/log seams cannot accidentally
    disclose raw arguments. Runtime calls this again for alternate/future provider adapters.
    """
    if not isinstance(value, (list, tuple)):
        raise ToolCallBudgetExceeded("tool call budget exceeded")
    calls: list[ToolCall] = []
    total_argument_bytes = 0
    for call in value:
        if len(calls) >= MAX_TOOL_CALLS_PER_RESPONSE or not isinstance(call, ToolCall):
            raise ToolCallBudgetExceeded("tool call budget exceeded")
        if (
            not isinstance(call.id, str)
            or len(call.id) > MAX_TOOL_CALL_ID_CHARS
            or not isinstance(call.name, str)
            or len(call.name) > MAX_TOOL_NAME_CHARS
            or not isinstance(call.arguments, str)
        ):
            raise ToolCallBudgetExceeded("tool call budget exceeded")
        argument_bytes = len(call.arguments.encode("utf-8"))
        if argument_bytes > MAX_TOOL_ARGUMENT_BYTES_PER_CALL:
            raise ToolCallBudgetExceeded("tool call budget exceeded")
        total_argument_bytes += argument_bytes
        if total_argument_bytes > MAX_TOOL_ARGUMENT_BYTES_PER_RESPONSE:
            raise ToolCallBudgetExceeded("tool call budget exceeded")
        calls.append(call)
    return calls


def _message_tool_calls(value: object) -> list[ToolCall]:
    """Convert an SDK response without first copying an unbounded provider list."""
    if value is None:
        return []
    try:
        iterator = iter(value)
    except TypeError as exc:
        raise ToolCallBudgetExceeded("tool call budget exceeded") from exc
    calls: list[ToolCall] = []
    for raw in iterator:
        if len(calls) >= MAX_TOOL_CALLS_PER_RESPONSE:
            raise ToolCallBudgetExceeded("tool call budget exceeded")
        try:
            call = ToolCall(
                id=raw.id,
                name=raw.function.name,
                arguments=raw.function.arguments,
            )
        except (AttributeError, TypeError) as exc:
            raise ToolCallBudgetExceeded("tool call budget exceeded") from exc
        calls.append(call)
    return bounded_tool_calls(calls)


def is_configured() -> bool:
    return bool(get_settings().llm_api_key)


def _secure_http_client(
    *,
    profile: str,
    snapshot,
    expected_api_key: str,
    request_contract: ProviderRequestContract,
    max_request_bytes: int,
    expected_runtime_policy_fingerprint: str,
    attempt_authorizer,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Build the only provider HTTP transport: no redirects and no ambient proxy inheritance."""
    if profile not in {"primary", "vision"}:
        raise ValueError("unknown provider profile")
    if (
        not isinstance(max_request_bytes, int)
        or isinstance(max_request_bytes, bool)
        or max_request_bytes <= 0
    ):
        raise ValueError("max_request_bytes must be a positive integer")
    delegate = transport or httpx.HTTPTransport(
        verify=_secure_ssl_context(),
        trust_env=False,
    )
    selected_transport = _GuardedTransport(
        delegate,
        snapshot=snapshot,
        expected_api_key=expected_api_key,
        request_contract=request_contract,
        max_request_bytes=max_request_bytes,
        expected_runtime_policy_fingerprint=expected_runtime_policy_fingerprint,
        attempt_authorizer=attempt_authorizer,
    )
    return httpx.Client(
        follow_redirects=False,
        trust_env=False,
        transport=selected_transport,
        headers={"Accept-Encoding": "identity"},
    )


def _exception_chain_contains(exc: BaseException, expected: type[BaseException]) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, expected):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _raise_mapped_wire_error(exc: BaseException, *, profile: str) -> None:
    """Project internal transport markers into stable, value-free domain exceptions."""
    if _exception_chain_contains(exc, WireEgressDenied):
        if profile == "vision":
            raise VisionEgressDenied("vision provider egress denied") from None
        raise ModelEgressDenied("primary model egress denied") from None
    if _exception_chain_contains(exc, WirePayloadBudgetExceeded):
        if profile == "vision":
            raise VisionPayloadBudgetExceeded("vision payload budget exceeded") from None
        raise ModelPayloadBudgetExceeded("primary model payload budget exceeded") from None
    if _exception_chain_contains(exc, WireResponseBudgetExceeded):
        if profile == "vision":
            raise VisionPayloadBudgetExceeded("vision payload budget exceeded") from None
        raise ModelOutputBudgetExceeded("model output budget exceeded") from None


def _coerce_runtime_policy_lease(value: object = None):
    from app.agent import tools

    return tools.capture_runtime_policy_lease() if value is None else value


def _require_primary_model_egress(messages: object, policy_lease: object):
    # Lazy import avoids provider <-> capability-registry import cycles while keeping the final
    # enforcement directly at the network boundary. Runtime/API checks remain UX + early release.
    from app.agent import tools

    if (
        type(policy_lease) is not tools.RuntimePolicyLease
        or not policy_lease.primary.admitted
        or not tools.runtime_policy_lease_current(policy_lease)
    ):
        raise ModelEgressDenied("primary model egress denied")
    if not tools.primary_model_payload_allowed(messages):
        raise ModelPayloadBudgetExceeded("primary model payload budget exceeded")
    return policy_lease


def _require_vision_egress(policy_lease: object):
    from app.agent import tools

    if (
        type(policy_lease) is not tools.RuntimePolicyLease
        or not policy_lease.vision.admitted
        or not tools.runtime_policy_lease_current(policy_lease)
    ):
        raise VisionEgressDenied("vision provider egress denied")
    return policy_lease


def _require_vision_payload_budget(paths: list[Path]) -> None:
    from app.agent import tools

    total = 0
    try:
        for path in paths:
            total += path.stat().st_size
            if not tools.vision_provider_payload_allowed(total):
                raise VisionPayloadBudgetExceeded("vision payload budget exceeded")
    except OSError as exc:
        raise VisionPayloadBudgetExceeded("vision payload budget exceeded") from exc


def _require_vision_projection_budget(payload: object) -> None:
    from app.agent import tools

    if not tools.vision_provider_projection_allowed(payload):
        raise VisionPayloadBudgetExceeded("vision payload budget exceeded")


def _client(*, policy_lease, request_contract: ProviderRequestContract, attempt_authorizer):
    snapshot = policy_lease.primary
    api_key = policy_lease.primary_api_key
    if not api_key:
        raise LLMNotConfigured("未配置 LLM_API_KEY")
    if snapshot.adapter != "openai_compatible":
        raise ModelEgressDenied("primary model egress denied")
    from openai import OpenAI  # 延迟导入：未配置 LLM 时后端其余功能不依赖该包

    _pin_provider_sdk_logging()
    return OpenAI(
        api_key=api_key,
        admin_api_key="",
        organization="",
        project="",
        webhook_secret="",
        base_url=snapshot.base_url,
        timeout=snapshot.timeout_seconds,
        max_retries=snapshot.max_retries,
        default_headers={},
        default_query={},
        http_client=_secure_http_client(
            profile="primary",
            snapshot=snapshot,
            expected_api_key=api_key,
            request_contract=request_contract,
            max_request_bytes=limits.CONVERSATION_CONTEXT_MAX_BYTES,
            expected_runtime_policy_fingerprint=policy_lease.fingerprint,
            attempt_authorizer=attempt_authorizer,
        ),
    ), snapshot


def _create_kwargs(
    snapshot,
    messages: list[dict],
    tool_schemas: list[dict] | None,
    *,
    stream: bool | None,
) -> tuple[dict, ProviderRequestContract]:
    """组装 chat.completions.create 的公共参数（流式/非流式共用）。"""
    try:
        options = json.loads(snapshot.request_options_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ModelEgressDenied("primary model egress denied") from exc
    if not isinstance(options, dict):
        raise ModelEgressDenied("primary model egress denied")
    wire_payload = {
        "model": snapshot.model,
        "messages": messages,
        "tools": tool_schemas or None,
    }
    if stream is not None:
        wire_payload["stream"] = stream
    if snapshot.max_tokens is not None:
        wire_payload["max_tokens"] = snapshot.max_tokens
    wire_payload.update(options)
    kwargs = {
        "model": snapshot.model,
        "messages": messages,
        "tools": tool_schemas or None,
        "extra_body": options or None,
    }
    if stream is not None:
        kwargs["stream"] = stream
    if snapshot.max_tokens is not None:
        kwargs["max_tokens"] = snapshot.max_tokens
    return kwargs, _request_contract("primary", wire_payload)


# ---------- 线格式装配（OpenAI 专有，集中在此，runtime 不碰；RUNTIME-2）----------
def append_assistant_turn(messages: list[dict], result: "ChatResult") -> None:
    """把模型这一轮回复（含工具调用请求）按当前 provider 的线格式追加进 messages。
    tool_calls/function.arguments 是 OpenAI 约定；将来接 Anthropic 在此实现各自线格式即可，
    runtime 只跟中性的 ChatResult/ToolCall 打交道。"""
    messages.append({
        "role": "assistant",
        "content": result.content,
        "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.arguments}}
            for c in result.tool_calls
        ],
    })


def append_tool_result(messages: list[dict], tool_call_id: str, content: str) -> dict:
    """把一次工具执行结果按 provider 线格式回灌（OpenAI: role=tool + tool_call_id）。"""
    message = {"role": "tool", "tool_call_id": tool_call_id, "content": content}
    messages.append(message)
    return message


def chat_stream(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    _policy_lease: object = None,
    _attempt_authorizer=None,
):
    """流式模型调用：逐段 yield ("delta", 正文)，结束时 yield ("result", ChatResult)。

    流式下 tool_calls 按 index 分片增量到达（name 整段、arguments 逐段拼接），
    在此累积重组，调用方拿到的 ChatResult 与非流式完全一致。供应商的
    reasoning_content 只被消费且丢弃，永不进入 runtime/SSE/持久化/日志。
    """
    if not callable(_attempt_authorizer):
        raise ModelEgressDenied("primary model egress denied")
    policy_lease = _coerce_runtime_policy_lease(_policy_lease)
    policy_lease = _require_primary_model_egress(messages, policy_lease)
    kwargs, request_contract = _create_kwargs(
        policy_lease.primary,
        messages,
        tools,
        stream=True,
    )
    client, _snapshot = _client(
        policy_lease=policy_lease,
        request_contract=request_contract,
        attempt_authorizer=_attempt_authorizer,
    )
    try:
        stream = client.chat.completions.create(**kwargs)
        content_parts: list[str] = []
        delta_parts: list[str] = []
        delta_bytes = 0
        emitted_delta_events = 0
        content_filter = ReasoningContentFilter()
        acc: dict[int, dict] = {}
        total_argument_bytes = 0
        visible_bytes = 0
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            # Never inspect, copy or log provider reasoning. Iterating the chunk consumes it;
            # only the declared user-facing content and tool-call fields may cross this boundary.
            clean_content = content_filter.feed(delta.content)
            if clean_content:
                visible_bytes += len(clean_content.encode("utf-8"))
                if visible_bytes > MAX_VISIBLE_RESPONSE_BYTES:
                    raise ModelOutputBudgetExceeded("model output budget exceeded")
                content_parts.append(clean_content)
                delta_parts.append(clean_content)
                delta_bytes += len(clean_content.encode("utf-8"))
                threshold = (
                    limits.FIRST_STREAM_DELTA_BATCH_BYTES
                    if emitted_delta_events == 0
                    else limits.STREAM_DELTA_BATCH_BYTES
                )
                if delta_bytes >= threshold:
                    emitted_delta_events += 1
                    if emitted_delta_events > limits.MAX_PUBLIC_DELTA_EVENTS:
                        raise ModelOutputBudgetExceeded("model output budget exceeded")
                    yield "delta", "".join(delta_parts)
                    delta_parts.clear()
                    delta_bytes = 0
            for tc in (delta.tool_calls or []):
                index = tc.index
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < MAX_TOOL_CALLS_PER_RESPONSE
                ):
                    raise ToolCallBudgetExceeded("tool call budget exceeded")
                if index not in acc:
                    if len(acc) >= MAX_TOOL_CALLS_PER_RESPONSE:
                        raise ToolCallBudgetExceeded("tool call budget exceeded")
                    acc[index] = {
                        "id": "",
                        "name": "",
                        "argument_parts": [],
                        "argument_bytes": 0,
                    }
                slot = acc[index]
                if tc.id:
                    if not isinstance(tc.id, str) or len(tc.id) > MAX_TOOL_CALL_ID_CHARS:
                        raise ToolCallBudgetExceeded("tool call budget exceeded")
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        if (
                            not isinstance(tc.function.name, str)
                            or len(tc.function.name) > MAX_TOOL_NAME_CHARS
                        ):
                            raise ToolCallBudgetExceeded("tool call budget exceeded")
                        slot["name"] = tc.function.name
                    if tc.function.arguments:
                        piece = tc.function.arguments
                        if not isinstance(piece, str):
                            raise ToolCallBudgetExceeded("tool call budget exceeded")
                        piece_bytes = len(piece.encode("utf-8"))
                        if slot["argument_bytes"] + piece_bytes > MAX_TOOL_ARGUMENT_BYTES_PER_CALL:
                            raise ToolCallBudgetExceeded("tool call budget exceeded")
                        total_argument_bytes += piece_bytes
                        if total_argument_bytes > MAX_TOOL_ARGUMENT_BYTES_PER_RESPONSE:
                            raise ToolCallBudgetExceeded("tool call budget exceeded")
                        slot["argument_parts"].append(piece)
                        slot["argument_bytes"] += piece_bytes
        tail = content_filter.finish()
        if tail:
            visible_bytes += len(tail.encode("utf-8"))
            if visible_bytes > MAX_VISIBLE_RESPONSE_BYTES:
                raise ModelOutputBudgetExceeded("model output budget exceeded")
            content_parts.append(tail)
            delta_parts.append(tail)
            delta_bytes += len(tail.encode("utf-8"))
        if delta_parts:
            emitted_delta_events += 1
            if emitted_delta_events > limits.MAX_PUBLIC_DELTA_EVENTS:
                raise ModelOutputBudgetExceeded("model output budget exceeded")
            yield "delta", "".join(delta_parts)
        calls = bounded_tool_calls([
            ToolCall(
                id=v["id"],
                name=v["name"],
                arguments="".join(v["argument_parts"]),
            )
            for _, v in sorted(acc.items())
        ])
        yield "result", ChatResult(content="".join(content_parts) or None, tool_calls=calls)
    except Exception as exc:  # noqa: BLE001 -- map only value-free transport markers
        _raise_mapped_wire_error(exc, profile="primary")
        raise
    finally:
        client.close()


def chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    _policy_lease: object = None,
    _attempt_authorizer=None,
) -> ChatResult:
    """单轮模型调用（非流式）：传入 OpenAI 格式 messages/tools，返回文本或工具调用请求。"""
    if not callable(_attempt_authorizer):
        raise ModelEgressDenied("primary model egress denied")
    policy_lease = _coerce_runtime_policy_lease(_policy_lease)
    policy_lease = _require_primary_model_egress(messages, policy_lease)
    kwargs, request_contract = _create_kwargs(
        policy_lease.primary,
        messages,
        tools,
        stream=None,
    )
    client, _snapshot = _client(
        policy_lease=policy_lease,
        request_contract=request_contract,
        attempt_authorizer=_attempt_authorizer,
    )
    try:
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = _message_tool_calls(msg.tool_calls)
        content = sanitize_model_text(msg.content)
        if len(content.encode("utf-8")) > MAX_VISIBLE_RESPONSE_BYTES:
            raise ModelOutputBudgetExceeded("model output budget exceeded")
        return ChatResult(content=content or None, tool_calls=calls)
    except Exception as exc:  # noqa: BLE001 -- map only value-free transport markers
        _raise_mapped_wire_error(exc, profile="primary")
        raise
    finally:
        client.close()


# ============================================================
# 视觉识别（图片/扫描件 → 文本）。独立 key/端点，默认 通义 Qwen-VL
# ============================================================

def vision_configured() -> bool:
    return bool(get_settings().vision_api_key)


def _img_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def vision_extract(
    images: list[Path],
    hint: str,
    *,
    _policy_lease: object = None,
    _attempt_authorizer=None,
) -> str:
    """图片/扫描件 PDF → 识别文本。images 为图片路径（PDF 由调用方先渲染成图）。

    用 openai SDK 对接 OpenAI 兼容视觉端点（默认 DashScope qwen-vl-max）。
    PDF 路径会先用 pypdfium2 逐页渲染成 PNG 再送（最多前若干页）。
    """
    if not callable(_attempt_authorizer):
        raise VisionEgressDenied("vision provider egress denied")
    policy_lease = _coerce_runtime_policy_lease(_policy_lease)
    if not getattr(policy_lease, "vision_api_key", ""):
        raise VisionNotConfigured("未配置 VISION_API_KEY")
    # Must precede PDF rendering, image reads/base64 encoding and provider-client construction.
    policy_lease = _require_vision_egress(policy_lease)
    snapshot = policy_lease.vision
    _require_vision_payload_budget(images)
    from openai import OpenAI

    _pin_provider_sdk_logging()

    tmp: list[Path] = []
    client = None
    try:
        # 展开：PDF → 多页图；图片原样。渲染后的实际二进制再次受同一 edge 上限约束，
        # 防止小型压缩 PDF 在转图后膨胀，再进入 read/base64/client 边界。
        img_paths: list[Path] = []
        for p in images:
            if p.suffix.lower() == ".pdf":
                tmp.extend(_render_pdf_pages(p, snapshot.max_pages or 0))
            else:
                img_paths.append(p)
        img_paths.extend(tmp)
        if not img_paths:
            return "(无可识别页面)"
        selected_paths = img_paths[: (snapshot.max_pages or 0)]
        _require_vision_payload_budget(selected_paths)

        content = [{"type": "text", "text": hint}]
        for ip in selected_paths:
            content.append({"type": "image_url", "image_url": {"url": _img_data_url(ip)}})

        request_projection = {
            "model": snapshot.model,
            "messages": [{"role": "user", "content": content}],
        }
        _require_vision_projection_budget(request_projection)
        request_contract = _request_contract("vision", request_projection)

        client = OpenAI(
            api_key=policy_lease.vision_api_key,
            admin_api_key="",
            organization="",
            project="",
            webhook_secret="",
            base_url=snapshot.base_url,
            timeout=snapshot.timeout_seconds,
            max_retries=0,
            default_headers={},
            default_query={},
            http_client=_secure_http_client(
                profile="vision",
                snapshot=snapshot,
                expected_api_key=policy_lease.vision_api_key,
                request_contract=request_contract,
                max_request_bytes=limits.VISION_INPUT_MAX_BYTES,
                expected_runtime_policy_fingerprint=policy_lease.fingerprint,
                attempt_authorizer=_attempt_authorizer,
            ),
        )
        try:
            resp = client.chat.completions.create(
                **request_projection,
            )
        except Exception as exc:  # noqa: BLE001 -- map only our value-free transport marker
            _raise_mapped_wire_error(exc, profile="vision")
            raise
        out = _strip_think(resp.choices[0].message.content or "")
        from app.agent import tools
        if not tools.vision_ocr_payload_allowed(out):
            raise VisionPayloadBudgetExceeded("vision payload budget exceeded")
        return out or "(视觉模型未返回内容)"
    finally:
        if client is not None:
            client.close()
        render_dirs = {t.parent for t in tmp}
        for t in tmp:
            t.unlink(missing_ok=True)
        for directory in render_dirs:
            try:
                directory.rmdir()
            except OSError:
                pass


def _render_pdf_pages(pdf_path: Path, max_pages: int) -> list[Path]:
    """PDF 逐页渲染成 PNG（pypdfium2，pdfplumber 依赖自带）。返回临时文件路径。"""
    import pypdfium2 as pdfium

    out: list[Path] = []
    render_dir = Path(tempfile.mkdtemp(prefix="agent-vision-render-"))
    started_at = time.monotonic()
    total_pixels = 0
    doc = None
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            width = float(page.get_width())
            height = float(page.get_height())
            pixels = math.ceil(width * 2.0) * math.ceil(height * 2.0)
            total_pixels += pixels
            if (
                width <= 0
                or height <= 0
                or pixels <= 0
                or pixels > limits.VISION_RENDER_MAX_PIXELS_PER_PAGE
                or total_pixels > limits.VISION_RENDER_MAX_PIXELS_TOTAL
                or time.monotonic() - started_at > limits.VISION_RENDER_MAX_SECONDS
            ):
                raise VisionPayloadBudgetExceeded("vision payload budget exceeded")
            bitmap = page.render(scale=2.0)  # ~144dpi，够清晰
            if time.monotonic() - started_at > limits.VISION_RENDER_MAX_SECONDS:
                raise VisionPayloadBudgetExceeded("vision payload budget exceeded")
            img = bitmap.to_pil()
            dst = render_dir / f"page-{i}.png"
            img.save(dst)
            out.append(dst)
        if not out:
            render_dir.rmdir()
    except Exception as exc:
        for path in render_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
        render_dir.rmdir()
        if isinstance(exc, VisionPayloadBudgetExceeded):
            raise
        raise VisionPayloadBudgetExceeded("vision payload budget exceeded") from None
    finally:
        if doc is not None:
            doc.close()
    return out
