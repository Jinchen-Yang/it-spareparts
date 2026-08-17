"""Return obligations and controlled bad-part return documents."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


class MaintenanceReturnObligation(Base):
    """Stable obligation projected from one confirmed site-consumption line."""

    __tablename__ = "maintenance_return_obligation"

    obligation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    issue_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_site_issue.issue_id"), nullable=False
    )
    issue_line_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_line_id: Mapped[str] = mapped_column(String(64), nullable=False)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    source_quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    required_quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    classification: Mapped[str] = mapped_column(String(24), nullable=False)
    # 豁免来源：none / category_disk / line_no_return / project_default_no_return；
    # pending_category 时为 NULL。
    exemption_source: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    category_id_snapshot: Mapped[int | None] = mapped_column(Integer)
    category_major_snapshot: Mapped[str | None] = mapped_column(String(64))
    category_minor_snapshot: Mapped[str | None] = mapped_column(String(128))
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_issue_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_source_event_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_site_issue_return_event.event_id"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
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
            "classification IN ('required', 'exempt', 'pending_category')",
            name="ck_maintenance_return_obligation_classification",
        ),
        CheckConstraint(
            "source_quantity > 0 AND source_quantity < 100000000000",
            name="ck_maintenance_return_obligation_source_quantity",
        ),
        CheckConstraint(
            "required_quantity >= 0 AND required_quantity < 100000000000",
            name="ck_maintenance_return_obligation_required_quantity",
        ),
        CheckConstraint(
            "(classification = 'required' AND category_id_snapshot IS NOT NULL "
            "AND required_quantity = source_quantity AND exemption_source = 'none') OR "
            "(classification = 'exempt' AND required_quantity = 0 AND ("
            "(category_id_snapshot IS NOT NULL AND category_major_snapshot = '硬盘' "
            "AND exemption_source = 'category_disk') OR "
            "(exemption_source IN ('line_no_return', 'project_default_no_return')"
            "))) OR "
            "(classification = 'pending_category' AND category_id_snapshot IS NULL "
            "AND category_major_snapshot IS NULL AND category_minor_snapshot IS NULL "
            "AND required_quantity = 0 AND exemption_source IS NULL)",
            name="ck_maintenance_return_obligation_rule_result",
        ),
        CheckConstraint(
            "source_issue_version >= 1 AND version >= 1",
            name="ck_maintenance_return_obligation_versions",
        ),
        UniqueConstraint(
            "issue_id",
            "delivery_line_id",
            name="uq_maintenance_return_obligation_source",
        ),
        Index(
            "ix_maintenance_return_obligation_project_state",
            "project_id",
            "is_active",
            "classification",
        ),
    )


class MaintenanceBadReturn(Base):
    """Server-owned return document; it never mutates cost or inventory."""

    __tablename__ = "maintenance_bad_return"

    return_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    return_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    replaces_return_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_bad_return.return_id"), unique=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    logistics_reference: Mapped[str | None] = mapped_column(String(128))
    warehouse_reference: Mapped[str | None] = mapped_column(String(128))
    inbound_reference: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    in_transit_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    warehouse_confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    voided_at: Mapped[datetime | None] = mapped_column(TZDateTime)
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
            "status IN ('draft', 'submitted', 'in_transit', 'warehouse_confirmed', 'void')",
            name="ck_maintenance_bad_return_status",
        ),
        CheckConstraint(
            "(status = 'draft' AND submitted_at IS NULL AND in_transit_at IS NULL "
            "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NULL "
            "AND warehouse_reference IS NULL AND inbound_reference IS NULL "
            "AND voided_at IS NULL) OR "
            "(status = 'submitted' AND submitted_at IS NOT NULL "
            "AND in_transit_at IS NULL AND warehouse_confirmed_at IS NULL "
            "AND logistics_reference IS NULL AND warehouse_reference IS NULL "
            "AND inbound_reference IS NULL AND voided_at IS NULL) OR "
            "(status = 'in_transit' AND submitted_at IS NOT NULL "
            "AND in_transit_at IS NOT NULL AND warehouse_confirmed_at IS NULL "
            "AND logistics_reference IS NOT NULL AND warehouse_reference IS NULL "
            "AND inbound_reference IS NULL AND voided_at IS NULL) OR "
            "(status = 'warehouse_confirmed' AND submitted_at IS NOT NULL "
            "AND warehouse_confirmed_at IS NOT NULL "
            "AND ((in_transit_at IS NULL AND logistics_reference IS NULL) OR "
            "(in_transit_at IS NOT NULL AND logistics_reference IS NOT NULL)) "
            "AND warehouse_reference IS NOT NULL AND voided_at IS NULL) OR "
            "(status = 'void' AND voided_at IS NOT NULL "
            "AND inbound_reference IS NULL AND ("
            "(submitted_at IS NULL AND in_transit_at IS NULL "
            "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NULL "
            "AND warehouse_reference IS NULL) OR "
            "(submitted_at IS NOT NULL AND in_transit_at IS NULL "
            "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NULL "
            "AND warehouse_reference IS NULL) OR "
            "(submitted_at IS NOT NULL AND in_transit_at IS NOT NULL "
            "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NOT NULL "
            "AND warehouse_reference IS NULL) OR "
            "(submitted_at IS NOT NULL AND warehouse_confirmed_at IS NOT NULL "
            "AND warehouse_reference IS NOT NULL "
            "AND ((in_transit_at IS NULL AND logistics_reference IS NULL) OR "
            "(in_transit_at IS NOT NULL AND logistics_reference IS NOT NULL)))))",
            name="ck_maintenance_bad_return_state_evidence",
        ),
        CheckConstraint(
            "replaces_return_id IS NULL OR replaces_return_id <> return_id",
            name="ck_maintenance_bad_return_replacement_not_self",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_bad_return_version"),
        Index(
            "uq_maintenance_bad_return_inbound_reference",
            "inbound_reference",
            unique=True,
            postgresql_where=text("inbound_reference IS NOT NULL"),
        ),
        Index(
            "ix_maintenance_bad_return_project_status",
            "project_id",
            "status",
            "created_at",
        ),
    )


class MaintenanceBadReturnLine(Base):
    """Quantity registered against one explicit return obligation."""

    __tablename__ = "maintenance_bad_return_line"

    return_line_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    return_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_bad_return.return_id"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    obligation_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_return_obligation.obligation_id"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "quantity > 0 AND quantity < 100000000000",
            name="ck_maintenance_bad_return_line_quantity",
        ),
        CheckConstraint("line_no >= 1", name="ck_maintenance_bad_return_line_no"),
        UniqueConstraint(
            "return_id",
            "line_no",
            name="uq_maintenance_bad_return_line_no",
        ),
        UniqueConstraint(
            "return_id",
            "obligation_id",
            name="uq_maintenance_bad_return_line_obligation",
        ),
        Index(
            "ix_maintenance_bad_return_line_obligation",
            "obligation_id",
            "return_id",
        ),
    )


class MaintenanceBadReturnCommand(Base):
    """Append-only idempotency receipt for return and category commands."""

    __tablename__ = "maintenance_bad_return_command"

    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('bad_return', 'return_obligation')",
            name="ck_maintenance_bad_return_command_entity_type",
        ),
        CheckConstraint(
            "action IN ('create', 'submit', 'in_transit', "
            "'warehouse_confirm', 'void', 'resolve_category')",
            name="ck_maintenance_bad_return_command_action",
        ),
        Index(
            "ix_maintenance_bad_return_command_entity_time",
            "entity_type",
            "entity_id",
            "created_at",
        ),
    )
