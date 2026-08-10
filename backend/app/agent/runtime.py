"""Agent 循环：system + 历史 → 模型 → 工具调用 → 回灌 → 直到产出最终答复。

单一内部循环 _agent_loop（run 与 run_stream 共用，仅"增量如何出口"分叉，RUNTIME-1）：
- run：非流式入口，消费循环只取最终答复（不含中间旁白），返回 {answer, tool_calls}。
- run_stream：流式入口，把循环事件原样转发供 SSE 推送。

事件：{type:"delta",text} 正文增量 /
{type:"tool",name,args} 工具开始（args 只是无值的参数形状） /
{type:"tool_done",name,ok,artifact_ids} 工具完成 /
{type:"done",tool_calls,answer,stopped?} 结束。

服务端无状态：对话历史由调用方持有并随请求带上。OpenAI 线格式装配下沉到 provider.append_*
（RUNTIME-2），此处只跟中性的 ChatResult/ToolCall 打交道。
"""
import json
import hashlib
import logging
import threading
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import security
from app.agent import limits, prompts, provider, tools

_log = logging.getLogger("agent")

_ITER_LIMIT_PROMPT = "工具调用轮数已达上限，请基于以上已获取的数据直接给出最终回答。"
MODEL_EGRESS_DENIED_MESSAGE = "当前 AI 模型数据出境策略未授权，已阻止发送会话内容"
MODEL_EGRESS_DENIED_CODE = "AGENT_MODEL_EGRESS_DENIED"
GENERIC_ERROR_MESSAGE = "AI 服务调用失败，请稍后重试"
GENERIC_ERROR_KIND = "runtime_error"
GENERIC_ERROR_CODE = "AGENT_RUNTIME_ERROR"
TOOL_CALL_BUDGET_MESSAGE = "模型工具调用超过安全预算，已停止本轮任务"
TOOL_CALL_BUDGET_KIND = "tool_call_budget_exceeded"
TOOL_CALL_BUDGET_CODE = "AGENT_TOOL_CALL_BUDGET_EXCEEDED"
EGRESS_PAYLOAD_BUDGET_MESSAGE = "模型上下文或工具结果超过数据出向预算，已停止本轮任务"
EGRESS_PAYLOAD_BUDGET_KIND = "egress_payload_budget_exceeded"
EGRESS_PAYLOAD_BUDGET_CODE = "AGENT_EGRESS_PAYLOAD_BUDGET_EXCEEDED"
MODEL_OUTPUT_BUDGET_MESSAGE = "模型输出超过安全预算，已停止本轮任务"
MODEL_OUTPUT_BUDGET_KIND = "model_output_budget_exceeded"
MODEL_OUTPUT_BUDGET_CODE = "AGENT_MODEL_OUTPUT_BUDGET_EXCEEDED"
IDENTITY_STALE_MESSAGE = "身份状态已失效，请重新登录"
IDENTITY_STALE_KIND = "identity_denied"
IDENTITY_STALE_CODE = "AGENT_IDENTITY_STALE"
CAPABILITY_REVOKED_MESSAGE = "能力权限已撤销，已停止本轮任务"
CAPABILITY_REVOKED_KIND = "capability_revoked"
CAPABILITY_REVOKED_CODE = "AGENT_CAPABILITY_REVOKED"
MAX_TOOL_CALLS_PER_RUN = limits.MAX_TOOL_CALLS_PER_RUN


@dataclass(frozen=True)
class _ReleasedResult:
    """Server-only replay authority; never persisted, logged or projected to public events."""

    name: str
    message: dict
    source_file_ids: tuple[str, ...]


_PUBLIC_ERROR_CONTRACTS = {
    MODEL_EGRESS_DENIED_CODE: {
        "message": MODEL_EGRESS_DENIED_MESSAGE,
        "kind": "model_context_egress_denied",
        "retriable": False,
    },
    TOOL_CALL_BUDGET_CODE: {
        "message": TOOL_CALL_BUDGET_MESSAGE,
        "kind": TOOL_CALL_BUDGET_KIND,
        "retriable": False,
    },
    EGRESS_PAYLOAD_BUDGET_CODE: {
        "message": EGRESS_PAYLOAD_BUDGET_MESSAGE,
        "kind": EGRESS_PAYLOAD_BUDGET_KIND,
        "retriable": False,
    },
    MODEL_OUTPUT_BUDGET_CODE: {
        "message": MODEL_OUTPUT_BUDGET_MESSAGE,
        "kind": MODEL_OUTPUT_BUDGET_KIND,
        "retriable": False,
    },
    IDENTITY_STALE_CODE: {
        "message": IDENTITY_STALE_MESSAGE,
        "kind": IDENTITY_STALE_KIND,
        "retriable": False,
    },
    CAPABILITY_REVOKED_CODE: {
        "message": CAPABILITY_REVOKED_MESSAGE,
        "kind": CAPABILITY_REVOKED_KIND,
        "retriable": False,
    },
}


