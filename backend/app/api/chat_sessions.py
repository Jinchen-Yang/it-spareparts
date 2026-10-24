"""对话会话 API（平台化 P1：服务端持久化，对标扣子/DeepSeek 网页版）。

- 统一授权下会话归属稳定用户 ID，旧兼容实例沿用 token.sub；管理员也不能看别人的对话。
- /{id}/chat/stream：客户端只发新消息，历史由服务端取（窗口见 chat_store）。
  客户端中断（停止生成/断网）时，已生成的部分照样落库并标 stopped。
- 无状态 /agent/chat(/stream) 在新权威下仅接收用户输入，可信助手历史由会话服务加载。
"""
import json
import logging
import queue
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import provider, runtime, stream_protocol as wire
from app.api.agent_route import AgentRoute
from app.auth import current_identity
from app.config import get_settings
from app.db import get_db
from app.security import UserContext, get_current_user_context, record_access_log, require_page
from app.services import chat_store, agent_authorization as authorization
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.authz.provenance import AgentAccess

_log = logging.getLogger("agent")
# 后端页面准入：page_chat=False 的角色连会话接口都进不来（PR-审计 HUB-1）。
router = APIRouter(route_class=AgentRoute, prefix="/agent/sessions", tags=["agent"],
                   dependencies=[Depends(authorization.page_gate)])

_NOT_CONFIGURED_MSG = (
    "AI 助手尚未配置：请在服务器 .env 设置 LLM_API_KEY（如 DeepSeek 密钥）后重启服务。"
    "在此之前可以继续使用「型号查询」页的近似搜索。"
)


class CreateSessionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    authorization_precondition: dict | None = None
    title: str | None = Field(None, max_length=80)


class RenameSessionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    authorization_precondition: dict | None = None
    title: str = Field(..., min_length=1, max_length=80)


class SendMessageRequest(BaseModel):
    model_config = {"extra": "forbid"}
    authorization_precondition: dict | None = None
    message: str = Field(..., min_length=1, max_length=8000)


def _require_agent_enabled() -> None:
    if not get_settings().enable_agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI 助手未启用")


def _require_real_identity(ident: dict) -> None:
    """会话按 token.sub 归属——共享口令回退登录的 sub 是自报的任意字符串，
    两个人自称同一个名字就会互看对话。这类身份禁用会话功能。"""
    if ident.get("fb"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "AI 助手会话需要实名账号（请管理员在系统中为你创建用户）")


def _owned_or_404(db: Session, ident: dict, session_id: int, *, access=None):
    _require_real_identity(ident)
    s = chat_store.get_owned(db, ident["sub"], session_id, access=access)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return s


@router.get("")
def list_sessions(
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    _require_real_identity(ident)
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.LIST, {}, view=view, precondition=authorization_precondition)
    if resolution is not None:
        return resolution
    items = chat_store.list_sessions(db, ident["sub"], access=access)
    for it in items:   # 标出后台仍在生成的会话，前端给「生成中」角标
        it["generating"] = is_generating(it["id"])
    return {"items": items, **({'authorization_context': access.execution_wire} if access.authoritative else {})}


@router.post("")
def create_session(
    req: CreateSessionRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    _require_real_identity(ident)
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.CREATE, {'title': req.title}, view=view, precondition=authorization.precondition(authorization_precondition, req.authorization_precondition) if access.authoritative else None)
    if resolution is not None:
        return resolution
    s = chat_store.create_session(db, ident["sub"], req.title, access=access)
    return {"id": s.id, "title": s.title, "updated_at": s.updated_at}


@router.patch("/{session_id}")
def rename_session(
    session_id: int,
    req: RenameSessionRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.RENAME, {'session_id': session_id, 'title': req.title}, view=view, precondition=authorization.precondition(authorization_precondition, req.authorization_precondition) if access.authoritative else None)
    if resolution is not None:
        return resolution
    s = _owned_or_404(db, ident, session_id, access=access)
    chat_store.rename_session(db, s, req.title, access=access)
    return {"id": s.id, "title": s.title}


