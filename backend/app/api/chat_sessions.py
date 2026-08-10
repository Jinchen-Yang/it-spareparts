"""对话会话 API（平台化 P1：服务端持久化，对标扣子/DeepSeek 网页版）。

- 会话/消息只归属 authn=sys_user 的稳定 token.sub；共享口令不能创建持久会话。
  本人可见，管理员也不能看别人的对话（报价隐私）。
- /{id}/chat/stream：客户端只发新消息，历史由服务端取（窗口见 chat_store）。
  客户端中断（停止生成/断网）时，已生成的部分照样落库并标 stopped。
- 旧的无状态 /agent/chat(/stream) 保留不动，供脚本/API 调用方使用。
"""
import json
import logging
import queue
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import security
from app.agent import audit as agent_audit
from app.agent import limits as agent_limits
from app.agent import provider, runtime, tools as agent_tools
from app.auth import current_identity
from app.config import get_settings
from app.db import get_db
from app.security import UserContext, get_current_user_context, record_access_log, require_page
from app.services import chat_store

_log = logging.getLogger("agent")


def _require_agent_enabled() -> None:
    if not get_settings().enable_agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI 助手未启用")


# 后端页面准入：page_chat=False 的角色连会话接口都进不来（PR-审计 HUB-1）。
router = APIRouter(prefix="/agent/sessions", tags=["agent"],
                   dependencies=[Depends(_require_agent_enabled), Depends(require_page("page_chat"))])

_NOT_CONFIGURED_MSG = (
    "AI 助手尚未配置：请在服务器 .env 设置 LLM_API_KEY（如 DeepSeek 密钥）后重启服务。"
    "在此之前可以继续使用「型号查询」页的近似搜索。"
)


class CreateSessionRequest(BaseModel):
    title: str | None = Field(None, max_length=80)


class RenameSessionRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)


def _require_real_identity(ident: dict) -> None:
    """会话按稳定 token.sub 归属；仅数据库实名账号可创建或读取持久会话。

    shared token 的 sub 可能自报（readonly）或固定为 admin，但两者都没有可吊销的账号主体，
    因此即使历史 token 没有 fb 标记，也必须按 authn=sys_user 失败关闭。
    """
    if ident.get("authn") != "sys_user":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "AI 助手会话需要实名账号（请管理员在系统中为你创建用户）")


