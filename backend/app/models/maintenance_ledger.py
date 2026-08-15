"""维保台账工作簿导入的事实层（raw 行 + 批次）。

台账工作簿是商务线唯一事实源（项目/合同/维保期限/回款计划/报销归集）。
导入流程分两步：preview 只解析并落 raw 行（零业务写入）；apply 把 raw 行
同步进 canonical 表（maintenance_project / maintenance_project_contract /
maintenance_collection_milestone）。报销归集行只保留 raw，与氚云 BXD 导出
逐条对账后才进入 canonical（见 C4 对账车道）。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
from app.models._types import Money, TZDateTime

# 台账来源标识：与 maintenance_collection_milestone.source 枚举对齐。
LEDGER_SOURCE = "project_manager_xls_v1"
# 台账模板 v1（见 docs/maintenance/workbook-template-design.md）。
LEDGER_TEMPLATE_SOURCE = "ledger_template_v1"


class MaintenanceLedgerImportBatch(Base):
    """一次台账工作簿上传：文件哈希、行数与 apply 状态。"""

    __tablename__ = "maintenance_ledger_import_batch"

    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    contract_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    plan_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    expense_rows: Mapped[int] = mapped_column(
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
        CheckConstraint(
            "source_kind IN ('project_manager_xls_v1', 'ledger_template_v1')",
            name="ck_maintenance_ledger_import_source_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_maintenance_ledger_import_status",
        ),
        CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL AND applied_by IS NOT NULL)",
            name="ck_maintenance_ledger_import_applied",
        ),
        Index("ix_maintenance_ledger_import_hash", "file_hash"),
        Index("ix_maintenance_ledger_import_uploaded", "uploaded_at"),
    )


class MaintenanceLedgerContractRow(Base):
    """台账「维保项目清单」逐行：原始值 + 归一化值，永不覆盖。"""

    __tablename__ = "maintenance_ledger_contract_row"

    row_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_ledger_import_batch.batch_id"), nullable=False
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # ---- 原始值（审计保留）----
    order_no_raw: Mapped[str | None] = mapped_column(String(64))
    order_date_raw: Mapped[str | None] = mapped_column(String(64))
    salesperson_raw: Mapped[str | None] = mapped_column(String(64))
    business_type_raw: Mapped[str | None] = mapped_column(String(64))
    project_name_raw: Mapped[str | None] = mapped_column(String(256))
    maint_start_raw: Mapped[str | None] = mapped_column(String(64))
    maint_end_raw: Mapped[str | None] = mapped_column(String(64))
    cmo_raw: Mapped[str | None] = mapped_column(String(128))
    manager_raw: Mapped[str | None] = mapped_column(String(128))
    amount_raw: Mapped[str | None] = mapped_column(String(64))
    collected_raw: Mapped[str | None] = mapped_column(String(64))
    receivable_raw: Mapped[str | None] = mapped_column(String(64))
    acceptance_material_raw: Mapped[str | None] = mapped_column(Text)
    acceptance_done_raw: Mapped[str | None] = mapped_column(String(16))
    acceptance_attachment_raw: Mapped[str | None] = mapped_column(String(255))
    inspection_time_raw: Mapped[str | None] = mapped_column(String(64))
    inspection_done_raw: Mapped[str | None] = mapped_column(String(16))
    # ---- 归一化值 ----
    order_no: Mapped[str | None] = mapped_column(String(64))
    order_date: Mapped[date | None] = mapped_column(Date)
    business_type: Mapped[str | None] = mapped_column(String(64))
    project_name: Mapped[str | None] = mapped_column(String(256))
    project_period_from: Mapped[date | None] = mapped_column(Date)
    project_period_to: Mapped[date | None] = mapped_column(Date)
    cmo: Mapped[str | None] = mapped_column(String(128))
    manager: Mapped[str | None] = mapped_column(String(128))
    amount_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    collected_amount: Mapped[Decimal | None] = mapped_column(Money)
    receivable_amount: Mapped[Decimal | None] = mapped_column(Money)
    issues: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)

    __table_args__ = (
        CheckConstraint("row_no >= 1", name="ck_maintenance_ledger_contract_row_no"),
        Index("ix_maintenance_ledger_contract_batch", "batch_id"),
        Index("ix_maintenance_ledger_contract_order_no", "order_no"),
    )


class MaintenanceLedgerPlanRow(Base):
    """台账回款计划行（旧版 24 组横向对展开后的纵向行 / 新版 02_回款计划 sheet）。"""

    __tablename__ = "maintenance_ledger_plan_row"

    row_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_ledger_import_batch.batch_id"), nullable=False
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    order_no_raw: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    time_raw: Mapped[str | None] = mapped_column(String(64))
    amount_raw: Mapped[str | None] = mapped_column(String(64))
    order_no: Mapped[str | None] = mapped_column(String(64))
    planned_date: Mapped[date | None] = mapped_column(Date)
    date_precision: Mapped[str | None] = mapped_column(String(8))
    planned_amount: Mapped[Decimal | None] = mapped_column(Money)
    issues: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)

    __table_args__ = (
        CheckConstraint(
            "sequence BETWEEN 1 AND 24",
            name="ck_maintenance_ledger_plan_seq",
        ),
        Index("ix_maintenance_ledger_plan_batch", "batch_id"),
        Index("ix_maintenance_ledger_plan_order", "order_no", "sequence"),
    )


class MaintenanceLedgerExpenseRow(Base):
    """台账「项目成本」报销归集行：只保留 raw + 归一化，对账后进 canonical。"""

    __tablename__ = "maintenance_ledger_expense_row"

    row_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_ledger_import_batch.batch_id"), nullable=False
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    bxd_no_raw: Mapped[str | None] = mapped_column(String(64))
    person_raw: Mapped[str | None] = mapped_column(String(64))
    expense_type_raw: Mapped[str | None] = mapped_column(String(64))
    reason_raw: Mapped[str | None] = mapped_column(Text)
    sales_order_raw: Mapped[str | None] = mapped_column(String(64))
    project_name_raw: Mapped[str | None] = mapped_column(String(256))
    sales_order_dup_raw: Mapped[str | None] = mapped_column(String(64))
    salesperson_raw: Mapped[str | None] = mapped_column(String(64))
    fee_category_raw: Mapped[str | None] = mapped_column(String(64))
    amount_raw: Mapped[str | None] = mapped_column(String(64))
    remark_raw: Mapped[str | None] = mapped_column(Text)
    bxd_no: Mapped[str | None] = mapped_column(String(64))
    sales_order: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal | None] = mapped_column(Money)
    issues: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)

    __table_args__ = (
        CheckConstraint("row_no >= 1", name="ck_maintenance_ledger_expense_row_no"),
        Index("ix_maintenance_ledger_expense_batch", "batch_id"),
        Index("ix_maintenance_ledger_expense_bxd", "bxd_no"),
    )
