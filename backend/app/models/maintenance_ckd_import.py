"""氚云发货单（CKD）导入事实层（C1a/F1）。

发货单是四类氚云机打单之一：166 列、主表+明细分组（明细行只填「备件明细.*」列）、
第 1 行字段码 + 第 2 行字段名。apply 只把「维保供货」明细按 WBDD→项目稳定关联
入前置库账本（kind=shipment_in）；销售出库/采购退货不计入前置库。
明细自带成本单价/金额：先原样落 raw，成本口径确认后回填 front_stock 估值。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (UniqueConstraint,
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, TZDateTime


class MaintenanceCkdImportBatch(Base):
    """一次发货单上传：哈希、行数、apply 状态。"""

    __tablename__ = "maintenance_ckd_import_batch"

    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    head_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    line_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    issue_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="'pending'"
    )
    report_json: Mapped[dict | None] = mapped_column(JSONB)
    applied_by: Mapped[str | None] = mapped_column(String(64))
    applied_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint("issue_rows >= 0", name="ck_maintenance_ckd_import_batch_issue_rows"),
        CheckConstraint("line_rows >= 0", name="ck_maintenance_ckd_import_batch_line_rows"),
        CheckConstraint("head_rows >= 0", name="ck_maintenance_ckd_import_batch_head_rows"),
        CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_maintenance_ckd_import_status",
        ),
        CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL AND applied_by IS NOT NULL)",
            name="ck_maintenance_ckd_import_applied",
        ),
        UniqueConstraint(
            "uploaded_by",
            "idempotency_key",
            name="uq_maintenance_ckd_import_idempotency",
        ),
        Index("ix_maintenance_ckd_import_hash", "file_hash"),
        Index("ix_maintenance_ckd_import_uploaded", "uploaded_at"),
    )


class MaintenanceCkdHeadRow(Base):
    """发货单主表行（出库单号 CKD-*）。"""

    __tablename__ = "maintenance_ckd_head_row"

    row_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_ckd_import_batch.batch_id"), nullable=False
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # ---- 原始值 ----
    order_no_raw: Mapped[str | None] = mapped_column(String(64))
    order_date_raw: Mapped[str | None] = mapped_column(String(64))
    category_raw: Mapped[str | None] = mapped_column(String(64))
    machine_or_part_raw: Mapped[str | None] = mapped_column(String(64))
    warehouse_raw: Mapped[str | None] = mapped_column(String(128))
    wh_center_raw: Mapped[str | None] = mapped_column(String(128))
    wbdd_raw: Mapped[str | None] = mapped_column(String(64))
    wbdd_parts_raw: Mapped[str | None] = mapped_column(String(64))
    sales_order_raw: Mapped[str | None] = mapped_column(String(64))
    salesperson_raw: Mapped[str | None] = mapped_column(String(64))
    project_manager_raw: Mapped[str | None] = mapped_column(String(128))
    maintainer_raw: Mapped[str | None] = mapped_column(String(128))
    data_status_raw: Mapped[str | None] = mapped_column(String(64))
    remark_raw: Mapped[str | None] = mapped_column(Text)
    # ---- 归一化值 ----
    order_no: Mapped[str | None] = mapped_column(String(64))
    order_date: Mapped[date | None] = mapped_column(Date)
    category: Mapped[str | None] = mapped_column(String(64))
    wbdd_no: Mapped[str | None] = mapped_column(String(64))
    wbdd_parts_no: Mapped[str | None] = mapped_column(String(64))
    sales_order_no: Mapped[str | None] = mapped_column(String(64))
    issues: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(128)), nullable=True, default=list
    )

    __table_args__ = (
        CheckConstraint("row_no >= 1", name="ck_maintenance_ckd_head_row_no"),
        Index("ix_maintenance_ckd_head_batch", "batch_id"),
        Index("ix_maintenance_ckd_head_order", "order_no"),
    )


class MaintenanceCkdLineRow(Base):
    """发货单明细行（备件明细.*）。"""

    __tablename__ = "maintenance_ckd_line_row"

    row_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_ckd_import_batch.batch_id"), nullable=False
    )
    head_row_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_ckd_head_row.row_id"), nullable=False
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # ---- 原始值 ----
    data_id_raw: Mapped[str | None] = mapped_column(String(64))
    seq_raw: Mapped[str | None] = mapped_column(String(64))
    title_raw: Mapped[str | None] = mapped_column(String(128))
    part_name_raw: Mapped[str | None] = mapped_column(String(256))
    self_code_raw: Mapped[str | None] = mapped_column(String(128))
    pn_raw: Mapped[str | None] = mapped_column(String(128))
    sn_raw: Mapped[str | None] = mapped_column(String(128))
    desc_raw: Mapped[str | None] = mapped_column(Text)
    warehouse_raw: Mapped[str | None] = mapped_column(String(128))
    location_raw: Mapped[str | None] = mapped_column(String(128))
    brand_raw: Mapped[str | None] = mapped_column(String(64))
    category_major_raw: Mapped[str | None] = mapped_column(String(64))
    category_minor_raw: Mapped[str | None] = mapped_column(String(128))
    unit_raw: Mapped[str | None] = mapped_column(String(16))
    out_qty_raw: Mapped[str | None] = mapped_column(String(64))
    unit_cost_raw: Mapped[str | None] = mapped_column(String(64))
    cost_amount_raw: Mapped[str | None] = mapped_column(String(64))
    test_result_raw: Mapped[str | None] = mapped_column(String(64))
    # ---- 归一化值 ----
    pn: Mapped[str | None] = mapped_column(String(128))
    out_qty: Mapped[Decimal | None] = mapped_column(Qty)
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    cost_amount: Mapped[Decimal | None] = mapped_column(Money)
    issues: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(128)), nullable=True, default=list
    )

    __table_args__ = (
        CheckConstraint("row_no >= 1", name="ck_maintenance_ckd_line_row_no"),
        Index("ix_maintenance_ckd_line_batch", "batch_id"),
        Index("ix_maintenance_ckd_line_head", "head_row_id"),
    )
