"""实时检索投影（M1，迁移 c4e6a8b1d3f5）。

两张表都由数据库触发器维护（dim_part / product_specs / part_alias 写入时同事务刷新），
应用代码只读；这里登记 ORM 模型是为了让 alembic check 与迁移结构一致。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SpecKeyLabel(Base):
    """规格键中文标签（与 services/spec_extract.SPEC_KEYS 一致，由测试守护）。"""

    __tablename__ = "spec_key_label"

    spec_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text)


class PartSearchText(Base):
    """每个非墓碑型号一行检索文本；墓碑删除投影行。"""

    __tablename__ = "part_search_text"

    part_id: Mapped[int] = mapped_column(
        ForeignKey("dim_part.id", ondelete="CASCADE"), primary_key=True)
    doc: Mapped[str] = mapped_column(Text, nullable=False)
    doc_compact: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_part_search_text_doc_trgm", "doc",
              postgresql_using="gin", postgresql_ops={"doc": "gin_trgm_ops"}),
        Index("ix_part_search_text_doc_compact_trgm", "doc_compact",
              postgresql_using="gin", postgresql_ops={"doc_compact": "gin_trgm_ops"}),
    )
