"""Agent 会话与消息。

新授权以稳定 owner_user_id 和服务端消息来源校验归属；owner_sub 只供旧兼容协议
及历史显示使用，不能替代规范来源。旧消息缺少来源时不得在新权威下重新提供。
"""
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class ChatSession(Base):
    __tablename__ = "chat_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_sub: Mapped[str] = mapped_column(String(64))
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sys_user.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(80), default="新对话", server_default="新对话")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_chat_session_owner_updated", "owner_sub", "updated_at"),
        Index("ix_chat_session_owner_user_updated", "owner_user_id", "updated_at"),
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
    authorization_provenance: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    stopped: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_chat_message_session", "session_id", "id"),
    )