def _owned_or_404(db: Session, ident: dict, session_id: int):
    _require_real_identity(ident)
    s = chat_store.get_owned(db, ident["sub"], session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return s


@router.get("")
def list_sessions(
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
    _require_real_identity(ident)
    items = chat_store.list_sessions(db, ident["sub"])
    for it in items:   # 标出后台仍在生成的会话，前端给「生成中」角标
        it["generating"] = is_generating(it["id"])
    return {"items": items}


@router.post("")
def create_session(
    req: CreateSessionRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
    _require_real_identity(ident)
    s = chat_store.create_session(db, ident["sub"], req.title)
    return {"id": s.id, "title": s.title, "updated_at": s.updated_at}


@router.patch("/{session_id}")
def rename_session(
    session_id: int,
    req: RenameSessionRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
    s = _owned_or_404(db, ident, session_id)
    chat_store.rename_session(db, s, req.title)
    return {"id": s.id, "title": s.title}


@router.delete("/{session_id}")
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
    s = _owned_or_404(db, ident, session_id)
    chat_store.delete_session(db, s)
    return {"ok": True}


@router.get("/{session_id}/messages")
def list_messages(
    session_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    s = _owned_or_404(db, ident, session_id)
    live_ctx = agent_tools.refresh_runtime_context(db, ctx)
    if live_ctx is None or not security.page_allowed(live_ctx, "page_chat"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问 AI 对话")
    artifact_authorizer = agent_tools.fresh_artifact_authorizer(ctx)
    return {"id": s.id, "title": s.title,
            "items": chat_store.list_messages(
                db,
                s.id,
                artifact_authorizer,
            )}


# ---------- 会话级生成中枢（互斥 + 取消 + 可重连扇出）----------
# 同一会话同时只允许一轮生成（ChatGPT 语义）：并发生成会让消息 id 顺序与
# 对话顺序永久错乱、半截 checkpoint 被当完整历史回灌 LLM。
# RunHub：worker 把每个事件既存进 buffer 又扇出给所有订阅者 → 原始流断开后，
# 切回会话可经 /chat/attach 重新订阅：先回放 buffer(已生成部分)、再续实时流
# （连续逐字直播）。单进程 uvicorn（Dockerfile 无 --workers）下进程内注册表即可；
# 多进程部署需改 Redis pub/sub + DB 行锁。
_guard_lock = threading.Lock()

# 回放缓冲事件数上限（backstop）：正常一轮远不及此（llm_max_tokens 已从源头限长）；
# 仅防病态超长 run 撑爆内存。超限后停止缓冲——实时订阅不受影响，只是晚到订阅者回放被截断。
_BUFFER_EVENT_CAP = 5000
_SUBSCRIBER_QUEUE_CAP = 256
_MAX_SUBSCRIBERS_PER_RUN = 8
_CHECKPOINT_BYTES = 64 * 1024
_CHECKPOINT_SECONDS = 5.0
_MAX_INTERMEDIATE_CHECKPOINTS = 96
_MAX_CHECKPOINT_FAILURES = 3
_SUBSCRIBER_RETRY_EVENT = {"type": "subscriber_evicted", "retry_attach": True}


def _start_agent_worker(target, *, name: str) -> None:
    """Narrow worker-start seam; tests must never monkeypatch global ``Thread.start``."""
    threading.Thread(target=target, daemon=True, name=name).start()


class _RunHub:
    """一轮生成的事件中枢：缓冲 + 多订阅者扇出 + 取消信号。"""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._buffer: list[dict] = []          # 本轮全部事件，供晚到订阅者回放
        self._buffer_capped = False
        self._subs: list["queue.Queue[dict | None]"] = []
        self._done = False

    def publish(self, ev: dict) -> None:
        with self._lock:
            if len(self._buffer) < _BUFFER_EVENT_CAP:
                self._buffer.append(ev)
            elif not self._buffer_capped:
                self._buffer_capped = True
                _log.warning("RunHub buffer 超 %d 事件，停止缓冲（实时流不受影响；"
                             "晚到订阅者回放将截断）", _BUFFER_EVENT_CAP)
            evicted: list["queue.Queue[dict | None]"] = []
            for q in self._subs:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    # A disconnected/slow subscriber must never apply backpressure or retain an
                    # unbounded copy of model output. Drop its stale queue and explicitly tell the
                    # client to reload durable history; a bare EOF would look like normal success.
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    q.put_nowait(dict(_SUBSCRIBER_RETRY_EVENT))
                    q.put_nowait(None)
                    evicted.append(q)
            for q in evicted:
                self._subs.remove(q)

    def finish(self) -> None:
        with self._lock:
            self._done = True
            for q in self._subs:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    # A queue that became exactly full on the final publish never gets another
                    # publish-time eviction opportunity. Bare EOF would make the client mark its
                    # truncated answer complete, so finish must use the same explicit retry path.
                    q.put_nowait(dict(_SUBSCRIBER_RETRY_EVENT))
                    q.put_nowait(None)
            self._subs.clear()

    def subscribe(self) -> tuple[list[dict], "queue.Queue[dict | None] | None"]:
        """返回 (已发生事件回放, 后续事件队列)；已结束则队列为 None。
        在锁内快照 buffer 再注册，保证回放与实时之间不丢不重。"""
        with self._lock:
            if self._done:
                return list(self._buffer), None
            if len(self._subs) >= _MAX_SUBSCRIBERS_PER_RUN:
                _log.warning("RunHub subscriber limit reached")
                return [dict(_SUBSCRIBER_RETRY_EVENT)], None
            replay = list(self._buffer)
            q: "queue.Queue[dict | None]" = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_CAP)
            self._subs.append(q)
            return replay, q

    def unsubscribe(self, q: "queue.Queue[dict | None]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


_active_runs: dict[int, _RunHub] = {}  # session_id -> 当前生成中枢


def acquire_session(session_id: int) -> _RunHub | None:
    """开始生成：返回该轮的中枢；该会话已有生成中则返回 None。"""
    with _guard_lock:
        if session_id in _active_runs:
            return None
        hub = _RunHub()
        _active_runs[session_id] = hub
        return hub


def get_run(session_id: int) -> _RunHub | None:
    with _guard_lock:
        return _active_runs.get(session_id)


def is_generating(session_id: int) -> bool:
    with _guard_lock:
        return session_id in _active_runs


def release_session(session_id: int) -> None:
    """结束生成：移出注册表并给所有订阅者发结束哨兵。"""
    with _guard_lock:
        hub = _active_runs.pop(session_id, None)
    if hub is not None:
        hub.finish()


def request_cancel(session_id: int) -> bool:
    """请求取消该会话当前生成。返回是否有生成在进行。"""
    hub = get_run(session_id)
    if hub is None:
        return False
    hub.cancel.set()
    return True


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False, default=str, allow_nan=False)}\n\n"


def _hub_event_stream(hub: _RunHub):
    """订阅一个生成中枢：先回放已发生事件（切回会话补齐已生成部分），
    再续实时事件，直到结束哨兵。客户端断开则 finally 注销订阅。"""
    replay, sub_q = hub.subscribe()
    try:
        for ev in replay:
            yield _sse(ev)
        if sub_q is None:          # 订阅时已结束：回放完即收
            return
        while True:
            try:
                ev = sub_q.get(timeout=600)
            except queue.Empty:
                return
            if ev is None:         # 结束哨兵
                return
            yield _sse(ev)
    finally:
        if sub_q is not None:
            hub.unsubscribe(sub_q)


@router.post("/{session_id}/chat/cancel")
def chat_cancel(
    session_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """停止当前生成：worker 收到信号后把已生成部分以 stopped=True 落库——
    前端显示的"已中断"与库内状态一致。"""
    s = _owned_or_404(db, ident, session_id)
    record_access_log(ctx, "chat_cancel", "agent", {"session_id": s.id})
    return {"cancelled": request_cancel(s.id)}


@router.post("/{session_id}/chat/stream")
def chat_stream(
    session_id: int,
    req: SendMessageRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """会话内流式问答。SSE 事件与旧 /agent/chat/stream 一致，外加
    {type:"title", title} —— 首条消息后服务端定的会话标题。

    中断语义：①点"停止"（/chat/cancel）→ worker 尽快收束，已生成部分以
    stopped=True 落库；②客户端断网/关页面（无 cancel）→ worker 跑到底，
    完整答案落库（重新打开会话可见全文）。"""
    _require_agent_enabled()
    s = _owned_or_404(db, ident, session_id)
    record_access_log(
        ctx,
        "chat_stream",
        "agent",
        agent_audit.chat_request_shape(
            message_count=1,
            last_message=req.message,
            endpoint="session_chat_stream",
            stream=True,
            session_id=s.id,
        ),
    )

    configured = provider.is_configured()
    if configured and not runtime.primary_model_call_allowed():
        # Preflight before acquiring a run or persisting the new user turn. Runtime repeats the
        # same live check before every provider call to catch policy drift during a tool loop.
        denied = runtime.project_error_event(runtime.model_egress_error_event([]))

        def denied_gen():
            yield _sse(denied)

        return StreamingResponse(
            denied_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    hub = acquire_session(s.id)
    if hub is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "该会话上一轮回答还在生成中，请稍候或先停止")

    sid = s.id  # 捕获纯标量：流式期间不再触碰请求级 ORM 对象/会话
    try:
        # 先落用户消息（即使后面中断，提问也不丢）；首条消息顺带定标题
        title_before = s.title
        chat_store.append_message(db, s, "user", req.message)
        history = chat_store.history_for_llm(db, s.id)
        new_title = s.title if s.title != title_before else None
        if not configured:
            chat_store.append_message(db, s, "assistant", _NOT_CONFIGURED_MSG)
    except BaseException:
        release_session(sid)
        raise
    finally:
        # 释放请求级连接回池：StreamingResponse 流完前 get_db 不会 teardown，
        # 不 rollback 的话每路对话白占一个池连接直到生成结束（评审实测确认）
        db.rollback()

    # 生成与传输解耦（ChatGPT 语义）：agent 循环在独立线程跑、沿途 checkpoint
    # 落库，并把事件 publish 到 hub（buffer + 扇出给所有订阅者）；HTTP 流只是
    # hub 的一个订阅者。切回会话经 /chat/attach 再订阅同一 hub → 回放已生成
    # 部分 + 续实时（连续逐字直播）。教训：sync generator 被客户端中断后既不被
    # close 也不再前进，落库/锁释放必须由 worker 的 finally 负责，不依赖流被消费。
    def _worker() -> None:
        from app.db import SessionLocal

        buf: list[str] = []
        trace: list[dict] = []
        msg_id: int | None = None
        since_ckpt_bytes = 0
        checkpoint_attempts = 0
        checkpoint_failures = 0
        last_checkpoint_at = time.monotonic()
        finalized = False
        buf_bytes = 0
        delta_filter = provider.ReasoningContentFilter()
        def authorize_artifact_live(artifact_id: str) -> bool:
            live_ctx = agent_tools.refresh_runtime_context(wdb, ctx)
            return bool(
                live_ctx is not None
                and security.page_allowed(live_ctx, "page_chat")
                and agent_tools.artifact_id_release_allowed(artifact_id, live_ctx)
            )

        public_projector = runtime.PublicEventProjector(authorize_artifact_live)

        def _save(stopped: bool, final: bool = False) -> None:
            nonlocal msg_id, since_ckpt_bytes, checkpoint_attempts, checkpoint_failures
            nonlocal last_checkpoint_at, finalized
            if finalized:
                return
            if not final and (
                checkpoint_attempts >= _MAX_INTERMEDIATE_CHECKPOINTS
                or checkpoint_failures >= _MAX_CHECKPOINT_FAILURES
            ):
                return
            if final:
                finalized = True
            content = "".join(buf).strip()
            if not content and not trace:
                return
            # Count and throttle attempts, not only successes. A persistent DB outage must not
            # turn every subsequent delta into another connection/commit/log attempt.
            if not final:
                checkpoint_attempts += 1
            since_ckpt_bytes = 0
            last_checkpoint_at = time.monotonic()
            try:
                live_ctx = agent_tools.refresh_runtime_context(wdb, ctx)
                if live_ctx is None or not security.page_allowed(live_ctx, "page_chat"):
                    return
                artifact_authorizer = agent_tools.fresh_artifact_authorizer(ctx)
                safe_trace = runtime._authorized_trace(trace, artifact_authorizer)
                msg_id = chat_store.save_assistant_progress(
                    sid,
                    msg_id,
                    content or "(无内容)",
                    safe_trace,
                    stopped,
                    artifact_authorizer=artifact_authorizer,
                )
                checkpoint_failures = 0
            except Exception as exc:  # noqa: BLE001 —— 落库失败不能炸生成
                checkpoint_failures += 1
                _log.error(
                    "checkpoint assistant message failed session=%s exception_type=%s",
                    sid,
                    type(exc).__name__,
                )

        wdb = SessionLocal()
        try:
            # cancel 透传给 runtime：除 worker 在事件间轮询外，runtime 也在 LLM 流内/调用前
            # 检查，点"停止"后能更快收束（含收尾作答，RUNTIME-4）。
            def projected_runtime_events():
                for raw_event in runtime.run_stream(
                    wdb,
                    history,
                    ctx,
                    cancel=hub.cancel,
                ):
                    yield from public_projector.project(raw_event)

            for ev in projected_runtime_events():
                if hub.cancel.is_set():  # 用户点了停止：收束并以"已中断"落库
                    _save(stopped=True, final=True)
                    hub.publish({
                        "type": "done",
                        "tool_calls": runtime._authorized_trace(trace, authorize_artifact_live),
                        "stopped": True,
                    })
                    return
                event_type = ev.get("type")
                if event_type not in {"delta", "tool", "tool_done", "done", "error"}:
                    # In particular, never buffer/replay a forged or legacy `thinking` event.
                    # The same whitelist protects attach subscribers from future event drift.
                    continue
                if event_type == "delta":
                    clean_text = delta_filter.feed(ev.get("text"))
                    if not clean_text:
                        continue
                    clean_bytes = len(clean_text.encode("utf-8"))
                    if buf_bytes + clean_bytes > agent_limits.MAX_VISIBLE_RUN_BYTES:
                        _save(stopped=True, final=True)
                        hub.publish(runtime.model_output_budget_error_event())
                        return
                    buf_bytes += clean_bytes
                    ev = {"type": "delta", "text": clean_text}
                    buf.append(clean_text)
                    since_ckpt_bytes += clean_bytes
                    if (
                        since_ckpt_bytes >= _CHECKPOINT_BYTES
                        or time.monotonic() - last_checkpoint_at >= _CHECKPOINT_SECONDS
                    ):
                        _save(stopped=True)  # 中间态：若进程在此挂掉，状态即"被中断"
                elif event_type == "tool":
                    if len(trace) >= agent_tools.MAX_PUBLIC_TRACE_ENTRIES:
                        _save(stopped=True, final=True)
                        hub.publish(runtime.tool_call_budget_error_event())
                        return
                    entry = agent_tools.safe_tool_trace_entry(
                        ev.get("name"),
                        ev.get("args"),
                        ev.get("artifact_ids"),
                        args_are_shape=ev.get("args_are_shape") is True,
                    )
                    trace.append({**entry, "args_are_shape": True})
                    ev = {"type": "tool", **entry, "args_are_shape": True}
                    _save(stopped=True)
                elif event_type == "tool_done":
                    done_entry = agent_tools.safe_tool_trace_entry(
                        ev.get("name"),
                        {},
                        ev.get("artifact_ids"),
                    )
                    artifact_ids = done_entry.get("artifact_ids", [])
                    if artifact_ids:
                        for item in reversed(trace):
                            if item.get("name") == done_entry["name"]:
                                item["artifact_ids"] = artifact_ids
                                break
                    ev = {
                        "type": "tool_done",
                        "name": done_entry["name"],
                        "ok": bool(ev.get("ok")),
                        "artifact_ids": artifact_ids,
                    }
                    wdb.rollback()  # 工具均只读：立即把连接还回池，别跨整轮 LLM 往返占着
                elif event_type == "done":
                    tail = delta_filter.finish()
                    if tail:
                        tail_bytes = len(tail.encode("utf-8"))
                        if buf_bytes + tail_bytes > agent_limits.MAX_VISIBLE_RUN_BYTES:
                            _save(stopped=True, final=True)
                            hub.publish(runtime.model_output_budget_error_event())
                            return
                        buf_bytes += tail_bytes
                        buf.append(tail)
                        hub.publish({"type": "delta", "text": tail})
                    _save(stopped=False, final=True)
                    ev = {
                        "type": "done",
                        "tool_calls": runtime._authorized_trace(trace, authorize_artifact_live),
                        "stopped": bool(ev.get("stopped", False)),
                    }
                elif event_type == "error":
                    # Alternate/future runtimes are untrusted telemetry sources. Rebuild from a
                    # fixed allowlist so raw messages, args, results and debug fields cannot enter
                    # the hub buffer, initial SSE, attach replay or logs. Error is terminal: a
                    # future adapter cannot publish an error and then mutate trace/state with more
                    # tool_done/done events.
                    tail = delta_filter.finish()
                    if tail:
                        tail_bytes = len(tail.encode("utf-8"))
                        if buf_bytes + tail_bytes > agent_limits.MAX_VISIBLE_RUN_BYTES:
                            _save(stopped=True, final=True)
                            hub.publish(runtime.model_output_budget_error_event())
                            return
                        buf_bytes += tail_bytes
                        buf.append(tail)
                        hub.publish({"type": "delta", "text": tail})
                    ev = runtime.project_error_event(ev)
                    _save(stopped=True, final=True)
                    hub.publish(ev)
                    return
                hub.publish(ev)
        except Exception as exc:  # noqa: BLE001 —— 上游 LLM 错误：保留已生成部分
            _log.error("agent session stream failed exception_type=%s", type(exc).__name__)
            _save(stopped=True, final=True)
            hub.publish(runtime.project_error_event())
        finally:
            _save(stopped=True, final=True)
            wdb.close()
            release_session(sid)  # → hub.finish() 给所有订阅者发结束哨兵

    # worker 在 handler 里启动而非 gen 里：客户端若在响应体被拉取前断开，
    # gen 可能永远不被迭代——锁释放/落库必须不依赖 gen 跑起来。
    if configured:
        try:
            _start_agent_worker(_worker, name=f"agent-session-{sid}")
        except BaseException:
            release_session(sid)
            raise
    else:
        release_session(sid)

    def gen():
        if new_title:
            yield _sse({"type": "title", "title": new_title})
        if not configured:
            yield _sse({"type": "delta", "text": _NOT_CONFIGURED_MSG})
            yield _sse({"type": "done", "tool_calls": [], "configured": False})
            return
        yield from _hub_event_stream(hub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/{session_id}/chat/attach")
def chat_attach(
    session_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """重新订阅会话进行中的生成（切回会话续看连续直播）：先回放已生成部分、再续实时。
    无进行中的生成 → 单个 {type:"no_active"} 事件，前端转为加载历史消息即可。"""
    _require_agent_enabled()
    s = _owned_or_404(db, ident, session_id)
    sid = s.id
    db.rollback()  # 释放请求连接：订阅期间不再触碰请求级会话
    hub = get_run(sid)

    def gen():
        if hub is None:
            yield _sse({"type": "no_active"})
            return
        yield from _hub_event_stream(hub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
