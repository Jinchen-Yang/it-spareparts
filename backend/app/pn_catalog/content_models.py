"""PN condition content and immutable candidate evidence; no SKU/stock ownership."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, BigInteger, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
TZDateTime = DateTime(timezone=True)


class ConditionContent(Base):
    __tablename__ = "pn_catalog_content"
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), primary_key=True)
    grade: Mapped[str] = mapped_column(String(8), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    title: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    field_versions: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    protected_fields: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    field_provenance: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    legacy_source_namespace: Mapped[str | None] = mapped_column(String(128))
    legacy_source_record_id: Mapped[str | None] = mapped_column(String(256))
    legacy_source_revision: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        CheckConstraint("grade IN ('good','bad')", name="ck_pn_content_grade"),
        CheckConstraint("revision >= 0", name="ck_pn_content_revision"),
        CheckConstraint("status IN ('pending','ai_filled','confirmed','needs_review')", name="ck_pn_content_status"),
        UniqueConstraint("legacy_source_namespace", "legacy_source_record_id", name="uq_pn_content_legacy_source"),
    )


class ContentCandidate(Base):
    __tablename__ = "pn_catalog_candidate"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    resource: Mapped[str] = mapped_column(String(16))
    grade: Mapped[str | None] = mapped_column(String(8))
    identity_revision: Mapped[int] = mapped_column(BigInteger)
    base_revision: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    proposal: Mapped[dict] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(256))
    adopted_fields: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    decisions: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (
        CheckConstraint("resource IN ('profile','content')", name="ck_pn_candidate_resource"),
        CheckConstraint("(resource = 'profile' AND grade IS NULL) OR (resource = 'content' AND grade IN ('good','bad'))",
                        name="ck_pn_candidate_grade"),
        CheckConstraint("revision >= 1 AND identity_revision >= 0 AND base_revision >= 0", name="ck_pn_candidate_revision"),
        CheckConstraint("status IN ('pending','partially_adopted','adopted','rejected')", name="ck_pn_candidate_status"),
        Index("ix_pn_candidate_part_created", "part_id", "created_at", "id"),
    )
