"""仓库作业分析投影。业务单头与明细使用各自的来源身份。"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WarehouseAnalyticsDocument(Base):
    __tablename__ = "warehouse_analytics_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(Text)
    number: Mapped[str] = mapped_column(Text)
    business_date: Mapped[date]
    source_modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    status: Mapped[str] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(Text)
    center: Mapped[str | None] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(Text)
    tester: Mapped[str | None] = mapped_column(Text)
    needs_test: Mapped[bool | None]
    tested: Mapped[bool | None]
    tested_date: Mapped[date | None]
    whole_pn: Mapped[str | None] = mapped_column(Text)
    whole_description: Mapped[str | None] = mapped_column(Text)
    whole_unit: Mapped[str | None] = mapped_column(Text)
    whole_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))

    __table_args__ = (
        CheckConstraint("source IN ('inbound','return','direct','outbound')", name="ck_warehouse_analytics_source"),
        UniqueConstraint("source", "source_id", name="uq_wa_document_identity"),
        UniqueConstraint("source", "number", name="uq_wa_document_number"),
        Index("ix_wa_document_status_date", "status", "business_date", "source"),
    )


class WarehouseAnalyticsLine(Base):
    __tablename__ = "warehouse_analytics_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("warehouse_analytics_document.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(Text)
    pn: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    category: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    warehouse: Mapped[str | None] = mapped_column(Text)
    bin_name: Mapped[str | None] = mapped_column(Text)
    test_result: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("document_id", "source_id", name="uq_wa_line_identity"),
        Index("ix_wa_line_source_id", "source_id"),
        Index("ix_wa_line_pn_document", "pn", "document_id"),
    )
