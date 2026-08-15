"""回款提醒凭证（F6）：上传凭证是回款提醒关闭的依据。

口径（§2.1 十二问答复 + 计划 §3 F6）：
- 回款提醒关闭 = 上传凭证（巡检报告/图片/PDF）；
- DB 只记 md5 + 文件元数据（business_file 已记 sha256/大小/MIME/上传人）；
- 文件存独立目录，每文件一个 yml 元信息 sidecar；
- 作废/归档保留事实（软删除），不物理抹除。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class MaintenanceCollectionEvidence(Base):
    """一条回款提醒凭证 = 一个受控 business_file 到回款节点的证据链接。"""

    __tablename__ = "maintenance_collection_evidence"

    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    milestone_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_collection_milestone.milestone_id"), nullable=False
    )
    file_id: Mapped[str] = mapped_column(
        ForeignKey("business_file.file_id"), nullable=False
    )
    # 文件内容 MD5：与磁盘文件 + yml sidecar 一一对应，用于去重与审计。
    md5: Mapped[str] = mapped_column(String(32), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    archived_by: Mapped[str | None] = mapped_column(String(64))
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __table_args__ = (
        CheckConstraint(
            "md5 ~ '^[a-f0-9]{32}$'",
            name="ck_maintenance_collection_evidence_md5",
        ),
        CheckConstraint(
            "(is_active AND archived_at IS NULL AND archived_by IS NULL) OR "
            "(NOT is_active AND archived_at IS NOT NULL AND archived_by IS NOT NULL)",
            name="ck_maintenance_collection_evidence_archive_state",
        ),
        CheckConstraint(
            "version >= 1", name="ck_maintenance_collection_evidence_version"
        ),
        UniqueConstraint(
            "milestone_id", "file_id", name="uq_maintenance_collection_evidence_file"
        ),
        Index(
            "ix_maintenance_collection_evidence_milestone",
            "milestone_id",
            "is_active",
        ),
    )
