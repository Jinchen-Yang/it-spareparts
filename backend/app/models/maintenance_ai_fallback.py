"""AI 兜底列映射提案 staging（C3）。

架构定位（已确认）：AI 只做「列映射提案」，绝不计算派生值、绝不直写业务表。
- 单步 LLM + 封闭 canonical 字段目录（AI 不能发明字段）；
- 提案只进 staging，人工确认后走与 py 路径完全相同的解析/校验/写入；
- 未配置 LLM 时优雅降级为人工映射模板，主路径正确性不依赖 AI。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class MaintenanceAiMappingProposal(Base):
    """一次 AI 列映射提案：表头快照、样本、提案与采纳状态。"""

    __tablename__ = "maintenance_ai_mapping_proposal"

    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(24), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    header_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    sample_rows: Mapped[list] = mapped_column(JSONB, nullable=False)
    # 提案：{column_mapping: {表头: canonical_field}, notes: [...]}
    proposal: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="'pending'"
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    accepted_batch_id: Mapped[str | None] = mapped_column(String(36))
    accepted_by: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint(
            "doc_type IN ('ckd_shipment', 'rkd_inbound', 'return_order',"
            " 'bxd_expense', 'ledger')",
            name="ck_maintenance_ai_proposal_doc_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_maintenance_ai_proposal_status",
        ),
        CheckConstraint(
            "(status = 'accepted') = (accepted_at IS NOT NULL"
            " AND accepted_by IS NOT NULL)",
            name="ck_maintenance_ai_proposal_accepted",
        ),
        Index("ix_maintenance_ai_proposal_hash", "file_hash"),
        Index("ix_maintenance_ai_proposal_type_created", "doc_type", "created_at"),
    )
