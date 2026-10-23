"""Transport trust bindings, not business grants or a parallel role system."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CatalogSubjectBinding(Base):
    __tablename__ = "sys_catalog_subject_binding"
    issuer: Mapped[str] = mapped_column(String(256), primary_key=True)
    subject: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    updated_by: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_catalog_subject_version"),
    )


class CatalogProofNonce(Base):
    __tablename__ = "sys_catalog_proof_nonce"
    issuer: Mapped[str] = mapped_column(String(256), primary_key=True)
    nonce: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
