"""Additive sales replenishment-cart Beta facts.

This module is deliberately isolated from inventory, purchase and maintenance facts.
Submitting or reviewing an application records intent only; it never mutates stock.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class ReplenishmentApplication(Base):
    """Stable owner-scoped application identity across immutable submissions."""

    __tablename__ = "replenishment_application"

    application_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    owner_username: Mapped[str] = mapped_column(
        ForeignKey("sys_user.username"), nullable=False
    )
    owner_display_name: Mapped[str | None] = mapped_column(String(128))
    # Business-system salesperson mapping is distinct from a UI nickname. The
    # snapshot prevents later profile edits from changing an approved export.
    salesperson_name_snapshot: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    latest_version_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','submitted','needs_revision','approved')",
            name="ck_replenishment_application_status",
        ),
        CheckConstraint(
            "latest_version_no >= 1 AND version >= 1",
            name="ck_replenishment_application_versions",
        ),
        CheckConstraint(
            "char_length(btrim(application_no)) > 0 "
            "AND char_length(btrim(owner_username)) > 0",
            name="ck_replenishment_application_identity",
        ),
        Index(
            "ix_replenishment_application_owner_updated",
            "owner_username",
            "updated_at",
            "application_id",
        ),
        Index(
            "ix_replenishment_application_status_updated",
            "status",
            "updated_at",
            "application_id",
        ),
    )


class ReplenishmentApplicationVersion(Base):
    """A mutable draft that becomes permanently immutable when submitted."""

    __tablename__ = "replenishment_application_version"

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("replenishment_application.application_id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("replenishment_application_version.version_id")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    warehouse: Mapped[str | None] = mapped_column(String(64))
    request_note: Mapped[str | None] = mapped_column(Text)
    content_digest: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    submitted_by: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("version_no >= 1", name="ck_replenishment_version_no"),
        CheckConstraint(
            "status IN ('draft','submitted')",
            name="ck_replenishment_version_status",
        ),
        CheckConstraint(
            "(status = 'draft' AND content_digest IS NULL "
            "AND submitted_by IS NULL AND submitted_at IS NULL) OR "
            "(status = 'submitted' AND content_digest ~ '^[a-f0-9]{64}$' "
            "AND char_length(btrim(submitted_by)) > 0 AND submitted_at IS NOT NULL "
            "AND char_length(btrim(warehouse)) > 0)",
            name="ck_replenishment_version_submission_state",
        ),
        CheckConstraint(
            "char_length(btrim(created_by)) > 0",
            name="ck_replenishment_version_creator",
        ),
        UniqueConstraint(
            "application_id", "version_no", name="uq_replenishment_application_version"
        ),
    )


class ReplenishmentApplicationLine(Base):
    """One PN/quantity row plus the price-and-pool evidence frozen at submit."""

    __tablename__ = "replenishment_application_line"

    line_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 同一选购意图跨版本稳定；换 PN 也沿用，审核反馈不靠 PN 文本猜对应关系。
    request_line_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("replenishment_application_version.version_id"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_line_id: Mapped[str | None] = mapped_column(
        ForeignKey("replenishment_application_line.line_id")
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    pn_std: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(128))
    unit: Mapped[str | None] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    special_note: Mapped[str | None] = mapped_column(Text)
    pool_group_id: Mapped[int | None] = mapped_column(Integer)
    pool_name: Mapped[str | None] = mapped_column(String(128))
    pool_version: Mapped[int | None] = mapped_column(Integer)
    price_window_from: Mapped[date] = mapped_column(Date, nullable=False)
    price_window_to: Mapped[date] = mapped_column(Date, nullable=False)
    price_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    purchase_stats_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sales_stats_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("line_no >= 1", name="ck_replenishment_line_no"),
        CheckConstraint(
            "quantity > 0 AND quantity <= 999999.999",
            name="ck_replenishment_line_quantity",
        ),
        CheckConstraint(
            "price_window_from <= price_window_to AND price_window_to <= price_as_of",
            name="ck_replenishment_line_window",
        ),
        CheckConstraint(
            "evidence_digest ~ '^[a-f0-9]{64}$'",
            name="ck_replenishment_line_evidence_digest",
        ),
        CheckConstraint(
            "(pool_group_id IS NULL AND pool_name IS NULL AND pool_version IS NULL) OR "
            "(pool_group_id IS NOT NULL AND pool_version >= 1)",
            name="ck_replenishment_line_pool_snapshot",
        ),
        UniqueConstraint("version_id", "line_no", name="uq_replenishment_line_no"),
        UniqueConstraint(
            "version_id", "request_line_id", name="uq_replenishment_request_line"
        ),
        UniqueConstraint("version_id", "part_id", name="uq_replenishment_line_part"),
    )


class ReplenishmentReview(Base):
    """Idempotent immutable review-result envelope supplied by a controlled caller."""

    __tablename__ = "replenishment_review"

    review_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("replenishment_application_version.version_id"),
        nullable=False,
        unique=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(128))
    summary_note: Mapped[str | None] = mapped_column(Text)
    approved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "approved_count >= 0 AND rejected_count >= 0 "
            "AND approved_count + rejected_count > 0",
            name="ck_replenishment_review_counts",
        ),
        CheckConstraint(
            "payload_digest ~ '^[a-f0-9]{64}$' "
            "AND char_length(btrim(idempotency_key)) >= 8 "
            "AND char_length(btrim(reviewed_by)) > 0",
            name="ck_replenishment_review_identity",
        ),
        Index("ix_replenishment_review_time", "reviewed_at", "review_id"),
    )


class ReplenishmentReviewLine(Base):
    """One approved/rejected outcome; every submitted line must receive exactly one."""

    __tablename__ = "replenishment_review_line"

    review_line_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("replenishment_review.review_id"), nullable=False
    )
    version_line_id: Mapped[str] = mapped_column(
        ForeignKey("replenishment_application_line.line_id"),
        nullable=False,
        unique=True,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved','rejected')",
            name="ck_replenishment_review_line_decision",
        ),
        CheckConstraint(
            "decision = 'approved' OR char_length(btrim(reason)) > 0",
            name="ck_replenishment_review_line_reason",
        ),
        Index("ix_replenishment_review_line_review", "review_id", "version_line_id"),
    )


class ReplenishmentAuditEvent(Base):
    """Append-only business audit independent of best-effort access logs."""

    __tablename__ = "replenishment_audit_event"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("replenishment_application.application_id"), nullable=False
    )
    version_id: Mapped[str | None] = mapped_column(
        ForeignKey("replenishment_application_version.version_id")
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    operated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('application_created','draft_updated','line_added','line_updated',"
            "'line_removed','version_submitted','review_recorded','revision_started',"
            "'manual_exported','wbdd_draft_exported')",
            name="ck_replenishment_audit_action",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) > 0 AND char_length(btrim(operated_by)) > 0",
            name="ck_replenishment_audit_actor_reason",
        ),
        Index(
            "ix_replenishment_audit_application_time",
            "application_id",
            "operated_at",
            "event_id",
        ),
    )
