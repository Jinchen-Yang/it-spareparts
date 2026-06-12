"""智能体对话持久化：会话 / 消息（平台化 P1）。

owner_sub 存 token 的 sub（用户名）而非 FK sys_user.id——登录层存在
"sys_user 无此账号回退共享口令"的兼容路径（auth.py），这类身份没有
sys_user 行；用字符串归属对两种路径一致。会话只对 owner 本人可见。
"""
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class ChatSession(Base):
    __tablename__ = "chat_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_sub: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(80), default="新对话", server_default="新对话")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_chat_session_owner_updated", "owner_sub", "updated_at"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_session.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text)
    # assistant 轮的工具轨迹 [{name,args}]；none_as_null：无轨迹存 SQL NULL 而非 JSON 'null'
    tools: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    stopped: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_chat_message_session", "session_id", "id"),
    )
