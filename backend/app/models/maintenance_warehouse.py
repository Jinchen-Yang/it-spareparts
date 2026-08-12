"""Warehouse document facts and fail-closed association ambiguities (#209)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Qty, TZDateTime


class MaintenanceWarehouseImportBatch(Base):
    __tablename__ = "maintenance_warehouse_import_batch"

    import_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    version_state: Mapped[str] = mapped_column(String(24), nullable=False)
    header_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    header_pairs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ambiguity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    applied_by: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("source_file_hash ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_batch_hash"),
        CheckConstraint("header_signature ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_batch_header_hash"),
        CheckConstraint("version_state IN ('known', 'unknown_version')", name="ck_maintenance_wh_batch_version_state"),
        CheckConstraint("status = 'applied'", name="ck_maintenance_wh_batch_status"),
        CheckConstraint("document_count >= 0 AND line_count >= 0 AND ambiguity_count >= 0", name="ck_maintenance_wh_batch_counts"),
        CheckConstraint("char_length(btrim(reason)) > 0", name="ck_maintenance_wh_batch_reason"),
        CheckConstraint("char_length(btrim(applied_by)) > 0", name="ck_maintenance_wh_batch_operator"),
        UniqueConstraint(
            "source_file_hash", "adapter_version",
            name="uq_maintenance_wh_batch_file_adapter",
        ),
        Index("ix_maintenance_wh_batch_applied", "applied_at", "import_id"),
    )


class MaintenanceWarehouseDocument(Base):
    __tablename__ = "maintenance_warehouse_document"

    document_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_no: Mapped[str] = mapped_column(String(128), nullable=False)
    document_date: Mapped[date | None] = mapped_column(Date)
    raw_status: Mapped[str | None] = mapped_column(String(128))
    normalized_status: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_fields_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    first_import_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_warehouse_import_batch.import_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("document_type IN ('shipment', 'return', 'receipt')", name="ck_maintenance_wh_document_type"),
        CheckConstraint("normalized_status IN ('confirmed', 'pending', 'void', 'unknown')", name="ck_maintenance_wh_document_status"),
        CheckConstraint("raw_fingerprint ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_document_fingerprint"),
        UniqueConstraint(
            "document_type", "document_no",
            name="uq_maintenance_wh_document_no",
        ),
        Index("ix_maintenance_wh_document_source", "document_type", "source_document_id"),
        Index("ix_maintenance_wh_document_date", "document_type", "document_date"),
    )


class MaintenanceWarehouseDocumentLine(Base):
    __tablename__ = "maintenance_warehouse_document_line"

    line_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_warehouse_document.document_id"), nullable=False
    )
    source_line_id: Mapped[str] = mapped_column(String(128), nullable=False)
    line_no: Mapped[int | None] = mapped_column(Integer)
    pn: Mapped[str | None] = mapped_column(String(256))
    sn: Mapped[str | None] = mapped_column(String(256))
    self_code: Mapped[str | None] = mapped_column(String(256))
    quantity: Mapped[Decimal | None] = mapped_column(Qty)
    raw_fields_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    first_import_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_warehouse_import_batch.import_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("line_no IS NULL OR line_no >= 1", name="ck_maintenance_wh_line_no"),
        CheckConstraint("quantity IS NULL OR (quantity >= 0 AND quantity < 1000000000000)", name="ck_maintenance_wh_line_qty"),
        CheckConstraint("raw_fingerprint ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_line_fingerprint"),
        UniqueConstraint("document_id", "source_line_id", name="uq_maintenance_wh_line_source"),
        Index("ix_maintenance_wh_line_pn", "pn"),
        Index("ix_maintenance_wh_line_sn", "sn"),
        Index("ix_maintenance_wh_line_self_code", "self_code"),
    )


class MaintenanceWarehouseDocumentLink(Base):
    __tablename__ = "maintenance_warehouse_document_link"

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_warehouse_document.document_id"), nullable=False
    )
    line_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_warehouse_document_line.line_id")
    )
    link_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    stable_key_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    stable_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    supersedes_link_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_warehouse_document_link.link_id"), unique=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("link_kind IN ('maintenance_order', 'project', 'site_issue', 'bad_return', 'part', 'warehouse_document')", name="ck_maintenance_wh_link_kind"),
        CheckConstraint("target_type IN ('maintenance_order', 'maintenance_project', 'maintenance_site_issue', 'maintenance_bad_return', 'dim_part', 'warehouse_document')", name="ck_maintenance_wh_link_target_type"),
        CheckConstraint(
            "(link_kind = 'maintenance_order' AND target_type = 'maintenance_order') OR "
            "(link_kind = 'project' AND target_type = 'maintenance_project') OR "
            "(link_kind = 'site_issue' AND target_type = 'maintenance_site_issue') OR "
            "(link_kind = 'bad_return' AND target_type = 'maintenance_bad_return') OR "
            "(link_kind = 'part' AND target_type = 'dim_part') OR "
            "(link_kind = 'warehouse_document' AND target_type = 'warehouse_document')",
            name="ck_maintenance_wh_link_target_matrix",
        ),
        CheckConstraint("source IN ('automatic', 'manual')", name="ck_maintenance_wh_link_source"),
        CheckConstraint("status IN ('active', 'superseded')", name="ck_maintenance_wh_link_status"),
        CheckConstraint(
            "(status = 'active' AND ((version = 1 AND supersedes_link_id IS NULL) OR "
            "(version >= 2 AND supersedes_link_id IS NOT NULL))) OR "
            "(status = 'superseded' AND version >= 2)",
            name="ck_maintenance_wh_link_supersession",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_wh_link_version"),
        CheckConstraint("stable_key_hash ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_link_key_hash"),
        CheckConstraint("char_length(btrim(reason)) > 0", name="ck_maintenance_wh_link_reason"),
        CheckConstraint("char_length(btrim(operated_by)) > 0", name="ck_maintenance_wh_link_operator"),
        Index(
            "uq_maintenance_wh_link_target",
            document_id,
            func.coalesce(line_id, ""),
            link_kind,
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_maintenance_wh_link_document", "document_id", "line_id", "link_kind"),
        Index("ix_maintenance_wh_link_target", "target_type", "target_id"),
    )


class MaintenanceWarehouseAmbiguity(Base):
    __tablename__ = "maintenance_warehouse_ambiguity"

    ambiguity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    import_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_warehouse_import_batch.import_id"), nullable=False
    )
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_warehouse_document.document_id")
    )
    line_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_warehouse_document_line.line_id")
    )
    ambiguity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    field_code: Mapped[str | None] = mapped_column(String(256))
    source_row: Mapped[int | None] = mapped_column(Integer)
    value_hash: Mapped[str | None] = mapped_column(String(64))
    candidates_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    resolution_json: Mapped[dict | None] = mapped_column(JSONB)
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "ambiguity_type IN ('unknown_version', 'missing_document_id', 'missing_line_id', "
            "'missing_stable_link', 'multiple_candidates', 'field_conflict', "
            "'unknown_enum', 'controlled_attachment', 'integration_blocker')",
            name="ck_maintenance_wh_ambiguity_type",
        ),
        CheckConstraint("status IN ('open', 'resolved')", name="ck_maintenance_wh_ambiguity_status"),
        CheckConstraint("version >= 1", name="ck_maintenance_wh_ambiguity_version"),
        CheckConstraint("source_row IS NULL OR source_row >= 3", name="ck_maintenance_wh_ambiguity_row"),
        CheckConstraint("value_hash IS NULL OR value_hash ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_ambiguity_value_hash"),
        CheckConstraint("fingerprint ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_ambiguity_fingerprint"),
        CheckConstraint(
            "(status = 'open' AND resolution_json IS NULL AND resolution_reason IS NULL "
            "AND resolved_by IS NULL AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolution_json IS NOT NULL "
            "AND char_length(btrim(resolution_reason)) > 0 "
            "AND char_length(btrim(resolved_by)) > 0 AND resolved_at IS NOT NULL)",
            name="ck_maintenance_wh_ambiguity_resolution",
        ),
        UniqueConstraint("import_id", "fingerprint", name="uq_maintenance_wh_ambiguity_fingerprint"),
        Index("ix_maintenance_wh_ambiguity_queue", "status", "ambiguity_type", "created_at"),
        Index("ix_maintenance_wh_ambiguity_document", "document_id", "line_id"),
    )


class MaintenanceWarehouseAuditEvent(Base):
    __tablename__ = "maintenance_warehouse_audit_event"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    import_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_warehouse_import_batch.import_id")
    )
    ambiguity_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_warehouse_ambiguity.ambiguity_id")
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('import_applied', 'ambiguity_resolved', "
            "'integration_reconciled')",
            name="ck_maintenance_wh_audit_action",
        ),
        CheckConstraint("char_length(btrim(reason)) > 0", name="ck_maintenance_wh_audit_reason"),
        CheckConstraint("char_length(btrim(operated_by)) > 0", name="ck_maintenance_wh_audit_operator"),
        Index("ix_maintenance_wh_audit_time", "occurred_at", "event_id"),
        Index("ix_maintenance_wh_audit_ambiguity", "ambiguity_id", "occurred_at"),
    )
