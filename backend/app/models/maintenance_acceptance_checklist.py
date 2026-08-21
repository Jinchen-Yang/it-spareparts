"""验收需求清单 Excel 导入（2026-08-21 客户反馈）。

与验收交付（maintenance_manager.MaintenanceAcceptanceDeliverable 的附件+审批流）
互不阻塞：清单是「验收需求 / 是否完成」的行级事实，交付是文件与审批状态。
建模复刻 maintenance_doc_import 范式——批次表 + 行表（raw_json 全量保留 +
归一化列），apply 即整表替换当前清单（旧批次留档为历史，replaced_batch_id 链）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class MaintenanceAcceptanceChecklistBatch(Base):
    """一次验收清单上传批次（两阶段：preview 落 raw → apply 生效）。"""

    __tablename__ = "maintenance_acceptance_checklist_batch"

    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    item_rows: Mapped[int] = mapped_column(
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
    # apply 时指向上一个生效批次（历史链头）；首版为 NULL
    replaced_batch_id: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        CheckConstraint(
            "item_rows >= 0", name="ck_acceptance_checklist_batch_item_rows"
        ),
        CheckConstraint(
            "issue_rows >= 0", name="ck_acceptance_checklist_batch_issue_rows"
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_acceptance_checklist_batch_status",
        ),
        CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL AND applied_by IS NOT NULL)",
            name="ck_acceptance_checklist_batch_applied",
        ),
        UniqueConstraint(
            "uploaded_by",
            "idempotency_key",
            name="uq_acceptance_checklist_batch_idempotency",
        ),
        Index("ix_acceptance_checklist_batch_hash", "file_hash"),
        Index(
            "ix_acceptance_checklist_batch_project",
            "project_id",
            "uploaded_at",
        ),
    )


class MaintenanceAcceptanceChecklistItem(Base):
    """清单行：raw_json 保留原始单元格，requirement/done 归一化供展示。"""

    __tablename__ = "maintenance_acceptance_checklist_item"

    item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_acceptance_checklist_batch.batch_id"),
        nullable=False,
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    done: Mapped[bool | None] = mapped_column(Boolean)
    issues: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(128)), nullable=True, default=list
    )

    __table_args__ = (
        CheckConstraint("row_no >= 1", name="ck_acceptance_checklist_item_row_no"),
        Index("ix_acceptance_checklist_item_batch", "batch_id"),
    )
