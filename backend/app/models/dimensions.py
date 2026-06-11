"""维度表：型号 / PN 别名 / 供应商 / 客户（§5）。"""
from datetime import datetime

from sqlalchemy import Boolean, Computed, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime

# 检索用紧凑化表达式：大写 + 去全部非字母数字（"4089-RT" → "4089RT"）。
# 生成列不能引用其他生成列，故 search_doc 内联同一表达式。
_COMPACT_PN = "upper(regexp_replace(pn_std, '[^a-zA-Z0-9]', '', 'g'))"
_COMPACT_RAW = "upper(regexp_replace(pn_raw, '[^a-zA-Z0-9]', '', 'g'))"
_SEARCH_DOC = (
    f"pn_std || ' ' || {_COMPACT_PN}"
    " || ' ' || coalesce(brand, '')"
    " || ' ' || coalesce(category_major, '')"
    " || ' ' || coalesce(category_minor, '')"
    " || ' ' || coalesce(description, '')"
)


class DimPart(Base):
    __tablename__ = "dim_part"

    id: Mapped[int] = mapped_column(primary_key=True)
    pn_std: Mapped[str] = mapped_column(String(128), unique=True)
    pn_raw_sample: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(128))
    category_major: Mapped[str | None] = mapped_column(String(64))   # 仅取自销售表（§7.5）
    category_minor: Mapped[str | None] = mapped_column(String(128))
    machine_or_part: Mapped[str | None] = mapped_column(String(16))
    unit: Mapped[str | None] = mapped_column(String(16))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # 数据治理：标记为非标/不计入利润统计的型号（如"一批备件""配件"），及原因（#25）
    is_excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    exclude_reason: Mapped[str | None] = mapped_column(Text)
    # 近似检索（§二期）：STORED 生成列由库自动维护，loader 无需写入
    pn_compact: Mapped[str | None] = mapped_column(Text, Computed(_COMPACT_PN, persisted=True))
    search_doc: Mapped[str | None] = mapped_column(Text, Computed(_SEARCH_DOC, persisted=True))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_part_pn_compact", "pn_compact"),
        Index("ix_part_pn_compact_trgm", "pn_compact",
              postgresql_using="gin", postgresql_ops={"pn_compact": "gin_trgm_ops"}),
        Index("ix_part_search_doc_trgm", "search_doc",
              postgresql_using="gin", postgresql_ops={"search_doc": "gin_trgm_ops"}),
    )


class PartAlias(Base):
    """PN 原值↔标准值映射，可人工修订（§5/§7.1）。"""

    __tablename__ = "part_alias"

    id: Mapped[int] = mapped_column(primary_key=True)
    pn_raw: Mapped[str] = mapped_column(String(256), unique=True)
    pn_std: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # 近似检索：对"原值写法"模糊匹配后折叠到 pn_std（人工别名即刻生效于搜索）
    pn_compact: Mapped[str | None] = mapped_column(Text, Computed(_COMPACT_RAW, persisted=True))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_alias_std", "pn_std"),
        Index("ix_alias_pn_compact_trgm", "pn_compact",
              postgresql_using="gin", postgresql_ops={"pn_compact": "gin_trgm_ops"}),
    )


class DimSupplier(Base):
    __tablename__ = "dim_supplier"

    id: Mapped[int] = mapped_column(primary_key=True)
    name_raw: Mapped[str] = mapped_column(String(256), unique=True)   # 去重用 raw
    name_normalized: Mapped[str | None] = mapped_column(String(256))  # 去质保后缀；聚合用
    supplier_code: Mapped[str | None] = mapped_column(String(64))
    supplier_type: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (Index("ix_supplier_norm", "name_normalized"),)


class DimCustomer(Base):
    __tablename__ = "dim_customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name_raw: Mapped[str] = mapped_column(String(256), unique=True)
    name_normalized: Mapped[str | None] = mapped_column(String(256))
    customer_type: Mapped[str | None] = mapped_column(String(64))
    customer_source: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (Index("ix_customer_norm", "name_normalized"),)
