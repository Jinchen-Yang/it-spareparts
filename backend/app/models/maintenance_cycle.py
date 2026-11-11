"""坏件循环可售意图 outbox（D-0058）。

一件一行的单位表和事件表已删除。意图挂在维修结果上，
``qty`` 为修好件数 1–1000，``wire_unit_id`` 抄结果上永不回收的整数。
发给商城的 JSON 不包含 ``record_id``。
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime
import app.models.maintenance_repair as _repair_models  # noqa: F401  意图外键依赖结果表

CONDITION_BUCKETS = ("good", "bad")
INTENT_STATES = ("pending", "delivering", "delivered", "blocked", "failed")
INTENT_RECEIPT_STATUSES = ("applied", "unchanged", "blocked", "conflict")
BLOCK_CODES = (
    "missing_formal_image",
    "missing_category_mapping",
    "missing_list_price",
    "missing_catalog_identity",
    "publish_api_missing_stock_update",
    "publish_disabled",
    "missing_operator",
)


class MaintenanceCycleSellIntent(Base):
    """一条有效结果最多一条可售意图。重发使用同一个 delivery_id。"""

    __tablename__ = "maintenance_cycle_sell_intent"

    delivery_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    record_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("maintenance_repair_record.record_id"),
        nullable=False,
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    wire_unit_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
        CheckConstraint(
            "qty BETWEEN 1 AND 1000", name="ck_maintenance_cycle_sell_qty"
        ),
        CheckConstraint("wire_unit_id > 0", name="ck_maintenance_cycle_sell_wire"),
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
        CheckConstraint(
            "block_code IS NULL OR block_code IN ("
            "'missing_formal_image', 'missing_category_mapping', 'missing_list_price', "
            "'missing_catalog_identity', 'publish_api_missing_stock_update', "
            "'publish_disabled', 'missing_operator')",
            name="ck_maintenance_cycle_sell_block",
        ),
        UniqueConstraint("record_id", name="uq_maintenance_cycle_sell_intent_record"),
        UniqueConstraint("wire_unit_id", name="uq_maintenance_cycle_sell_intent_wire"),
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
