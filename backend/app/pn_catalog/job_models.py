"""Durable PG queue; inputs never change when a logical job is retried."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
TZDateTime = DateTime(timezone=True)


class CatalogJob(Base):
    __tablename__ = "pn_catalog_job"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(64))
    queue: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(256))
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    resource_generation: Mapped[int] = mapped_column(BigInteger)
    input_revision: Mapped[int] = mapped_column(BigInteger)
    deduplication_key: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    context: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default="pending", default="pending")
    attempt: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    attempt_in_cycle: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    revision: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5", default=5)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    available_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    error_code: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[dict | None] = mapped_column(JSONB)
    execution_usage: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    __table_args__ = (
        UniqueConstraint("kind", "deduplication_key", name="uq_pn_catalog_job_dedupe"),
        CheckConstraint("status IN ('pending','running','succeeded','failed','cancelled','stale')", name="ck_pn_catalog_job_status"),
        CheckConstraint("revision >= 0 AND resource_generation >= 0 AND input_revision >= 0 AND attempt >= 0 AND attempt_in_cycle >= 0 AND retry_count >= 0 AND lease_epoch >= 0 AND max_attempts BETWEEN 1 AND 20", name="ck_pn_catalog_job_counts"),
        CheckConstraint("(status = 'running' AND lease_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'running' AND lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL)", name="ck_pn_catalog_job_lease"),
        Index("ix_pn_catalog_job_pending", "queue", "available_at", "created_at", postgresql_where=text("status = 'pending'")),
        Index("ix_pn_catalog_job_expired", "lease_expires_at", postgresql_where=text("status = 'running'")),
        Index("ix_pn_catalog_job_resource", "resource_type", "resource_id", "created_at"),
    )


class CatalogJobEvent(Base):
    __tablename__ = "pn_catalog_job_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("pn_catalog_job.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer)
    lease_epoch: Mapped[int] = mapped_column(BigInteger)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    code: Mapped[str | None] = mapped_column(String(128))
    context: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (Index("ix_pn_catalog_job_event_job", "job_id", "id"),)