def primary_model_call_allowed() -> bool:
    """Re-evaluate the live destination policy immediately before provider use."""
    return tools.primary_model_call_allowed()


def primary_model_payload_allowed(messages: object) -> bool:
    """Re-evaluate the conversation edge byte ceiling immediately before provider use."""
    return tools.primary_model_payload_allowed(messages)


def model_egress_error_event(tool_calls: object = None) -> dict:
    """Stable, value-free denial contract shared by runtime and API preflight."""
    return {
        "type": "error",
        "message": MODEL_EGRESS_DENIED_MESSAGE,
        "kind": "model_context_egress_denied",
        "code": MODEL_EGRESS_DENIED_CODE,
        "retriable": False,
        "tool_calls": tools.sanitize_tool_trace(tool_calls),
    }


def project_error_event(value: object = None) -> dict:
    """Rebuild an SSE error from a small, value-free public contract.

    Runtime/provider exceptions and future alternate runtimes are untrusted telemetry sources.
    Only a code already registered here may select a public contract; its message, kind and
    retryability are taken from the registry rather than copied from the input. Unknown errors
    collapse to one generic event. Every other source key (args, result, trace, debug, etc.) is
    deliberately discarded.
    """
    candidate = value.get("code") if isinstance(value, dict) else None
    code = candidate if isinstance(candidate, str) else None
    contract = _PUBLIC_ERROR_CONTRACTS.get(code)
    if contract is None:
        return {
            "type": "error",
            "message": GENERIC_ERROR_MESSAGE,
            "kind": GENERIC_ERROR_KIND,
            "code": GENERIC_ERROR_CODE,
            "retriable": True,
        }
    return {
        "type": "error",
        "message": contract["message"],
        "kind": contract["kind"],
        "code": code,
        "retriable": contract["retriable"],
    }


def tool_call_budget_error_event() -> dict:
    """Stable public response for provider/runtime tool-call amplification attempts."""
    return project_error_event({"code": TOOL_CALL_BUDGET_CODE})


def egress_payload_budget_error_event() -> dict:
    """Stable public response for an enforced edge byte ceiling."""
    return project_error_event({"code": EGRESS_PAYLOAD_BUDGET_CODE})


def model_output_budget_error_event() -> dict:
    """Stable public response for provider-visible answer amplification attempts."""
    return project_error_event({"code": MODEL_OUTPUT_BUDGET_CODE})


def identity_stale_error_event() -> dict:
    """Stable public response when the named DB principal cannot be refreshed."""
    return project_error_event({"code": IDENTITY_STALE_CODE})


def capability_revoked_error_event() -> dict:
    """Stable public response when a live role/permission no longer authorizes a result."""
    return project_error_event({"code": CAPABILITY_REVOKED_CODE})


def model_egress_error_result(tool_calls: object = None) -> dict:
    event = model_egress_error_event(tool_calls)
    return {
        "answer": event["message"],
        "tool_calls": event["tool_calls"],
        "kind": event["kind"],
        "code": event["code"],
        "retriable": event["retriable"],
    }


