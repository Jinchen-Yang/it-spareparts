"""坏件循环待修池：维修结果、扣数来源、写操作幂等（D-0058）。

待修、已上电商、废品都是查询结果，不在这里存余额。
``record_id`` 只在 itsp 内部使用；出站 ``unitId`` 用永不回收的 ``wire_unit_id``。
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime

RECORD_STATUSES = ("active", "voided")
COMMAND_ACTIONS = ("submit", "void", "attest_shop_adjustment")


class MaintenanceRepairRecord(Base):
    """一次维修结果：修好 N 与废品 M。作废整笔退回，不改扣数行的件数。"""

    __tablename__ = "maintenance_repair_record"

    record_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    wire_unit_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    record_no: Mapped[str] = mapped_column(String(32), nullable=False)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    fixed_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    scrap_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    operator: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    voided_by: Mapped[str | None] = mapped_column(String(64))
    voided_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    void_reason: Mapped[str | None] = mapped_column(String(256))
    shop_adjusted_manually: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("wire_unit_id > 0", name="ck_maintenance_repair_record_wire"),
        CheckConstraint(
            "record_no = 'WX' || lpad(wire_unit_id::text, 8, '0')",
            name="ck_maintenance_repair_record_no",
        ),
        CheckConstraint(
            "fixed_qty BETWEEN 0 AND 1000",
            name="ck_maintenance_repair_record_fixed",
        ),
        CheckConstraint(
            "scrap_qty BETWEEN 0 AND 1000",
            name="ck_maintenance_repair_record_scrap",
        ),
        CheckConstraint(
            "fixed_qty + scrap_qty >= 1",
            name="ck_maintenance_repair_record_total",
        ),
        CheckConstraint(
            "status IN ('active', 'voided')",
            name="ck_maintenance_repair_record_status",
        ),
        CheckConstraint(
            "("
            "status = 'active' AND voided_by IS NULL AND voided_at IS NULL "
            "AND void_reason IS NULL"
            ") OR ("
            "status = 'voided' AND voided_by IS NOT NULL AND voided_at IS NOT NULL "
            "AND void_reason IS NOT NULL"
            ")",
            name="ck_maintenance_repair_record_void_shape",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_repair_record_version"),
        CheckConstraint(
            "char_length(btrim(pn)) > 0 AND char_length(btrim(operator)) > 0",
            name="ck_maintenance_repair_record_text",
        ),
        UniqueConstraint("wire_unit_id", name="uq_maintenance_repair_record_wire"),
        Index("ix_maintenance_repair_record_part", "part_id", "status"),
        Index("ix_maintenance_repair_record_operator", "operator", "created_at"),
    )


class MaintenanceRepairRecordSource(Base):
    """结果扣自哪张坏返行。作废后保留，统计只计 status=active 的结果。"""

    __tablename__ = "maintenance_repair_record_source"

    record_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("maintenance_repair_record.record_id"),
        primary_key=True,
    )
    receipt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("maintenance_rkd_return_line.rkd_line_id"),
        primary_key=True,
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_maintenance_repair_source_qty"),
        Index("ix_maintenance_repair_source_receipt", "receipt_id"),
    )


class MaintenanceRepairCommand(Base):
    """写操作幂等。主键含操作者，避免一个账号读到另一个账号的响应。"""

    __tablename__ = "maintenance_repair_command"

    actor: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    record_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('submit', 'void', 'attest_shop_adjustment')",
            name="ck_maintenance_repair_command_action",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_maintenance_repair_command_fingerprint",
        ),
        CheckConstraint(
            "status_code BETWEEN 200 AND 299",
            name="ck_maintenance_repair_command_status",
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_maintenance_repair_command_key"
        ),
    )
