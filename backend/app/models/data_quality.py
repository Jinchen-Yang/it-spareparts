"""采购/销售事实行级疑点（DEV-05A）。"""
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class FactDataQualityIssue(Base):
    """一条事实行在一条检测规则下的当前疑点状态。

    ``line_id`` 是按 ``side`` 解释的多态定位键，事实表定位和批次/PN 归属只能由
    服务层从原始事实行派生，调用方不能自行指定，避免伪造追溯关系。
    """

    __tablename__ = "fact_data_quality_issue"

    id: Mapped[int] = mapped_column(primary_key=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    line_id: Mapped[int] = mapped_column(Integer, nullable=False)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("sys_import_batch.id"))
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open",
                                        server_default="open")
    detected_by: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False,
                                                   server_default=func.now())
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    review_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False,
                                                  server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False,
                                                  server_default=func.now())

    __table_args__ = (
        UniqueConstraint("side", "line_id", "rule_code", name="uq_fact_dq_issue_current"),
        CheckConstraint("side IN ('purchase','sales')", name="ck_fact_dq_issue_side"),
        CheckConstraint(
            "status IN ('open','confirmed_valid','confirmed_source_error','source_changed')",
            name="ck_fact_dq_issue_status",
        ),
        CheckConstraint("version >= 1", name="ck_fact_dq_issue_version"),
        Index("ix_fact_dq_issue_status_updated", "status", "updated_at"),
        Index("ix_fact_dq_issue_part_status", "part_id", "status"),
        Index("ix_fact_dq_issue_batch", "import_batch_id"),
    )