class PublicEventProjector:
    """Single stateful boundary for SSE, hub replay and durable session progress."""

    def __init__(self, artifact_authorizer=None) -> None:
        self._reasoning_filter = provider.ReasoningContentFilter()
        self._visible_bytes = 0
        self._visible_parts: list[str] = []
        self._pending_delta_parts: list[str] = []
        self._pending_delta_bytes = 0
        self._delta_events = 0
        self._trace: list[dict] = []
        self._pending_tools: dict[str, int] = {}
        self._terminated = False
        self._artifact_authorizer = artifact_authorizer

    @property
    def trace(self) -> list[dict]:
        return _authorized_trace(self._trace, self._artifact_authorizer)

    def _visible(self, value: object) -> str | None:
        text = self._reasoning_filter.feed(value)
        if not text:
            return None
        size = len(text.encode("utf-8"))
        if self._visible_bytes + size > limits.MAX_VISIBLE_RUN_BYTES:
            self._terminated = True
            return None
        self._visible_bytes += size
        self._pending_delta_parts.append(text)
        self._pending_delta_bytes += size
        return text

    def _tail(self) -> str | None:
        tail = self._reasoning_filter.finish()
        if not tail:
            return None
        size = len(tail.encode("utf-8"))
        if self._visible_bytes + size > limits.MAX_VISIBLE_RUN_BYTES:
            self._terminated = True
            return None
        self._visible_bytes += size
        self._pending_delta_parts.append(tail)
        self._pending_delta_bytes += size
        return tail

    def _flush_delta(self, *, force: bool) -> list[dict]:
        threshold = (
            limits.FIRST_STREAM_DELTA_BATCH_BYTES
            if self._delta_events == 0
            else limits.STREAM_DELTA_BATCH_BYTES
        )
        if not self._pending_delta_parts or (not force and self._pending_delta_bytes < threshold):
            return []
        self._delta_events += 1
        if self._delta_events > limits.MAX_PUBLIC_DELTA_EVENTS:
            self._terminated = True
            self._pending_delta_parts.clear()
            self._pending_delta_bytes = 0
            return [model_output_budget_error_event()]
        text = "".join(self._pending_delta_parts)
        self._visible_parts.append(text)
        self._pending_delta_parts.clear()
        self._pending_delta_bytes = 0
        return [{"type": "delta", "text": text}]

    def _trace_entry(self, name: object, args: object, artifact_ids: object, *, shaped: bool) -> dict:
        entry = tools.safe_tool_trace_entry(
            name,
            args,
            artifact_ids,
            args_are_shape=shaped,
        )
        admitted: list[str] = []
        if self._artifact_authorizer is not None:
            for artifact_id in entry.get("artifact_ids", []):
                try:
                    if self._artifact_authorizer(artifact_id):
                        admitted.append(artifact_id)
                except Exception:  # noqa: BLE001 -- authorization failures are fail-closed
                    continue
        if admitted:
            entry["artifact_ids"] = admitted
        else:
            entry.pop("artifact_ids", None)
        return entry

    def project(self, value: object) -> list[dict]:
        """Return zero or more rebuilt public events; unknown/thinking events are dropped."""
        if self._terminated or not isinstance(value, dict):
            return []
        event_type = value.get("type")
        if event_type == "delta":
            self._visible(value.get("text"))
            if self._terminated:
                return [model_output_budget_error_event()]
            return self._flush_delta(force=False)
        if event_type == "tool":
            events = self._flush_delta(force=True)
            if self._terminated:
                return events
            if len(self._trace) >= limits.MAX_PUBLIC_TRACE_ENTRIES:
                self._terminated = True
                return [*events, tool_call_budget_error_event()]
            entry = self._trace_entry(
                value.get("name"),
                value.get("args"),
                value.get("artifact_ids"),
                shaped=value.get("args_are_shape") is True,
            )
            self._trace.append({**entry, "args_are_shape": True})
            name = entry["name"]
            self._pending_tools[name] = self._pending_tools.get(name, 0) + 1
            return [*events, {"type": "tool", **entry, "args_are_shape": True}]
        if event_type == "tool_done":
            events = self._flush_delta(force=True)
            if self._terminated:
                return events
            entry = self._trace_entry(
                value.get("name"),
                {},
                value.get("artifact_ids"),
                shaped=False,
            )
            pending = self._pending_tools.get(entry["name"], 0)
            if pending <= 0:
                self._terminated = True
                return [*events, tool_call_budget_error_event()]
            if pending == 1:
                self._pending_tools.pop(entry["name"], None)
            else:
                self._pending_tools[entry["name"]] = pending - 1
            artifact_ids = entry.get("artifact_ids", [])
            if artifact_ids:
                for item in reversed(self._trace):
                    if item.get("name") == entry["name"]:
                        item["artifact_ids"] = artifact_ids
                        break
            return [*events, {
                "type": "tool_done",
                "name": entry["name"],
                "ok": bool(value.get("ok")),
                "artifact_ids": artifact_ids,
            }]
        if event_type in {"done", "error"}:
            # A streamed terminal answer is never trusted as a second source of truth.  If no
            # visible delta was observed, a done-only adapter may supply its answer through the
            # same filter and cumulative budget exactly once.
            if event_type == "done" and self._visible_bytes == 0:
                self._visible(value.get("answer"))
            self._tail()
            if self._terminated:
                return [model_output_budget_error_event()]
            events = self._flush_delta(force=True)
            if self._terminated:
                return events
            self._terminated = True
            if event_type == "error":
                events.append(project_error_event(value))
                return events
            events.append({
                "type": "done",
                "tool_calls": self.trace,
                "answer": "".join(self._visible_parts),
                "stopped": bool(value.get("stopped", False)),
            })
            return events
        # `thinking`, raw provider records and future event types are not public contracts.
        return []