@router.delete("/{session_id}")
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.DELETE, {'session_id': session_id}, view=view, precondition=authorization_precondition)
    if resolution is not None:
        return resolution
    s = _owned_or_404(db, ident, session_id, access=access)
    chat_store.delete_session(db, s, access=access)
    return {"ok": True}


@router.get("/{session_id}/messages")
def list_messages(
    session_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.HISTORY, {'session_id': session_id}, view=view, precondition=authorization_precondition)
    if resolution is not None:
        return resolution
    s = _owned_or_404(db, ident, session_id, access=access)
    if access.authoritative:
        return {**chat_store._canonical_history(db, s.id, access), 'authorization_context': access.execution_wire}
    return {"id": s.id, "title": s.title,
            "items": chat_store.list_messages(db, s.id, access=access)}


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


class _RunHub:
    """一轮生成的事件中枢：缓冲 + 多订阅者扇出 + 取消信号。"""

    def __init__(self) -> None:
        self.access = None
        self.run_id = wire.new_run_id()
        self._seq = 0
        self._terminal = False
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._buffer: list[dict] = []          # 本轮全部事件，供晚到订阅者回放
        self._buffer_capped = False
        self._subs: list["queue.Queue[dict | None]"] = []
        self._done = False

    def publish(self, ev: dict) -> None:
        if self.access is not None and ev.get("type") != "error":
            self.access.check()
        with self._lock:
            if self.access is not None and self.access.authoritative:
                if self._terminal:
                    raise ValueError('Cannot publish after terminal event')
                ev = wire.frame(ev, self.run_id, self._seq + 1)
                self._seq += 1
                self._terminal = ev['type'] in wire.TERMINAL
            if len(self._buffer) < _BUFFER_EVENT_CAP:
                self._buffer.append(ev)
            elif not self._buffer_capped:
                self._buffer_capped = True
                _log.warning("RunHub buffer 超 %d 事件，停止缓冲（实时流不受影响；"
                             "晚到订阅者回放将截断）", _BUFFER_EVENT_CAP)
            for q in self._subs:
                q.put(ev)

    def finish(self) -> None:
        with self._lock:
            self._done = True
            for q in self._subs:
                q.put(None)                    # 结束哨兵

    def subscribe(self) -> tuple[list[dict], "queue.Queue[dict | None] | None"]:
        """返回 (已发生事件回放, 后续事件队列)；已结束则队列为 None。
        在锁内快照 buffer 再注册，保证回放与实时之间不丢不重。"""
        with self._lock:
            replay = list(self._buffer)
            if self._done:
                return replay, None
            q: "queue.Queue[dict | None]" = queue.Queue()
            self._subs.append(q)
            return replay, q

    def subscribe_complete(self):
        with self._lock:
            if self._buffer_capped:
                return [], None, False
            replay = list(self._buffer)
            if self._done:
                return replay, None, True
            q = queue.Queue()
            self._subs.append(q)
            return replay, q, True

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
    return f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"


def _hub_event_stream(hub: _RunHub, *, access=None, session_id=None):
    def check():
        if hub.access is not None:
            hub.access.check_sources()
            if access is not None and access.authoritative:
                access.inherit(hub.access.snapshot())
        if access is not None:
            access.check()
    canonical = access is not None and access.authoritative
    replay, sub_q, complete = hub.subscribe_complete() if canonical else (*hub.subscribe(), True)
    expected = 1
    terminal = False
    try:
        if canonical:
            yield wire.encode(wire.ready(session_id, hub.run_id, access.execution_wire, replay_complete=complete))
            check()
            if not complete:
                yield wire.encode(wire.frame(wire.error_payload('CHAT_REPLAY_GAP'), hub.run_id, 1))
                return
        def send(ev):
            nonlocal expected, terminal
            check()
            if canonical:
                if ev['seq'] != expected or ev['run_id'] != hub.run_id or terminal:
                    raise ValueError('Invalid hub sequence')
                expected += 1
                terminal = ev['type'] in wire.TERMINAL
            return wire.encode(ev)
        for ev in replay:
            yield send(ev)
        if sub_q is not None and not terminal:
            while True:
                try:
                    ev = sub_q.get(timeout=600)
                except queue.Empty:
                    break
                if ev is None:
                    break
                yield send(ev)
                if terminal:
                    break
        if canonical and not terminal:
            yield wire.encode(wire.frame(wire.error_payload('CHAT_STREAM_PROTOCOL_ERROR'), hub.run_id, expected))
    except Exception as exc:
        if not terminal:
            error = wire.exception_payload(exc)
            yield wire.encode(wire.frame(error, hub.run_id, expected) if canonical else error)
    finally:
        if sub_q is not None:
            hub.unsubscribe(sub_q)


