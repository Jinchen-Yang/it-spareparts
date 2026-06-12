"""维度表：型号 / PN 别名 / 供应商 / 客户（§5）。"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
    # ---- 主数据治理（整改 P1）----
    # status：active=正常 / merged=已并入他档（墓碑行，pn_std 保留占位防复活）。
    # 「停用/排除」语义继续走 is_excluded，不另设 disabled 防双开关。
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("product_categories.id"))
    data_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    reviewed_at: Mapped[datetime | None] = mapped_column(TZDateTime)   # 人工确认时间（区分自动建档）
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
        # (id, pn_std) 冗余唯一：供 part_alias 复合外键强制「part_id 与 pn_std 文本」永远一致
        UniqueConstraint("id", "pn_std", name="uq_part_id_pn"),
        CheckConstraint("status IN ('active','merged')", name="ck_part_status"),
        CheckConstraint("merged_into_id IS NULL OR merged_into_id <> id", name="ck_part_no_self_merge"),
        CheckConstraint("(status = 'merged') = (merged_into_id IS NOT NULL)", name="ck_part_merged_pair"),
        Index("ix_part_brand", "brand_id"),
        Index("ix_part_category", "category_id"),
        Index("ix_part_merged_into", "merged_into_id",
              postgresql_where=text("merged_into_id IS NOT NULL")),
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
    # ---- 主数据治理（整改 P1）----
    # part_id：别名归属的商品实体；与 pn_std 的一致性由复合外键强制（漏改任一列即报错）。
    part_id: Mapped[int | None] = mapped_column()
    # status：active=生效（参与导入重定向）/ pending=待审 / rejected=已否决。
    # 单一写入口 loader 同时派生 needs_review 与 status，审核接口两列同改，防双真相源。
    status: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_alias_std", "pn_std"),
        Index("ix_alias_pn_compact_trgm", "pn_compact",
              postgresql_using="gin", postgresql_ops={"pn_compact": "gin_trgm_ops"}),
        ForeignKeyConstraint(["part_id", "pn_std"], ["dim_part.id", "dim_part.pn_std"],
                             name="fk_alias_part_pn"),
        CheckConstraint("status IS NULL OR status IN ('pending','active','rejected')",
                        name="ck_alias_status"),
        Index("ix_alias_part", "part_id"),
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