def _authorized_trace(value: object, artifact_authorizer=None) -> list[dict]:
    clean = tools.sanitize_tool_trace(value)
    for entry in clean:
        admitted: list[str] = []
        if artifact_authorizer is not None:
            for artifact_id in entry.get("artifact_ids", []):
                try:
                    if artifact_authorizer(artifact_id):
                        admitted.append(artifact_id)
                except Exception:  # noqa: BLE001 -- authorization failures are fail-closed
                    continue
        if admitted:
            entry["artifact_ids"] = admitted
        else:
            entry.pop("artifact_ids", None)
    return clean


def project_run_result(value: object, artifact_authorizer=None) -> dict:
    """Rebuild the stateless JSON result from public fields only."""
    if not isinstance(value, dict):
        event = project_error_event()
        return {
            "answer": event["message"],
            "tool_calls": [],
            "kind": event["kind"],
            "code": event["code"],
            "retriable": event["retriable"],
        }
    code = value.get("code")
    if isinstance(code, str) and code in _PUBLIC_ERROR_CONTRACTS:
        event = project_error_event(value)
        return {
            "answer": event["message"],
            "tool_calls": _authorized_trace(value.get("tool_calls"), artifact_authorizer),
            "kind": event["kind"],
            "code": event["code"],
            "retriable": event["retriable"],
        }
    answer = provider.sanitize_model_text(value.get("answer"))
    if len(answer.encode("utf-8")) > limits.MAX_VISIBLE_RUN_BYTES:
        event = model_output_budget_error_event()
        return {
            "answer": event["message"],
            "tool_calls": [],
            "kind": event["kind"],
            "code": event["code"],
            "retriable": event["retriable"],
        }
    return {
        "answer": answer,
        "tool_calls": _authorized_trace(value.get("tool_calls"), artifact_authorizer),
    }


_INVALID_TOOL_ARGS = object()


