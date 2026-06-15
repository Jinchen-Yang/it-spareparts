"""对话会话 API（平台化 P1：服务端持久化，对标扣子/DeepSeek 网页版）。

- 会话/消息归属 token.sub，本人可见；管理员也不能看别人的对话（报价隐私）。
- /{id}/chat/stream：客户端只发新消息，历史由服务端取（窗口见 chat_store）。
  客户端中断（停止生成/断网）时，已生成的部分照样落库并标 stopped。
- 旧的无状态 /agent/chat(/stream) 保留不动，供脚本/API 调用方使用。
"""
import json
import logging
import queue
import threading

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import provider, runtime
from app.auth import current_identity
from app.config import get_settings
from app.db import get_db
from app.security import UserContext, get_current_user_context, record_access_log
from app.services import chat_store

_log = logging.getLogger("agent")
router = APIRouter(prefix="/agent/sessions", tags=["agent"])

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
) -> dict:
    s = _owned_or_404(db, ident, session_id)
    return {"id": s.id, "title": s.title,
            "items": chat_store.list_messages(db, s.id)}


# ---------- 会话级生成中枢（互斥 + 取消 + 可重连扇出）----------
# 同一会话同时只允许一轮生成（ChatGPT 语义）：并发生成会让消息 id 顺序与
# 对话顺序永久错乱、半截 checkpoint 被当完整历史回灌 LLM。
# RunHub：worker 把每个事件既存进 buffer 又扇出给所有订阅者 → 原始流断开后，
# 切回会话可经 /chat/attach 重新订阅：先回放 buffer(已生成部分)、再续实时流
# （连续逐字直播）。单进程 uvicorn（Dockerfile 无 --workers）下进程内注册表即可；
# 多进程部署需改 Redis pub/sub + DB 行锁。
_guard_lock = threading.Lock()


class _RunHub:
    """一轮生成的事件中枢：缓冲 + 多订阅者扇出 + 取消信号。"""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._buffer: list[dict] = []          # 本轮全部事件，供晚到订阅者回放
        self._subs: list["queue.Queue[dict | None]"] = []
        self._done = False

    def publish(self, ev: dict) -> None:
        with self._lock:
            self._buffer.append(ev)
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
    record_access_log(ctx, "chat_stream", "agent",
                      {"session_id": s.id, "q": req.message[:200]})

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
        configured = provider.is_configured()
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

        CHECKPOINT_DELTAS = 40
        buf: list[str] = []
        trace: list[dict] = []
        msg_id: int | None = None
        since_ckpt = 0
        finalized = False

        def _save(stopped: bool, final: bool = False) -> None:
            nonlocal msg_id, since_ckpt, finalized
            if finalized:
                return
            if final:
                finalized = True
            content = "".join(buf).strip()
            if not content and not trace:
                return
            try:
                msg_id = chat_store.save_assistant_progress(
                    sid, msg_id, content or "(无内容)", trace, stopped)
                since_ckpt = 0
            except Exception:  # noqa: BLE001 —— 落库失败不能炸生成
                _log.exception("checkpoint assistant message failed (session=%s)", sid)

        wdb = SessionLocal()
        try:
            for ev in runtime.run_stream(wdb, history, ctx):
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
        except Exception as exc:  # noqa: BLE001 —— 上游 LLM 错误：保留已生成部分
            _log.error("agent session stream failed: %r", exc)
            _save(stopped=True, final=True)
            hub.publish({"type": "error", "message": "AI 服务调用失败，请稍后重试"})
        finally:
            _save(stopped=True, final=True)
            wdb.close()
            release_session(sid)  # → hub.finish() 给所有订阅者发结束哨兵

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
