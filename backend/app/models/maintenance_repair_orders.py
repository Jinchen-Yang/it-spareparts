"""实名维修任务单、任务条和可重试写操作。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._types import TZDateTime


repair_order_no_sequence = Sequence("maintenance_repair_order_no_seq", metadata=Base.metadata)


class MaintenanceRepairOrder(Base):
    __tablename__ = "maintenance_repair_order"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id"), nullable=False)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id"))
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    route: Mapped[str] = mapped_column(String(8), nullable=False)
    returned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    note: Mapped[str] = mapped_column(String(1000), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, server_default=func.now())
    history: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    repair_source_line_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "maintenance_repair_order_line.id",
            use_alter=True,
            name="fk_repair_order_previous_line",
        )
    )
    lines: Mapped[list[MaintenanceRepairOrderLine]] = relationship(
        foreign_keys="MaintenanceRepairOrderLine.order_id",
        back_populates="order",
        order_by="MaintenanceRepairOrderLine.position",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("mode IN ('internal', 'external')", name="ck_repair_order_mode"),
        CheckConstraint("route IN ('pool', 'direct', 'assigned', 'claimed')", name="ck_repair_order_route"),
        CheckConstraint("NOT returned OR assignee_id IS NULL", name="ck_repair_order_returned"),
        CheckConstraint("version >= 1", name="ck_repair_order_version"),
        Index("ix_repair_order_creator", "creator_id", "created_at"),
        Index("ix_repair_order_assignee", "assignee_id", "created_at"),
        Index("ix_repair_order_created_at", "created_at", "id"),
        Index("ix_repair_order_previous_line", "repair_source_line_id"),
    )


class MaintenanceRepairOrderLine(Base):
    __tablename__ = "maintenance_repair_order_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("maintenance_repair_order.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    fixed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unfixed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    previously_handled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    dispositions: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    source_receipt_id: Mapped[str | None] = mapped_column(ForeignKey("maintenance_rkd_return_line.rkd_line_id"))
    source_head_no: Mapped[str | None] = mapped_column(String(64))
    source_version: Mapped[int | None] = mapped_column(Integer)
    order: Mapped[MaintenanceRepairOrder] = relationship(
        foreign_keys=[order_id], back_populates="lines"
    )

    __table_args__ = (
        CheckConstraint("qty > 0 AND fixed >= 0 AND unfixed >= 0 AND qty - fixed >= unfixed", name="ck_repair_order_line_qty"),
        CheckConstraint("position >= 0", name="ck_repair_order_line_position"),
        CheckConstraint("char_length(btrim(pn)) > 0", name="ck_repair_order_line_pn"),
        CheckConstraint(
            "(source_receipt_id IS NULL AND source_head_no IS NULL AND source_version IS NULL) OR "
            "(source_receipt_id IS NOT NULL AND source_head_no IS NOT NULL "
            "AND source_version IS NOT NULL AND source_version > 0)",
            name="ck_repair_order_line_source",
        ),
        UniqueConstraint(
            "order_id", "position", name="uq_repair_order_line_position",
            deferrable=True, initially="DEFERRED",
        ),
        Index("ix_repair_order_line_source", "source_receipt_id"),
    )


class MaintenanceRepairOrderCommand(Base):
    __tablename__ = "maintenance_repair_order_command"

    actor_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("maintenance_repair_order.id"), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, server_default=func.now())
