"""Lossless migration staging; unresolved evidence stays discoverable and read-only."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

TZDateTime = DateTime(timezone=True)


class MigrationBatch(Base):
    __tablename__ = "pn_catalog_migration_batch"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    source_schema: Mapped[str] = mapped_column(String(128))
    writer_fence_ref: Mapped[str] = mapped_column(String(256))
    record_count: Mapped[int] = mapped_column()
    # Explicit states prevent archive-only success from being reported as cutover readiness.
    status: Mapped[str] = mapped_column(String(24), default="archived", server_default="archived")
    principal_id: Mapped[str] = mapped_column(String(256))
    audit_id: Mapped[int | None] = mapped_column(ForeignKey("pn_catalog_audit.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (CheckConstraint("status IN ('archived','materialized')", name="ck_pn_migration_batch_status"),)


class LegacySourceRecord(Base):
    __tablename__ = "pn_catalog_legacy_record"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(256))
    source_pn_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    source_revision: Mapped[str | None] = mapped_column(String(256))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("pn_catalog_migration_batch.id"))
    target_part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    mapping: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    materialized_ref: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("namespace", "kind", "source_id", name="uq_pn_legacy_source_record"),
        CheckConstraint("state IN ('pending','materialized','unresolved','conflict','historical')", name="ck_pn_legacy_record_state"),
        Index("ix_pn_legacy_discovery", "namespace", "state", "kind", "id"),
    )
