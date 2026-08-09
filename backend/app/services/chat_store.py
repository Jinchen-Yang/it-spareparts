"""对话持久化服务（平台化 P1）。

会话归属 owner_sub（token 的 sub）；所有读写都必须先 get_owned 校验归属，
否则横向越权（A 看 B 的报价对话 = 客户/价格泄露）。
LLM 上下文窗口在服务端截取（最近 N 条），客户端不再持有历史。
"""
import re
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage, ChatSession

MAX_SESSIONS_LISTED = 100
HISTORY_WINDOW = 20          # 回灌 LLM 的最近消息条数（与旧前端 slice(-20) 口径一致）
HISTORY_CHAR_CAP = 8000      # 单条消息回灌上限，防超长答复撑爆上下文

_FILE_PREFIX = re.compile(r"^\[已上传文件[^\]]*\]\n*")


def _content_free_tool_calls(tool_calls: list | None) -> list[dict]:
    """Normalize tool traces at the persistence/API boundary without values.

    Runtime normally supplies an already-safe summary.  We still sanitize it here so a
    future caller cannot accidentally persist raw customer queries, workbook cells or
    report rows.  Legacy rows with raw ``args`` are converted on read as well.
    """
    from app.agent import tools as agent_tools

    safe_calls: list[dict] = []
    for item in tool_calls or []:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name in agent_tools._REGISTRY else "unknown"
        payload = item.get("args")
        if (
            isinstance(payload, dict)
            and {"outcome", "arg_count", "arg_keys"}.issubset(payload)
        ):
            summary = agent_tools._sanitize_tool_audit(name, payload)
        else:
            summary = agent_tools._safe_tool_audit(
                name,
                payload if isinstance(payload, dict) else {},
                "recorded",
            )
        safe_calls.append({"name": name, "args": summary})
    return safe_calls


def list_sessions(db: Session, sub: str) -> list[dict]:
    rows = db.scalars(
        select(ChatSession)
        .where(ChatSession.owner_sub == sub, ChatSession.archived.is_(False))
        .order_by(ChatSession.updated_at.desc())
        .limit(MAX_SESSIONS_LISTED)
    ).all()
    return [{"id": s.id, "title": s.title, "updated_at": s.updated_at} for s in rows]


def create_session(db: Session, sub: str, title: str | None = None) -> ChatSession:
    s = ChatSession(owner_sub=sub, title=(title or "新对话")[:80])
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def get_owned(db: Session, sub: str, session_id: int) -> ChatSession | None:
    s = db.get(ChatSession, session_id)
    if s is None or s.owner_sub != sub or s.archived:
        return None
    return s


def rename_session(db: Session, session: ChatSession, title: str) -> None:
    session.title = title.strip()[:80] or session.title
    db.commit()


def delete_session(db: Session, session: ChatSession) -> None:
    # 显式删消息：ondelete=CASCADE 在库层兜底，但 ORM 级 delete 不触发关系级联
    db.execute(delete(ChatMessage).where(ChatMessage.session_id == session.id))
    db.delete(session)
    db.commit()


def list_messages(db: Session, session_id: int) -> list[dict]:
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id)
    ).all()
    return [
        {"id": m.id, "role": m.role, "content": m.content,
         "tools": _content_free_tool_calls(m.tools),
         "stopped": m.stopped, "created_at": m.created_at}
        for m in rows
    ]


def append_message(db: Session, session: ChatSession, role: str, content: str,
                   tools: list | None = None, stopped: bool = False) -> ChatMessage:
    safe_tools = _content_free_tool_calls(tools)
    m = ChatMessage(session_id=session.id, role=role, content=content,
                    tools=safe_tools or None, stopped=stopped)
    db.add(m)
    # 首条用户消息自动定标题（剥掉文件注入前缀，与旧前端口径一致）
    if role == "user" and session.title == "新对话":
        plain = _FILE_PREFIX.sub("", content).strip()
        session.title = (plain[:20] or "文件处理")
    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(m)
    return m


def save_assistant_progress(session_id: int, msg_id: int | None, content: str,
                            tools: list | None, stopped: bool) -> int:
    """流式 checkpoint：首次调用插入 assistant 行，之后按 id 原地更新。

    用独立短连接会话——流式响应被客户端中断后，请求级 db 会话的生命周期
    不可依赖（FastAPI 依赖可能已 teardown），checkpoint 必须自给自足。
    返回消息 id 供后续更新。
    """
    from app.db import SessionLocal

    safe_tools = _content_free_tool_calls(tools)
    with SessionLocal() as db:
        if msg_id is None:
            m = ChatMessage(session_id=session_id, role="assistant",
                            content=content, tools=safe_tools or None, stopped=stopped)
            db.add(m)
            db.flush()
            msg_id = m.id
        else:
            db.execute(
                ChatMessage.__table__.update()
                .where(ChatMessage.id == msg_id)
                .values(content=content, tools=safe_tools or None, stopped=stopped)
            )
        db.execute(
            ChatSession.__table__.update()
            .where(ChatSession.id == session_id)
            .values(updated_at=datetime.now(timezone.utc))
        )
        db.commit()
        return msg_id


def history_for_llm(db: Session, session_id: int) -> list[dict]:
    """取最近 HISTORY_WINDOW 条 user/assistant 消息，按时间正序回灌模型。"""
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id,
               ChatMessage.role.in_(("user", "assistant")))
        .order_by(ChatMessage.id.desc())
        .limit(HISTORY_WINDOW)
    ).all()
    return [
        {"role": m.role, "content": m.content[:HISTORY_CHAR_CAP]}
        for m in reversed(rows)
    ]
