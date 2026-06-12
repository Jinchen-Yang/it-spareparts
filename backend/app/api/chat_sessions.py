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


def _owned_or_404(db: Session, ident: dict, session_id: int):
    s = chat_store.get_owned(db, ident["sub"], session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return s


@router.get("")
def list_sessions(
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
    return {"items": chat_store.list_sessions(db, ident["sub"])}


@router.post("")
def create_session(
    req: CreateSessionRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
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


@router.post("/{session_id}/chat/stream")
def chat_stream(
    session_id: int,
    req: SendMessageRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """会话内流式问答。SSE 事件与旧 /agent/chat/stream 一致，外加
    {type:"title", title} —— 首条消息后服务端定的会话标题。"""
    _require_agent_enabled()
    s = _owned_or_404(db, ident, session_id)
    record_access_log(ctx, "chat_stream", "agent",
                      {"session_id": s.id, "q": req.message[:200]})

    # 先落用户消息（即使后面中断，提问也不丢）；首条消息顺带定标题
    title_before = s.title
    chat_store.append_message(db, s, "user", req.message)
    history = chat_store.history_for_llm(db, s.id)

    def _sse(ev: dict) -> str:
        return f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

    sid = s.id  # 捕获纯 int：worker 线程不碰请求级 ORM 对象/会话

    # 生成与传输解耦（ChatGPT 语义）：agent 循环在独立线程跑到底——客户端
    # 关页面/点停止，服务端照样把本轮答案生成完并完整落库；HTTP 流只是
    # 从队列搬运事件。教训：sync generator 被客户端中断后既不被 close 也
    # 不再前进，靠 finally/沿途 checkpoint 落库都不可靠。
    def _worker(q: "queue.Queue[dict | None]") -> None:
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
                if ev.get("type") == "delta":
                    buf.append(ev.get("text") or "")
                    since_ckpt += 1
                    if since_ckpt >= CHECKPOINT_DELTAS:
                        _save(stopped=True)  # 中间态：若进程在此挂掉，状态即"被中断"
                elif ev.get("type") == "tool":
                    trace.append({"name": ev.get("name"), "args": ev.get("args")})
                    _save(stopped=True)
                elif ev.get("type") == "done":
                    _save(stopped=False, final=True)
                q.put(ev)
        except Exception as exc:  # noqa: BLE001 —— 上游 LLM 错误：保留已生成部分
            _log.error("agent session stream failed: %r", exc)
            _save(stopped=True, final=True)
            q.put({"type": "error", "message": "AI 服务调用失败，请稍后重试"})
        finally:
            _save(stopped=True, final=True)
            wdb.close()
            q.put(None)  # 结束哨兵

    def gen():
        if s.title != title_before:
            yield _sse({"type": "title", "title": s.title})
        if not provider.is_configured():
            chat_store.append_message(db, s, "assistant", _NOT_CONFIGURED_MSG)
            yield _sse({"type": "delta", "text": _NOT_CONFIGURED_MSG})
            yield _sse({"type": "done", "tool_calls": [], "configured": False})
            return

        q: "queue.Queue[dict | None]" = queue.Queue()  # 无界：断开后无人消费也不堵 worker
        threading.Thread(target=_worker, args=(q,), daemon=True,
                         name=f"agent-session-{sid}").start()
        while True:
            try:
                ev = q.get(timeout=600)  # 轮数受 llm_max_tool_iters 限制，600s 仅防僵尸
            except queue.Empty:
                yield _sse({"type": "error", "message": "生成超时，请重试"})
                return
            if ev is None:
                return
            yield _sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