def _parse_args(raw: object) -> object:
    """Parse provider tool arguments without turning malformed input into a broad empty query."""
    if not isinstance(raw, str) or not raw:
        return _INVALID_TOOL_ARGS

    def reject_constant(_value: str):
        raise ValueError("non-finite JSON constant")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        args = json.loads(
            raw,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return _INVALID_TOOL_ARGS
    return args if isinstance(args, dict) else _INVALID_TOOL_ARGS


def _safe_chat_result(
    value: object,
    visible_parts: list[str] | None = None,
) -> provider.ChatResult | None:
    if not isinstance(value, provider.ChatResult):
        return None
    content = (
        "".join(visible_parts)
        if visible_parts
        else provider.sanitize_model_text(value.content)
    )
    if len(content.encode("utf-8")) > limits.MAX_VISIBLE_RESPONSE_BYTES:
        raise provider.ModelOutputBudgetExceeded("model output budget exceeded")
    return provider.ChatResult(
        content=content or None,
        tool_calls=provider.bounded_tool_calls(value.tool_calls),
    )


def _runtime_subject_epoch(ctx: security.UserContext) -> str | None:
    """Hash every identity/row/field authorization input that may shape a run."""
    if not isinstance(ctx, security.UserContext):
        return None
    try:
        payload = json.dumps(
            {
                "user_id": ctx.user_id,
                "role": ctx.role,
                "salesperson_name": ctx.salesperson_name,
                "permissions": ctx.permissions,
                "ding_user_id": ctx.ding_user_id,
                "department_id": ctx.department_id,
                "team_id": ctx.team_id,
                "is_authenticated": ctx.is_authenticated,
                "authn": ctx.authn,
                "token_version": ctx.token_version,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _agent_loop(db: Session, messages: list[dict], ctx: security.UserContext,
                *, cancel: "threading.Event | None" = None):
    """单一 Agent 循环，yield 事件 dict。

    cancel（threading.Event）为一等入参（RUNTIME-4）：每次 LLM 调用前、流内 chunk 间检查，
    置位即尽快收束——不再只能在事件之间被动等待，收尾作答也可中断。
    """
    # One immutable policy epoch governs every provider round, SDK retry and nested Vision tool.
    # A new independent run may capture a newly admitted profile; this run never silently does.
    policy_lease = tools.capture_runtime_policy_lease()
    initial_ctx = tools.refresh_runtime_context(db, ctx)
    subject_epoch = _runtime_subject_epoch(initial_ctx) if initial_ctx is not None else None
    if initial_ctx is None or subject_epoch is None:
        yield identity_stale_error_event()
        return
    if not security.page_allowed(initial_ctx, "page_chat"):
        yield capability_revoked_error_event()
        return
    msgs: list[dict] = [{"role": "system", "content": prompts.system_prompt()}] + messages
    trace: list[dict] = []
    total_tool_calls = 0
    visible_run_bytes = 0
    public_delta_events = 0
    released_results: list[_ReleasedResult] = []

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    def public_trace() -> list[dict]:
        return tools.sanitize_tool_trace(trace)

    def purge_released_results() -> None:
        if not released_results:
            return
        released_ids = {id(record.message) for record in released_results}
        msgs[:] = [message for message in msgs if id(message) not in released_ids]
        released_results.clear()

    def release_denial_event(
        name: str,
        live_ctx: security.UserContext,
        source_file_ids: tuple[str, ...] = (),
    ) -> dict | None:
        if tools.capability_result_release_allowed(name, live_ctx):
            if tools.source_file_ids_release_allowed(source_file_ids, live_ctx):
                return None
            return capability_revoked_error_event()
        if not tools.tool_result_egress_allowed(name):
            return model_egress_error_event(public_trace())
        return capability_revoked_error_event()

    def release_public_event(event: dict) -> tuple[dict, bool]:
        """Re-authorize every coalesced public release against a fresh DB identity/policy."""
        if (
            not tools.runtime_policy_lease_current(policy_lease)
            or not primary_model_call_allowed()
        ):
            purge_released_results()
            return model_egress_error_event(public_trace()), True
        current_ctx = tools.refresh_runtime_context(db, ctx)
        if current_ctx is None:
            purge_released_results()
            return identity_stale_error_event(), True
        if not security.page_allowed(current_ctx, "page_chat"):
            purge_released_results()
            return capability_revoked_error_event(), True
        if _runtime_subject_epoch(current_ctx) != subject_epoch:
            purge_released_results()
            return capability_revoked_error_event(), True
        for record in released_results:
            denial = release_denial_event(
                record.name,
                current_ctx,
                record.source_file_ids,
            )
            if denial is not None:
                purge_released_results()
                return denial, True
        return event, False

    def authorize_primary_attempt() -> bool:
        """Re-authorize the exact principal and every released capability at the wire seam."""
        current_ctx = tools.refresh_runtime_context(db, ctx)
        if (
            current_ctx is None
            or not security.page_allowed(current_ctx, "page_chat")
            or _runtime_subject_epoch(current_ctx) != subject_epoch
            or not tools.runtime_policy_lease_current(policy_lease)
            or not primary_model_call_allowed()
        ):
            return False
        return all(
            tools.capability_result_release_allowed(record.name, current_ctx)
            and tools.source_file_ids_release_allowed(record.source_file_ids, current_ctx)
            for record in released_results
        )

    for _ in range(policy_lease.max_tool_iters):
        if cancelled():
            event, denied = release_public_event(
                {"type": "done", "tool_calls": public_trace(), "answer": "", "stopped": True}
            )
            yield event
            return
        live_ctx = tools.refresh_runtime_context(db, ctx)
        if live_ctx is None:
            purge_released_results()
            yield identity_stale_error_event()
            return
        if not security.page_allowed(live_ctx, "page_chat"):
            purge_released_results()
            yield capability_revoked_error_event()
            return
        if _runtime_subject_epoch(live_ctx) != subject_epoch:
            purge_released_results()
            yield capability_revoked_error_event()
            return
        for record in released_results:
            denial = release_denial_event(
                record.name,
                live_ctx,
                record.source_file_ids,
            )
            if denial is not None:
                purge_released_results()
                yield denial
                return
        tool_schemas = tools.tools_for(live_ctx)
        if (
            not tools.runtime_policy_lease_current(policy_lease)
            or not primary_model_call_allowed()
        ):
            yield model_egress_error_event(public_trace())
            return
        if not primary_model_payload_allowed(msgs):
            yield egress_payload_budget_error_event()
            return
        res = None
        content_filter = provider.ReasoningContentFilter()
        visible_parts: list[str] = []
        pending_delta_parts: list[str] = []
        pending_delta_bytes = 0
        response_visible_bytes = 0

        def flush_runtime_delta(*, force: bool) -> str | None:
            nonlocal pending_delta_bytes, public_delta_events
            threshold = (
                limits.FIRST_STREAM_DELTA_BATCH_BYTES
                if public_delta_events == 0
                else limits.STREAM_DELTA_BATCH_BYTES
            )
            if not pending_delta_parts or (not force and pending_delta_bytes < threshold):
                return None
            public_delta_events += 1
            if public_delta_events > limits.MAX_PUBLIC_DELTA_EVENTS:
                raise provider.ModelOutputBudgetExceeded("model output budget exceeded")
            batch = "".join(pending_delta_parts)
            pending_delta_parts.clear()
            pending_delta_bytes = 0
            visible_parts.append(batch)
            return batch

        try:
            for kind, payload in provider.chat_stream(
                msgs,
                tool_schemas,
                _policy_lease=policy_lease,
                _attempt_authorizer=authorize_primary_attempt,
            ):
                if kind == "delta":
                    clean_content = content_filter.feed(payload)
                    if clean_content:
                        chunk_bytes = len(clean_content.encode("utf-8"))
                        response_visible_bytes += chunk_bytes
                        visible_run_bytes += chunk_bytes
                        if (
                            response_visible_bytes > limits.MAX_VISIBLE_RESPONSE_BYTES
                            or visible_run_bytes > limits.MAX_VISIBLE_RUN_BYTES
                        ):
                            raise provider.ModelOutputBudgetExceeded(
                                "model output budget exceeded"
                            )
                        pending_delta_parts.append(clean_content)
                        pending_delta_bytes += chunk_bytes
                        batch = flush_runtime_delta(force=False)
                        if batch is not None:
                            event, denied = release_public_event(
                                {"type": "delta", "text": batch}
                            )
                            yield event
                            if denied:
                                return
                elif kind == "reasoning":
                    # Defense in depth for alternate/future providers: reasoning never becomes
                    # SSE, trace, checkpoint or log data even if the provider adapter emits it.
                    pass
                else:
                    res = _safe_chat_result(payload)
                if cancelled():
                    event, denied = release_public_event({
                        "type": "done",
                        "tool_calls": public_trace(),
                        "answer": (res.content if res else "") or "",
                        "stopped": True,
                    })
                    yield event
                    return
        except provider.ModelEgressDenied:
            yield model_egress_error_event(public_trace())
            return
        except provider.ToolCallBudgetExceeded:
            yield tool_call_budget_error_event()
            return
        except provider.ModelPayloadBudgetExceeded:
            yield egress_payload_budget_error_event()
            return
        except provider.ModelOutputBudgetExceeded:
            yield model_output_budget_error_event()
            return
        tail = content_filter.finish()
        if tail:
            tail_bytes = len(tail.encode("utf-8"))
            response_visible_bytes += tail_bytes
            visible_run_bytes += tail_bytes
            if (
                response_visible_bytes > limits.MAX_VISIBLE_RESPONSE_BYTES
                or visible_run_bytes > limits.MAX_VISIBLE_RUN_BYTES
            ):
                yield model_output_budget_error_event()
                return
            pending_delta_parts.append(tail)
            pending_delta_bytes += tail_bytes
        try:
            batch = flush_runtime_delta(force=True)
        except provider.ModelOutputBudgetExceeded:
            yield model_output_budget_error_event()
            return
        if batch is not None:
            event, denied = release_public_event({"type": "delta", "text": batch})
            yield event
            if denied:
                return
        res = _safe_chat_result(res, visible_parts)
        if not visible_parts and res is not None and res.content:
            visible_run_bytes += len(res.content.encode("utf-8"))
            if visible_run_bytes > limits.MAX_VISIBLE_RUN_BYTES:
                yield model_output_budget_error_event()
                return
        if res is None or not res.tool_calls:
            event, denied = release_public_event({
                "type": "done",
                "tool_calls": public_trace(),
                "answer": (res.content if res else "") or "",
            })
            yield event
            return

        if total_tool_calls + len(res.tool_calls) > MAX_TOOL_CALLS_PER_RUN:
            yield tool_call_budget_error_event()
            return
        total_tool_calls += len(res.tool_calls)

        if res.content:   # 本轮有旁白又接着调工具：补段落分隔，避免与后文粘连
            visible_run_bytes += 2
            if visible_run_bytes > limits.MAX_VISIBLE_RUN_BYTES:
                yield model_output_budget_error_event()
                return
            event, denied = release_public_event({"type": "delta", "text": "\n\n"})
            yield event
            if denied:
                return
        parsed_calls: list[tuple[provider.ToolCall, object]] = []
        canonical_calls: list[provider.ToolCall] = []
        for c in res.tool_calls:
            args = _parse_args(c.arguments)
            parsed_calls.append((c, args))
            canonical_arguments = (
                json.dumps(args, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                if isinstance(args, dict)
                else "{}"
            )
            canonical_calls.append(provider.ToolCall(
                id=c.id,
                name=c.name,
                arguments=canonical_arguments,
            ))
        provider.append_assistant_turn(
            msgs,
            provider.ChatResult(content=res.content, tool_calls=canonical_calls),
        )
        for c, args in parsed_calls:
            args_shape = tools.audit_summary(c.name, args)
            event, denied = release_public_event({
                "type": "tool",
                "name": tools.audit_name(c.name),
                "args": args_shape,
                "args_are_shape": True,
            })
            yield event
            if denied:
                return
            result = tools.dispatch(
                db,
                c.name,
                args,
                live_ctx,
                _policy_lease=policy_lease,
            )
            source_file_ids = tools.source_file_ids_from_tool_exchange(args, result)
            post_handler_ctx = tools.refresh_runtime_context(db, ctx)
            if post_handler_ctx is None:
                purge_released_results()
                yield identity_stale_error_event()
                return
            if not security.page_allowed(post_handler_ctx, "page_chat"):
                purge_released_results()
                yield capability_revoked_error_event()
                return
            if _runtime_subject_epoch(post_handler_ctx) != subject_epoch:
                purge_released_results()
                yield capability_revoked_error_event()
                return
            denial = release_denial_event(c.name, post_handler_ctx, source_file_ids)
            if denial is not None:
                purge_released_results()
                yield denial
                return
            projected_result = tools.serialize_tool_result_for_model(c.name, result)
            artifact_ids = tools.authorized_artifact_ids_from_result(result, post_handler_ctx)
            trace.append({
                "name": c.name,
                "args": args_shape,
                "args_are_shape": True,
                "artifact_ids": artifact_ids,
            })
            if projected_result is None:
                denial = release_denial_event(c.name, post_handler_ctx, source_file_ids)
                if denial is not None:
                    yield denial
                else:
                    yield egress_payload_budget_error_event()
                return
            _log.info("agent tool=%s", tools.audit_name(c.name))
            event, denied = release_public_event({
                "type": "tool_done",
                "name": tools.audit_name(c.name),
                "ok": not (isinstance(result, dict) and result.get("error")),
                "artifact_ids": artifact_ids,
            })
            yield event
            if denied:
                return
            latest_ctx = tools.refresh_runtime_context(db, ctx)
            if latest_ctx is None:
                purge_released_results()
                yield identity_stale_error_event()
                return
            if not security.page_allowed(latest_ctx, "page_chat"):
                purge_released_results()
                yield capability_revoked_error_event()
                return
            if _runtime_subject_epoch(latest_ctx) != subject_epoch:
                purge_released_results()
                yield capability_revoked_error_event()
                return
            denial = release_denial_event(c.name, latest_ctx, source_file_ids)
            if denial is not None:
                purge_released_results()
                yield denial
                return
            tool_message = provider.append_tool_result(msgs, c.id, projected_result)
            released_results.append(_ReleasedResult(
                name=c.name,
                message=tool_message,
                source_file_ids=source_file_ids,
            ))

    # 轮数上限：收尾作答仍走流式逐字（RUNTIME-5：不再退化为非流式整段，避免最长对话突兀静默）
    msgs.append({"role": "user", "content": _ITER_LIMIT_PROMPT})
    final_ctx = tools.refresh_runtime_context(db, ctx)
    if final_ctx is None:
        purge_released_results()
        yield identity_stale_error_event()
        return
    if not security.page_allowed(final_ctx, "page_chat"):
        purge_released_results()
        yield capability_revoked_error_event()
        return
    if _runtime_subject_epoch(final_ctx) != subject_epoch:
        purge_released_results()
        yield capability_revoked_error_event()
        return
    for record in released_results:
        denial = release_denial_event(
            record.name,
            final_ctx,
            record.source_file_ids,
        )
        if denial is not None:
            purge_released_results()
            yield denial
            return
    if (
        not tools.runtime_policy_lease_current(policy_lease)
        or not primary_model_call_allowed()
    ):
        yield model_egress_error_event(public_trace())
        return
    if not primary_model_payload_allowed(msgs):
        yield egress_payload_budget_error_event()
        return
    final = None
    content_filter = provider.ReasoningContentFilter()
    visible_parts = []
    pending_delta_parts = []
    pending_delta_bytes = 0
    response_visible_bytes = 0

    def flush_final_delta(*, force: bool) -> str | None:
        nonlocal pending_delta_bytes, public_delta_events
        threshold = (
            limits.FIRST_STREAM_DELTA_BATCH_BYTES
            if public_delta_events == 0
            else limits.STREAM_DELTA_BATCH_BYTES
        )
        if not pending_delta_parts or (not force and pending_delta_bytes < threshold):
            return None
        public_delta_events += 1
        if public_delta_events > limits.MAX_PUBLIC_DELTA_EVENTS:
            raise provider.ModelOutputBudgetExceeded("model output budget exceeded")
        batch = "".join(pending_delta_parts)
        pending_delta_parts.clear()
        pending_delta_bytes = 0
        visible_parts.append(batch)
        return batch

    try:
        for kind, payload in provider.chat_stream(
            msgs,
            _policy_lease=policy_lease,
            _attempt_authorizer=authorize_primary_attempt,
        ):
            if kind == "delta":
                clean_content = content_filter.feed(payload)
                if clean_content:
                    chunk_bytes = len(clean_content.encode("utf-8"))
                    response_visible_bytes += chunk_bytes
                    visible_run_bytes += chunk_bytes
                    if (
                        response_visible_bytes > limits.MAX_VISIBLE_RESPONSE_BYTES
                        or visible_run_bytes > limits.MAX_VISIBLE_RUN_BYTES
                    ):
                        raise provider.ModelOutputBudgetExceeded(
                            "model output budget exceeded"
                        )
                    pending_delta_parts.append(clean_content)
                    pending_delta_bytes += chunk_bytes
                    batch = flush_final_delta(force=False)
                    if batch is not None:
                        event, denied = release_public_event(
                            {"type": "delta", "text": batch}
                        )
                        yield event
                        if denied:
                            return
            elif kind == "reasoning":
                pass
            else:
                final = _safe_chat_result(payload)
            if cancelled():
                event, denied = release_public_event({
                    "type": "done",
                    "tool_calls": public_trace(),
                    "answer": (final.content if final else "") or "",
                    "stopped": True,
                })
                yield event
                return
    except provider.ModelEgressDenied:
        yield model_egress_error_event(public_trace())
        return
    except provider.ToolCallBudgetExceeded:
        yield tool_call_budget_error_event()
        return
    except provider.ModelPayloadBudgetExceeded:
        yield egress_payload_budget_error_event()
        return
    except provider.ModelOutputBudgetExceeded:
        yield model_output_budget_error_event()
        return
    tail = content_filter.finish()
    if tail:
        tail_bytes = len(tail.encode("utf-8"))
        response_visible_bytes += tail_bytes
        visible_run_bytes += tail_bytes
        if (
            response_visible_bytes > limits.MAX_VISIBLE_RESPONSE_BYTES
            or visible_run_bytes > limits.MAX_VISIBLE_RUN_BYTES
        ):
            yield model_output_budget_error_event()
            return
        pending_delta_parts.append(tail)
        pending_delta_bytes += tail_bytes
    try:
        batch = flush_final_delta(force=True)
    except provider.ModelOutputBudgetExceeded:
        yield model_output_budget_error_event()
        return
    if batch is not None:
        event, denied = release_public_event({"type": "delta", "text": batch})
        yield event
        if denied:
            return
    final = _safe_chat_result(final, visible_parts)
    if not visible_parts and final is not None and final.content:
        visible_run_bytes += len(final.content.encode("utf-8"))
        if visible_run_bytes > limits.MAX_VISIBLE_RUN_BYTES:
            yield model_output_budget_error_event()
            return
    event, denied = release_public_event({
        "type": "done",
        "tool_calls": public_trace(),
        "answer": (final.content if final else "") or "",
    })
    yield event


def run(db: Session, messages: list[dict], ctx: security.UserContext) -> dict:
    """非流式：跑完循环，返回 {answer, tool_calls}。

    answer 只取最终答复（done 事件的 answer），不含中间旁白——与旧实现口径一致。
    """
    answer, trace = "", []
    for ev in _agent_loop(db, messages, ctx):
        if ev["type"] == "done":
            answer = ev.get("answer", "")
            trace = ev.get("tool_calls", trace)
        elif ev["type"] == "error":
            public_error = project_error_event(ev)
            return project_run_result({
                "answer": public_error["message"],
                "tool_calls": tools.sanitize_tool_trace(ev.get("tool_calls", trace)),
                "kind": public_error["kind"],
                "code": public_error["code"],
                "retriable": public_error["retriable"],
            }, tools.fresh_artifact_authorizer(ctx))
    return project_run_result(
        {"answer": answer, "tool_calls": trace},
        tools.fresh_artifact_authorizer(ctx),
    )


def run_stream(db: Session, messages: list[dict], ctx: security.UserContext,
               cancel: "threading.Event | None" = None):
    """流式：error 经固定公开契约重建，其余循环事件转发。cancel 透传以便及时收束。"""
    projector = PublicEventProjector(tools.fresh_artifact_authorizer(ctx))
    for raw_event in _agent_loop(db, messages, ctx, cancel=cancel):
        for event in projector.project(raw_event):
            yield event
            if event.get("type") == "error":
                return
