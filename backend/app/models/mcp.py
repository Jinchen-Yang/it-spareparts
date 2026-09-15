"""Durable MCP credentials, workflow records and append-only audit events."""

from datetime import datetime

from sqlalchemy import Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class McpCredential(Base):
    __tablename__ = "mcp_credential"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list] = mapped_column(JSONB)
    auth_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class McpRecord(Base):
    __tablename__ = "mcp_record"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(24), default="ready")
    key: Mapped[str | None] = mapped_column(String(160))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("owner", "kind", "key", name="uq_mcp_record_owner_kind_key"),
        Index("ix_mcp_record_owner_kind", "owner", "kind"),
        Index("ix_mcp_record_job_state", "kind", "state"),
    )


class McpAuditEvent(Base):
    __tablename__ = "mcp_audit_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    owner: Mapped[str] = mapped_column(String(64))
    credential_id: Mapped[str | None] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(80))
    stage: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (Index("ix_mcp_audit_owner_time", "owner", "created_at"),)
