"""维度表：型号 / PN 别名 / 供应商 / 客户（§5）。"""
from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


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
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )


class PartAlias(Base):
    """PN 原值↔标准值映射，可人工修订（§5/§7.1）。"""

    __tablename__ = "part_alias"

    id: Mapped[int] = mapped_column(primary_key=True)
    pn_raw: Mapped[str] = mapped_column(String(256), unique=True)
    pn_std: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (Index("ix_alias_std", "pn_std"),)


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