@router.post("/{session_id}/chat/cancel")
def chat_cancel(
    session_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """停止当前生成：worker 收到信号后把已生成部分以 stopped=True 落库——
    前端显示的"已中断"与库内状态一致。"""
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.CANCEL, {'session_id': session_id}, view=view, precondition=authorization_precondition)
    if resolution is not None:
        return resolution
    s = _owned_or_404(db, ident, session_id, access=access)
    record_access_log(ctx, "chat_cancel", "agent", {"session_id": s.id})
    return {"cancelled": request_cancel(s.id)}


@router.post("/{session_id}/chat/stream")
def chat_stream(
    session_id: int,
    req: SendMessageRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """会话内流式问答。SSE 事件与旧 /agent/chat/stream 一致，外加
    {type:"title", title} —— 首条消息后服务端定的会话标题。

    中断语义：①点"停止"（/chat/cancel）→ worker 尽快收束，已生成部分以
    stopped=True 落库；②客户端断网/关页面（无 cancel）→ worker 跑到底，
    完整答案落库（重新打开会话可见全文）。"""
    _require_agent_enabled()
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.RUN, {'session_id': session_id, 'message': req.message}, view=view, precondition=authorization.precondition(authorization_precondition, req.authorization_precondition) if access.authoritative else None)
    if resolution is not None:
        return resolution
    s = _owned_or_404(db, ident, session_id, access=access)
    record_access_log(ctx, "chat_stream", "agent",
                      {"session_id": s.id, "q": req.message[:200]})

    hub = acquire_session(s.id)
    if hub is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"code": "CHAT_RUN_BUSY", "message": "该会话上一轮回答仍在生成", "accepted": False})

    hub.access = access
    sid = s.id  # 捕获纯标量：流式期间不再触碰请求级 ORM 对象/会话
    try:
        # 先落用户消息（即使后面中断，提问也不丢）；首条消息顺带定标题
        title_before = s.title
        history = chat_store.history_for_llm(db, s.id, access=access)
        chat_store.append_message(db, s, "user", req.message, access=access)
        history.append({'role': 'user', 'content': req.message})
        new_title = s.title if s.title != title_before else None
        configured = provider.is_configured()
        if not configured:
            chat_store.append_message(db, s, "assistant", _NOT_CONFIGURED_MSG, access=access)
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

        CHECKPOINT_DELTAS = 40
        buf: list[str] = []
        trace: list[dict] = []
        msg_id: int | None = None
        since_ckpt = 0
        finalized = False
        authorization_failed = False

        def _save(stopped: bool, final: bool = False) -> None:
            nonlocal msg_id, since_ckpt, finalized
            if finalized or authorization_failed:
                return
            content = "".join(buf).strip()
            if not content and not trace:
                return
            try:
                msg_id = chat_store.save_assistant_progress(
                    sid, msg_id, content or "(无内容)", trace, stopped, access=access)
                since_ckpt = 0
                if final:
                    finalized = True
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001 —— 落库失败不能炸生成
                _log.exception("checkpoint assistant message failed (session=%s)", sid)
                if access.authoritative:
                    from app.authz.errors import authz_error
                    raise authz_error(503, 'CHAT_PERSISTENCE_FAILED', '回答保存失败') from None

        wdb = SessionLocal()
        try:
            # cancel 透传给 runtime：除 worker 在事件间轮询外，runtime 也在 LLM 流内/调用前
            # 检查，点"停止"后能更快收束（含收尾作答，RUNTIME-4）。
            for ev in runtime.run_stream(wdb, history, ctx, cancel=hub.cancel, access=access):
                if hub.cancel.is_set():  # 用户点了停止：收束并以"已中断"落库
                    _save(stopped=True, final=True)
                    hub.publish({"type": "done", "tool_calls": trace, "stopped": True})
                    return
                if ev.get("type") == "delta":
                    buf.append(ev.get("text") or "")
                    since_ckpt += 1
                    if since_ckpt >= CHECKPOINT_DELTAS:
                        _save(stopped=True)  # 中间态：若进程在此挂掉，状态即"被中断"
                elif ev.get("type") == "tool":
                    trace.append({"name": ev.get("name"), "args": ev.get("args")})
                    _save(stopped=True)
                elif ev.get("type") == "tool_done":
                    wdb.rollback()  # 工具均只读：立即把连接还回池，别跨整轮 LLM 往返占着
                elif ev.get("type") == "done":
                    _save(stopped=False, final=True)
                hub.publish(ev)
        except HTTPException as exc:
            authorization_failed = True
            hub.publish(wire.exception_payload(exc))
        except Exception as exc:  # noqa: BLE001 —— 上游 LLM 错误：保留已生成部分
            _log.error("agent session stream failed: %r", exc)
            try:
                _save(stopped=True, final=True)
            except Exception as save_exc:
                exc = save_exc
            authorization_failed = True
            hub.publish(wire.exception_payload(exc))
        finally:
            try:
                _save(stopped=True, final=True)
            finally:
                wdb.close()
                release_session(sid)  # → hub.finish() 给所有订阅者发结束哨兵

    try:
        if new_title:
            hub.publish({'type': 'title', 'title': new_title})
        if not configured:
            hub.publish({'type': 'delta', 'text': _NOT_CONFIGURED_MSG})
            hub.publish({'type': 'done', 'tool_calls': [], 'configured': False})
    except BaseException:
        release_session(sid)
        raise

    # worker 在 handler 里启动而非 gen 里：客户端若在响应体被拉取前断开，
    # gen 可能永远不被迭代——锁释放/落库必须不依赖 gen 跑起来。
    if configured:
        try:
            threading.Thread(target=_worker, daemon=True,
                             name=f"agent-session-{sid}").start()
        except BaseException:
            release_session(sid)
            raise
    else:
        release_session(sid)

    def gen():
        yield from _hub_event_stream(hub, access=access, session_id=sid)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


@router.post("/{session_id}/chat/attach")
def chat_attach(
    session_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """重新订阅会话进行中的生成（切回会话续看连续直播）：先回放已生成部分、再续实时。
    无进行中的生成 → 单个 {type:"no_active"} 事件，前端转为加载历史消息即可。"""
    _require_agent_enabled()
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.ATTACH, {'session_id': session_id}, view=view, precondition=authorization_precondition)
    if resolution is not None:
        return resolution
    s = _owned_or_404(db, ident, session_id, access=access)
    sid = s.id
    db.rollback()  # 释放请求连接：订阅期间不再触碰请求级会话
    hub = get_run(sid)
    if access.authoritative and hub is not None and hub.access is not None:
        access.inherit(hub.access.snapshot())
        access.check()

    def gen():
        if hub is None:
            if access.authoritative:
                yield wire.encode(wire.ready(sid, None, access.execution_wire))
                try:
                    access.check()
                    yield wire.encode(wire.frame({'type': 'no_active'}, None, 1))
                except Exception as exc:
                    yield wire.encode(wire.frame(wire.exception_payload(exc), None, 1))
            else:
                access.check()
                yield _sse({'type': 'no_active'})
            return
        yield from _hub_event_stream(hub, access=access, session_id=sid)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})
