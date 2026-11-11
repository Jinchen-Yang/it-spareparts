"""坏件循环池：循环单位、只追加事件、闲鱼可售意图（D-0057 T1）。

不写氚云库存 / 前台库存 / 采销单 / 变卖台账；处置状态与返还收货件况分离。
一行一件：单位表没有 qty 列，出站意图 qty 恒为 1（DB CHECK 兜底）。
可售意图 outbox 的租约/回执质量对齐 ``catalog_delivery``。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

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
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Qty, TZDateTime

# 工位：回收 / 检测 / 维修 / 委外 / 待处置 / 已结案
STATIONS = ("pool", "testing", "repairing", "outsource", "ready", "closed")
# 结案去向：电商 / 回公司 / 报废 / 已售出（售出由商城回写 mark-sold）
OUTCOMES = ("to_shop", "to_company", "scrap", "sold")
CONDITION_BUCKETS = ("good", "bad")
SOURCE_KINDS = ("receipt", "external")
INTENT_STATES = ("pending", "delivering", "delivered", "blocked", "failed")
INTENT_RECEIPT_STATUSES = ("applied", "unchanged", "blocked", "conflict")


class MaintenanceCycleUnit(Base):
    """一件循环单位（一行一件）。"""

    __tablename__ = "maintenance_cycle_unit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    unit_uid: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        nullable=False,
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
    withdrawn_at: Mapped[datetime | None] = mapped_column(TZDateTime)

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
            "withdrawn_at IS NULL OR "
            "(station = 'pool' AND outcome IS NULL AND closed_at IS NULL)",
            name="ck_maintenance_cycle_unit_withdrawn",
        ),
        CheckConstraint(
            "(outcome IS NULL OR outcome NOT IN ('to_shop', 'sold')) OR "
            "(condition_bucket IS NOT NULL AND condition_bucket IN ('good', 'bad'))",
            name="ck_maintenance_cycle_unit_shop_bucket",
        ),
        CheckConstraint(
            "repair_round >= 0 AND version >= 1",
            name="ck_maintenance_cycle_unit_counters",
        ),
        CheckConstraint(
            "char_length(btrim(pn)) > 0",
            name="ck_maintenance_cycle_unit_pn",
        ),
        UniqueConstraint("unit_uid", name="uq_maintenance_cycle_unit_uid"),
        Index("ix_maintenance_cycle_unit_station", "station", "assignee_username"),
        Index("ix_maintenance_cycle_unit_part", "part_id", "station"),
        Index("ix_maintenance_cycle_unit_receipt", "source_receipt_id"),
        # 同一收货行的非空 SN 在未撤回单位中唯一（SN 可选；撤回后释放）。
        Index(
            "ux_maintenance_cycle_unit_receipt_sn",
            "source_receipt_id",
            "serial_number",
            unique=True,
            postgresql_where=text(
                "withdrawn_at IS NULL AND serial_number IS NOT NULL"
            ),
        ),
    )


class MaintenanceCycleEvent(Base):
    """循环事件（只追加）。payload 主管审计字段由服务层白名单控制。"""

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
    """闲鱼可售意图 outbox（一单位一意图；领取租约 + 回执 CAS）。"""

    __tablename__ = "maintenance_cycle_sell_intent"

    delivery_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    unit_id: Mapped[int] = mapped_column(
        ForeignKey("maintenance_cycle_unit.id"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    condition_bucket: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    receipt_status: Mapped[str | None] = mapped_column(String(16))
    block_code: Mapped[str | None] = mapped_column(String(40))
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_sell_bucket",
        ),
        CheckConstraint(
            "state IN ('pending', 'delivering', 'delivered', 'blocked', 'failed')",
            name="ck_maintenance_cycle_sell_state",
        ),
        CheckConstraint("qty = 1", name="ck_maintenance_cycle_sell_qty"),
        CheckConstraint(
            "attempts >= 0", name="ck_maintenance_cycle_sell_attempts"
        ),
        CheckConstraint(
            "(state = 'delivering') = "
            "(lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_maintenance_cycle_sell_lease",
        ),
        CheckConstraint(
            "receipt_status IS NULL OR "
            "receipt_status IN ('applied', 'unchanged', 'blocked', 'conflict')",
            name="ck_maintenance_cycle_sell_receipt",
        ),
        UniqueConstraint(
            "unit_id", name="uq_maintenance_cycle_sell_intent_unit"
        ),
        Index(
            "ix_maintenance_cycle_sell_pending",
            "available_at",
            "created_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index(
            "ix_maintenance_cycle_sell_lease",
            "lease_until",
            postgresql_where=text("state = 'delivering'"),
        ),
        Index(
            "ix_maintenance_cycle_sell_blocked",
            "available_at",
            postgresql_where=text("state = 'blocked'"),
        ),
    )
