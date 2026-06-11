"""历史询价（备件汇总.xlsx，行级 hash 去重，仅作报价参考，§5）。"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, TZDateTime


class FPartInquiry(Base):
    __tablename__ = "f_part_inquiry"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_row_hash: Mapped[str] = mapped_column(String(64), unique=True)  # SHA256 行级去重
    apply_time: Mapped[datetime | None] = mapped_column(TZDateTime)
    parts_type: Mapped[str | None] = mapped_column(String(64))
    pn_raw: Mapped[str | None] = mapped_column(String(256))
    pn_std: Mapped[str | None] = mapped_column(String(128))
    # 商品身份（可空：询价只是报价参考，不强制建档；refresh 时按 pn_std 回填）
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    description: Mapped[str | None] = mapped_column(Text)
    money: Mapped[Decimal | None] = mapped_column(Money)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))

    __table_args__ = (
        Index("ix_inq_pn", "pn_std"),
        Index("ix_inq_part", "part_id"),
    )
