"""Persistence owned by pn_catalog. Existing dim_part IDs remain authoritative."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class PartState(Base):
    __tablename__ = "pn_catalog_state"
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), primary_key=True)
    identity_revision: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    profile_revision: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    availability_revision: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    availability: Mapped[str] = mapped_column(String(16), server_default="active", default="active")
    manufacturer: Mapped[str | None] = mapped_column(String(256))
    field_versions: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
    deleted_specs: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    field_provenance: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
    __table_args__ = (
        CheckConstraint("availability IN ('active','inactive')", name="ck_pn_catalog_availability"),
        CheckConstraint("identity_revision >= 0 AND profile_revision >= 0 AND availability_revision >= 0",
                        name="ck_pn_catalog_revisions"),
    )


class AuditEvent(Base):
    __tablename__ = "pn_catalog_audit"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    action: Mapped[str] = mapped_column(String(64))
    principal_id: Mapped[str] = mapped_column(String(256))
    client_id: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    actor: Mapped[dict] = mapped_column(JSONB)
    context: Mapped[dict] = mapped_column(JSONB)
    authorization: Mapped[dict] = mapped_column(JSONB)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    before_revision: Mapped[int] = mapped_column(BigInteger)
    after_revision: Mapped[int] = mapped_column(BigInteger)
    changes: Mapped[dict] = mapped_column(JSONB)
    revisions: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (Index("ix_pn_catalog_audit_part_id", "part_id", "id"),)


class CommandReceipt(Base):
    __tablename__ = "pn_catalog_receipt"
    principal_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64))
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    payload_hash: Mapped[str] = mapped_column(String(64))
    applied_revision: Mapped[int] = mapped_column(BigInteger)
    audit_id: Mapped[int] = mapped_column(ForeignKey("pn_catalog_audit.id"))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
