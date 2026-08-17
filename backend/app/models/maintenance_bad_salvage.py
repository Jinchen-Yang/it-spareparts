"""坏件变卖登记（F5）：独立事实表，不伪造采购/销售单。

口径（2026-08-15 计划 §3 F5 + §12 完成标准）：
- 坏件变卖是回收环节的独立登记事实：只记 PN/数量/变卖收入/日期/渠道，
  不写采购、销售、前置库账本；
- 贡献毛利 = 变卖收入 − 对应坏件领用成本（含税口径），取该项目该 PN 最近
  已确认领用单的单位含税成本；无成本证据时毛利为 null（缺成本不按 0）。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, TZDateTime


class MaintenanceBadSalvage(Base):
    __tablename__ = "maintenance_bad_salvage"

    salvage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    # 坏件 PN 可能未解析（报废件无标准 PN）→ part_id 可空，pn 文本保留事实。
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    revenue: Mapped[Decimal] = mapped_column(Money, nullable=False)
    salvage_date: Mapped[date] = mapped_column(Date, nullable=False)
    buyer_note: Mapped[str | None] = mapped_column(String(256))
    reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # 登记时冻结的成本证据（登记后事实变化不改写历史毛利）
    cost_basis_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    cost_source_ref: Mapped[str | None] = mapped_column(String(64))
    cost_algorithm_version: Mapped[str | None] = mapped_column(String(64))
    # 登记时是否已对前置库做 salvage_out（好件/未用件在库才扣账）
    stock_deducted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    voided_by: Mapped[str | None] = mapped_column(String(64))
    voided_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_maintenance_bad_salvage_qty"),
        CheckConstraint(
            "revenue >= 0 AND revenue < 100000000000",
            name="ck_maintenance_bad_salvage_revenue",
        ),
        CheckConstraint(
            "char_length(btrim(pn)) > 0",
            name="ck_maintenance_bad_salvage_pn",
        ),
        CheckConstraint(
            "payload_digest ~ '^[a-f0-9]{64}$'",
            name="ck_maintenance_bad_salvage_payload_digest",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_bad_salvage_version"),
        CheckConstraint(
            "(cost_basis_inc_tax IS NULL AND cost_source_ref IS NULL "
            "AND cost_algorithm_version IS NULL) OR "
            "(cost_basis_inc_tax IS NOT NULL AND cost_source_ref IS NOT NULL "
            "AND cost_algorithm_version IS NOT NULL)",
            name="ck_maintenance_bad_salvage_cost_pair",
        ),
        CheckConstraint(
            "(is_active AND voided_at IS NULL AND voided_by IS NULL) OR "
            "(NOT is_active AND voided_at IS NOT NULL AND voided_by IS NOT NULL)",
            name="ck_maintenance_bad_salvage_void_state",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_maintenance_bad_salvage_idempotency",
        ),
        Index(
            "ix_maintenance_bad_salvage_project_date",
            "project_id",
            "salvage_date",
        ),
    )
