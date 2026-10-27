"""通用待办与只追加时间线；主体仅以类型和 ID 标识，不依赖业务表。"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class WorkItem(Base):
    __tablename__ = "work_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[int] = mapped_column(BigInteger)
    pool_key: Mapped[str] = mapped_column(String(32))
    assignee_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sys_user.id"))
    claimed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("sys_user.id"))
    claimed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("sys_user.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'done', 'declined', 'cancelled')",
            name="ck_work_item_status",
        ),
        CheckConstraint("version >= 1", name="ck_work_item_version"),
        Index("ix_work_item_assignee_status", "assignee_id", "status"),
        Index(
            "ix_work_item_pool_status", "pool_key", "status",
            postgresql_where=text("assignee_id IS NULL"),
        ),
        Index("ix_work_item_subject", "subject_type", "subject_id"),
        Index(
            "uq_work_item_open_subject_assignee",
            "subject_type", "subject_id", "assignee_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'in_progress')"),
        ),
    )


class SubjectEvent(Base):
    __tablename__ = "subject_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[int] = mapped_column(BigInteger)
    work_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("work_item.id"))
    kind: Mapped[str] = mapped_column(String(48))
    actor: Mapped[int | None] = mapped_column(Integer, ForeignKey("sys_user.id"))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_subject_event_subject_id", "subject_type", "subject_id", "id"),
    )
