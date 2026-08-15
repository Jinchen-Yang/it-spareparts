"""氚云三单通用导入事实层（C1b）：入库单 RKD / 退货返库单 / 报销单 BXD。

设计取舍：CKD 发货单是成本与前置库入账的关键路径，采用显式列建模
（maintenance_ckd_import）；本模块覆盖的三单当前接线较浅（返库单→前置库出账、
RKD→坏件返还事实待 F3、BXD→对账待 C4），采用「归一化关键列 + raw_json 全量保留」
的方式，原始单元格值永不丢失，后续接线只增列不搬家。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (UniqueConstraint,
    ARRAY,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, TZDateTime

DOC_TYPES = ("rkd_inbound", "return_order", "bxd_expense")


class MaintenanceDocImportBatch(Base):
    """一次三单之一的上传批次。"""

    __tablename__ = "maintenance_doc_import_batch"

    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(16), nullable=False)
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
        CheckConstraint("issue_rows >= 0", name="ck_maintenance_doc_import_batch_issue_rows"),
        CheckConstraint("line_rows >= 0", name="ck_maintenance_doc_import_batch_line_rows"),
        CheckConstraint("head_rows >= 0", name="ck_maintenance_doc_import_batch_head_rows"),
        CheckConstraint(
            "doc_type IN ('rkd_inbound', 'return_order', 'bxd_expense')",
            name="ck_maintenance_doc_import_doc_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_maintenance_doc_import_status",
        ),
        CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL AND applied_by IS NOT NULL)",
            name="ck_maintenance_doc_import_applied",
        ),
        UniqueConstraint(
            "uploaded_by",
            "idempotency_key",
            name="uq_maintenance_doc_import_idempotency",
        ),
        Index("ix_maintenance_doc_import_hash", "file_hash"),
        Index("ix_maintenance_doc_import_type_uploaded", "doc_type", "uploaded_at"),
    )


class MaintenanceDocHeadRow(Base):
    """三单主表行：raw_json 保留全部单元格，归一化列供 apply。"""

    __tablename__ = "maintenance_doc_head_row"

    row_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_doc_import_batch.batch_id"), nullable=False
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # ---- 归一化 ----
    head_no: Mapped[str | None] = mapped_column(String(64))  # RKD-*/BXD-*/数据ID
    head_date: Mapped[Date | None] = mapped_column(Date)
    category: Mapped[str | None] = mapped_column(String(64))  # 入库类别/返库类别/报销类别
    wbdd_no: Mapped[str | None] = mapped_column(String(64))
    xsdd_no: Mapped[str | None] = mapped_column(String(64))
    project_name: Mapped[str | None] = mapped_column(String(256))
    data_status: Mapped[str | None] = mapped_column(String(64))
    issues: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(128)), nullable=True, default=list
    )

    __table_args__ = (
        CheckConstraint("row_no >= 1", name="ck_maintenance_doc_head_row_no"),
        Index("ix_maintenance_doc_head_batch", "batch_id"),
        Index("ix_maintenance_doc_head_no", "head_no"),
    )


class MaintenanceDocLineRow(Base):
    """三单明细行：raw_json 保留全部单元格，归一化列供 apply。"""

    __tablename__ = "maintenance_doc_line_row"

    row_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_doc_import_batch.batch_id"), nullable=False
    )
    head_row_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_doc_head_row.row_id"), nullable=False
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # ---- 归一化 ----
    line_key: Mapped[str | None] = mapped_column(String(64))  # 明细数据ID/序号
    pn: Mapped[str | None] = mapped_column(String(128))
    qty: Mapped[Decimal | None] = mapped_column(Qty)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    test_result: Mapped[str | None] = mapped_column(String(64))  # 成品/坏品/...
    warehouse: Mapped[str | None] = mapped_column(String(128))
    location: Mapped[str | None] = mapped_column(String(128))
    issues: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(128)), nullable=True, default=list
    )

    __table_args__ = (
        CheckConstraint("row_no >= 1", name="ck_maintenance_doc_line_row_no"),
        Index("ix_maintenance_doc_line_batch", "batch_id"),
        Index("ix_maintenance_doc_line_head", "head_row_id"),
    )


class MaintenanceRkdReturnLine(Base):
    """RKD 入库单坏件返还 canonical 事实（F3 返还率分子，Q8 口径）。

    apply 时从 raw 明细行投影：test_result ∈ 坏品/坏件/故障 的行 = 已返还事实。
    不扣前置库账本（坏件是消耗返还，不走 front_stock）；领用→不返还 义务在
    maintenance_return_obligation，本表只记「入库单确认收到」的数量。
    """

    __tablename__ = "maintenance_rkd_return_line"

    rkd_line_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_doc_import_batch.batch_id"), nullable=False
    )
    head_row_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_doc_head_row.row_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    head_no: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(96), nullable=False)
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    test_result: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_maintenance_rkd_return_qty"),
        CheckConstraint(
            "char_length(btrim(pn)) > 0",
            name="ck_maintenance_rkd_return_pn",
        ),
        UniqueConstraint(
            "source_ref", name="uq_maintenance_rkd_return_source_ref"
        ),
        Index(
            "ix_maintenance_rkd_return_project",
            "project_id",
            "part_id",
            "occurred_at",
        ),
    )
