"""维保前置库账本（B1）。

模型（已确认口径 2026-08-15）：
- 前置库 = 项目现场小库；**没有现场收货环节**：发货单（维保供货）直接入前置库；
- 前置库名以 Excel 原样为准（一般 = 项目名），不强建仓库字典；
- **现场领用不写本账本**（领用只登记消耗事实与应返义务）；
- 出库 = 项目结束收回（返库单）与变卖；
- 不记录 SN，只统计 PN 与数量。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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

# 流水类型（入库为正、出库为负）
MOVEMENT_KINDS = ("shipment_in", "purchase_in", "return_out", "salvage_out")
# 来源类型：ckd_shipment_line=氚云发货单明细 / return_order_line=氚云返库单明细 /
# f_maintenance_line=WBDD 需求明细 / warehouse_document_line=仓库单据 / salvage=变卖登记
SOURCE_TYPES = (
    "ckd_shipment_line",
    "return_order_line",
    "f_maintenance_line",
    "warehouse_document_line",
    "salvage",
    "manual",
)


class MaintenanceFrontStock(Base):
    """前置库结存：项目 × PN × 前置库名（Excel 原样）的最新数量与库龄。"""

    __tablename__ = "maintenance_front_stock"

    stock_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    # 前置库名以来源 Excel 原样为准；空串 = 项目默认库（按项目名）。
    warehouse_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default="''"
    )
    qty: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    unit_cost_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    unit_cost_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    # 最近一次入账时间：库龄 = 当前时间 − last_inbound_at
    last_inbound_at: Mapped[datetime | None] = mapped_column(TZDateTime)
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
            "qty >= 0", name="ck_maintenance_front_stock_qty_non_negative"
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_front_stock_version"),
        CheckConstraint(
            "char_length(warehouse_name) <= 128",
            name="ck_maintenance_front_stock_warehouse_len",
        ),
        UniqueConstraint(
            "project_id",
            "part_id",
            "warehouse_name",
            name="uq_maintenance_front_stock_identity",
        ),
        Index("ix_maintenance_front_stock_project", "project_id"),
        Index("ix_maintenance_front_stock_part", "part_id"),
        Index("ix_maintenance_front_stock_inbound", "last_inbound_at"),
    )


class MaintenanceFrontStockLedger(Base):
    """前置库流水（append-only）：每一次入/出账与余额快照。"""

    __tablename__ = "maintenance_front_stock_ledger"

    ledger_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stock_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_front_stock.stock_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    # 有符号数量：入为正、出为负；qty_after 为该笔之后的结存快照。
    qty_change: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    qty_after: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    # 同来源重放校验：payload 摘要一致才算幂等重放，不一致失败关闭。
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 业务发生时间（发货日期等）；库龄以此为准，imported_at 用 created_at。
    occurred_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    unit_cost_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    unit_cost_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    reason: Mapped[str | None] = mapped_column(Text)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('shipment_in', 'purchase_in', 'return_out', 'salvage_out')",
            name="ck_maintenance_front_stock_ledger_kind",
        ),
        CheckConstraint(
            "source_type IN ('ckd_shipment_line', 'return_order_line',"
            " 'f_maintenance_line', 'warehouse_document_line', 'salvage', 'manual')",
            name="ck_maintenance_front_stock_ledger_source_type",
        ),
        CheckConstraint(
            "qty_change <> 0", name="ck_maintenance_front_stock_ledger_qty_change"
        ),
        CheckConstraint(
            "qty_after >= 0", name="ck_maintenance_front_stock_ledger_qty_after"
        ),
        CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_front_stock_ledger_operator",
        ),
        # 幂等：同一来源事件只入账一次；kind/part/qty 变化由 payload_hash 校验拒绝。
        UniqueConstraint(
            "source_type",
            "source_ref",
            name="uq_maintenance_front_stock_ledger_source",
        ),
        Index("ix_maintenance_front_stock_ledger_project", "project_id", "created_at"),
        Index("ix_maintenance_front_stock_ledger_stock", "stock_id", "created_at"),
        Index("ix_maintenance_front_stock_ledger_source", "source_type", "source_ref"),
    )
