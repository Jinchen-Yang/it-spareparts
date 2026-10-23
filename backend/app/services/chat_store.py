"""对话持久化服务（平台化 P1）。

统一授权下会话归属稳定 owner_user_id；旧兼容实例沿用 owner_sub。所有读写校验归属，
否则横向越权（A 看 B 的报价对话 = 客户/价格泄露）。
LLM 上下文窗口在服务端截取（最近 N 条），客户端不再持有历史。
"""
import re
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage, ChatSession
from app.authz.provenance import AgentAccess, unavailable, validate_provenance
from app.authz import service


def _access(db, access):
    access = access or AgentAccess(db.get_bind(), None)
    access.check()
    return access


def _owned_access(db, session_id, access, *, write=False):
    access = _access(db, access)
    if access.authoritative:
        row = (db.scalar(select(ChatSession).where(ChatSession.id == session_id)
               .with_for_update().execution_options(populate_existing=True)) if write
               else db.get(ChatSession, session_id, populate_existing=True))
        if row is None or row.archived or row.owner_user_id != access.snapshot()["principal_id"]:
            raise unavailable()
        if write:
            service.lock_actor(db, access.identity)
            validate_provenance(db, access.identity, access.snapshot())
    return access

MAX_SESSIONS_LISTED = 100
HISTORY_WINDOW = 20          # 回灌 LLM 的最近消息条数（与旧前端 slice(-20) 口径一致）
HISTORY_CHAR_CAP = 8000      # 单条消息回灌上限，防超长答复撑爆上下文

_FILE_PREFIX = re.compile(r"^\[已上传文件[^\]]*\]\n*")


def list_sessions(db: Session, sub: str, *, access=None) -> list[dict]:
    access = _access(db, access)
    rows = db.scalars(
        select(ChatSession)
        .where(ChatSession.owner_user_id == access.snapshot()["principal_id"] if access.authoritative else ChatSession.owner_sub == sub, ChatSession.archived.is_(False))
        .order_by(ChatSession.updated_at.desc())
        .limit(MAX_SESSIONS_LISTED)
    ).all()
    return [{"id": s.id, "title": s.title, "updated_at": s.updated_at} for s in rows]


def create_session(db: Session, sub: str, title: str | None = None, *, access=None) -> ChatSession:
    access = _access(db, access)
    access.mark_used()
    if access.authoritative:
        service.lock_actor(db, access.identity)
    s = ChatSession(owner_sub=access.identity.username if access.authoritative else sub, owner_user_id=access.snapshot()["principal_id"] if access.authoritative else None, title=(title or "新对话")[:80])
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def get_owned(db: Session, sub: str, session_id: int, *, access=None) -> ChatSession | None:
    access = _access(db, access)
    s = db.get(ChatSession, session_id, populate_existing=True)
    if s is None or s.archived:
        return None
    if access.authoritative:
        return s if s.owner_user_id == access.snapshot()["principal_id"] else None
    return s if s.owner_sub == sub else None


def rename_session(db: Session, session: ChatSession, title: str, *, access=None) -> None:
    _owned_access(db, session.id, access, write=True)
    session.title = title.strip()[:80] or session.title
    db.commit()


def delete_session(db: Session, session: ChatSession, *, access=None) -> None:
    _owned_access(db, session.id, access, write=True)
    # 显式删消息：ondelete=CASCADE 在库层兜底，但 ORM 级 delete 不触发关系级联
    db.execute(delete(ChatMessage).where(ChatMessage.session_id == session.id))
    db.delete(session)
    db.commit()


def list_messages(db: Session, session_id: int, *, access=None) -> list[dict]:
    access = _owned_access(db, session_id, access)
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id)
    ).all()
    if access.authoritative:
        for m in rows:
            if m.role != "user":
                access.inherit(m.authorization_provenance)
        access.check()
    return [
        {"id": m.id, "role": m.role, "content": m.content,
         "tools": m.tools or [], "stopped": m.stopped, "created_at": m.created_at}
        for m in rows
    ]


def append_message(db: Session, session: ChatSession, role: str, content: str,
                   tools: list | None = None, stopped: bool = False, *, access=None) -> ChatMessage:
    access = _access(db, access)
    access.mark_used()
    access = _owned_access(db, session.id, access, write=True)
    m = ChatMessage(session_id=session.id, role=role, content=content,
                    tools=tools or None, stopped=stopped,
                    authorization_provenance=access.snapshot() if role != "user" else None)
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
                            tools: list | None, stopped: bool, *, access=None) -> int:
    """流式 checkpoint：首次调用插入 assistant 行，之后按 id 原地更新。

    用独立短连接会话——流式响应被客户端中断后，请求级 db 会话的生命周期
    不可依赖（FastAPI 依赖可能已 teardown），checkpoint 必须自给自足。
    返回消息 id 供后续更新。
    """
    from app.db import SessionLocal

    with SessionLocal() as db:
        access = _access(db, access)
        access.mark_used()
        access = _owned_access(db, session_id, access, write=True)
        provenance = access.snapshot()
        if msg_id is None:
            m = ChatMessage(session_id=session_id, role="assistant",
                            content=content, tools=tools or None, stopped=stopped, authorization_provenance=provenance)
            db.add(m)
            db.flush()
            msg_id = m.id
        else:
            updated = db.execute(
                ChatMessage.__table__.update()
                .where(ChatMessage.id == msg_id, ChatMessage.session_id == session_id, ChatMessage.role == "assistant")
                .values(content=content, tools=tools or None, stopped=stopped, authorization_provenance=provenance)
            )
            if updated.rowcount != 1:
                raise unavailable()
        db.execute(
            ChatSession.__table__.update()
            .where(ChatSession.id == session_id)
            .values(updated_at=datetime.now(timezone.utc))
        )
        db.commit()
        return msg_id


def history_for_llm(db: Session, session_id: int, *, access=None) -> list[dict]:
    """取最近 HISTORY_WINDOW 条 user/assistant 消息，按时间正序回灌模型。"""
    access = _owned_access(db, session_id, access)
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id,
               ChatMessage.role.in_(("user", "assistant")))
        .order_by(ChatMessage.id.desc())
        .limit(HISTORY_WINDOW)
    ).all()
    if access.authoritative:
        for m in rows:
            if m.role != "user":
                access.inherit(m.authorization_provenance)
        access.check()
    return [
        {"role": m.role, "content": m.content[:HISTORY_CHAR_CAP]}
        for m in reversed(rows)
    ]
