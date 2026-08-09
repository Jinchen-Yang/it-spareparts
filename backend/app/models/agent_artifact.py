"""Immutable AI input/output artifact metadata."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class AgentArtifact(Base):
    """Server-owned metadata for an immutable file stored by the Agent platform."""

    __tablename__ = "agent_artifact"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    owner_sub: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    access_scope: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    extra_meta: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared', 'validating', 'ready', 'failed', 'expired')",
            name="ck_agent_artifact_status",
        ),
        CheckConstraint(
            "kind IN ('upload', 'generated')",
            name="ck_agent_artifact_kind",
        ),
        CheckConstraint(
            "sensitivity IN ('low', 'medium', 'high', 'critical')",
            name="ck_agent_artifact_sensitivity",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_agent_artifact_size"),
        CheckConstraint("char_length(sha256) = 64", name="ck_agent_artifact_sha256"),
        CheckConstraint(
            "char_length(btrim(owner_sub)) > 0",
            name="ck_agent_artifact_owner",
        ),
        CheckConstraint(
            "char_length(btrim(filename)) > 0",
            name="ck_agent_artifact_filename",
        ),
        CheckConstraint(
            "char_length(btrim(storage_key)) > 0",
            name="ck_agent_artifact_storage_key",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_agent_artifact_expiry",
        ),
        Index("ix_agent_artifact_owner_created", "owner_sub", "created_at"),
        Index("ix_agent_artifact_status_expiry", "status", "expires_at"),
    )
