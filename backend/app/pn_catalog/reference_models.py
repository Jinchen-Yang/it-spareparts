"""Stable external PN references; labels are never identity joins."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExternalReference(Base):
    __tablename__ = "pn_catalog_external_reference"
    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), index=True)
    identity_revision: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    decision_ref: Mapped[str] = mapped_column(String(256))
    audit_id: Mapped[int] = mapped_column(ForeignKey("pn_catalog_audit.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("version >= 1 AND identity_revision >= 0", name="ck_pn_reference_versions"),)
