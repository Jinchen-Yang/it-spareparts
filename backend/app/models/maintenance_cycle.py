"""坏件循环单位与可售意图（D-0057）。

不写氚云库存 / 前置库 / 采销单。处置状态与返还收货件况分离。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Qty, TZDateTime

STATIONS = ("pool", "testing", "repairing", "outsource", "ready", "closed")
OUTCOMES = ("to_shop", "to_company", "scrap", "sold")
CONDITION_BUCKETS = ("good", "bad")
SOURCE_KINDS = ("receipt", "external")
INTENT_STATES = ("pending", "delivering", "delivered", "blocked", "failed")


class MaintenanceCycleUnit(Base):
    __tablename__ = "maintenance_cycle_unit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    unit_uid: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        nullable=False,
        unique=True,
        default=lambda: str(uuid4()),
        server_default=text("gen_random_uuid()"),
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    station: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pool", server_default="pool"
    )
    outcome: Mapped[str | None] = mapped_column(String(16))
    condition_bucket: Mapped[str | None] = mapped_column(String(8))
    intake_condition: Mapped[str | None] = mapped_column(String(64))
    repair_round: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_rkd_return_line.rkd_line_id")
    )
    serial_number: Mapped[str | None] = mapped_column(String(128))
    vendor_name: Mapped[str | None] = mapped_column(String(128))
    assignee_username: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint(
            "station IN ('pool', 'testing', 'repairing', 'outsource', 'ready', 'closed')",
            name="ck_maintenance_cycle_unit_station",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('to_shop', 'to_company', 'scrap', 'sold')",
            name="ck_maintenance_cycle_unit_outcome",
        ),
        CheckConstraint(
            "condition_bucket IS NULL OR condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_unit_bucket",
        ),
        CheckConstraint(
            "source_kind IN ('receipt', 'external')",
            name="ck_maintenance_cycle_unit_source_kind",
        ),
        CheckConstraint(
            "(source_kind = 'receipt' AND source_receipt_id IS NOT NULL) OR "
            "(source_kind = 'external' AND source_receipt_id IS NULL)",
            name="ck_maintenance_cycle_unit_source_shape",
        ),
        CheckConstraint(
            "(station = 'closed') = (outcome IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_maintenance_cycle_unit_closed_shape",
        ),
        CheckConstraint(
            "repair_round >= 0 AND version >= 1",
            name="ck_maintenance_cycle_unit_counters",
        ),
        CheckConstraint(
            "char_length(btrim(pn)) > 0",
            name="ck_maintenance_cycle_unit_pn",
        ),
        Index("ix_maintenance_cycle_unit_station", "station", "assignee_username"),
        Index("ix_maintenance_cycle_unit_part", "part_id", "station"),
        Index("ix_maintenance_cycle_unit_receipt", "source_receipt_id"),
    )


class MaintenanceCycleEvent(Base):
    __tablename__ = "maintenance_cycle_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    unit_id: Mapped[int] = mapped_column(
        ForeignKey("maintenance_cycle_unit.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_maintenance_cycle_event_unit", "unit_id", "id"),
    )


class MaintenanceCycleSellIntent(Base):
    __tablename__ = "maintenance_cycle_sell_intent"

    delivery_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    unit_id: Mapped[int] = mapped_column(
        ForeignKey("maintenance_cycle_unit.id"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    condition_bucket: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    lease_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint(
            "condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_sell_bucket",
        ),
        CheckConstraint(
            "state IN ('pending', 'delivering', 'delivered', 'blocked', 'failed')",
            name="ck_maintenance_cycle_sell_state",
        ),
        CheckConstraint("qty > 0", name="ck_maintenance_cycle_sell_qty"),
        CheckConstraint(
            "attempt_count >= 0", name="ck_maintenance_cycle_sell_attempts"
        ),
        UniqueConstraint(
            "unit_id", name="uq_maintenance_cycle_sell_intent_unit"
        ),
        Index("ix_maintenance_cycle_sell_state", "state", "created_at"),
    )
