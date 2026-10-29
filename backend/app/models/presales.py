"""售前询价、当前报价和只追加报价快照。"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, TZDateTime


class PresalesWorkbookExport(Base):
    """只存导出授权范围，不保存备注、价格或自定义字段原值。"""

    __tablename__ = "presales_workbook_export"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    schema_name: Mapped[str] = mapped_column(String(64))
    export_id: Mapped[str] = mapped_column(String(128), unique=True)
    actor: Mapped[str] = mapped_column(String(64), ForeignKey("sys_user.username"))
    scope: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (Index("ix_presales_workbook_export_actor_schema", "actor", "schema_name"),)


class PresalesInquiry(Base):
    __tablename__ = "presales_inquiry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    inquiry_no: Mapped[str] = mapped_column(String(16), unique=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("dim_customer.id"))
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    offer_audience_rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    notes_audience_rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    assignee_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    # 只保存上次提交的行身份和影响报价的字段；撤回编辑不覆盖基线。
    submitted_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_by: Mapped[int] = mapped_column(ForeignKey("sys_user.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), onupdate=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    closed_outcome: Mapped[str | None] = mapped_column(String(16))
    won_offer_id: Mapped[int | None] = mapped_column(ForeignKey(
        "presales_offer.id", use_alter=True, name="fk_presales_inquiry_won_offer",
    ))
    won_offer_revision_no: Mapped[int | None] = mapped_column(Integer)
    # D-0047：优先级两档；催办只记次数与最近时间，30 分钟间隔由服务层校验。
    priority: Mapped[str] = mapped_column(String(8), default="normal", server_default="normal")
    urge_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_urged_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint("kind IN ('sales_inquiry', 'proactive_offer')", name="ck_presales_inquiry_kind"),
        CheckConstraint("priority IN ('normal', 'urgent')", name="ck_presales_inquiry_priority"),
        CheckConstraint("status IN ('draft', 'submitted', 'revising', 'closed')", name="ck_presales_inquiry_status"),
        CheckConstraint("revision >= 1 AND version >= 1", name="ck_presales_inquiry_version"),
        CheckConstraint("closed_outcome IS NULL OR closed_outcome IN ('won', 'lost', 'cancelled')", name="ck_presales_inquiry_outcome"),
        Index("ix_presales_inquiry_owner_updated", "created_by", "updated_at"),
        Index("ix_presales_inquiry_kind_status", "kind", "status"),
    )


class PresalesInquiryLine(Base):
    __tablename__ = "presales_inquiry_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("presales_inquiry.id"))
    line_key: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    pn_raw: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[Decimal] = mapped_column(Qty)
    require_exact_pn: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    expected_delivery: Mapped[date | None] = mapped_column(Date)
    ref_offer_id: Mapped[int | None] = mapped_column(ForeignKey(
        "presales_offer.id", use_alter=True, name="fk_presales_line_ref_offer",
    ))
    ref_offer_revision_no: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    notes: Mapped[str | None] = mapped_column(Text)
    notes_audience_rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    return_defective: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("inquiry_id", "line_key", name="uq_presales_line_key"),
        CheckConstraint("seq >= 1 AND version >= 1 AND qty > 0", name="ck_presales_line_values"),
        CheckConstraint("status IN ('active', 'deleted')", name="ck_presales_line_status"),
        Index("ix_presales_line_inquiry_seq", "inquiry_id", "seq"),
    )


class PresalesOffer(Base):
    __tablename__ = "presales_offer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inquiry_id: Mapped[int | None] = mapped_column(ForeignKey("presales_inquiry.id"))
    line_id: Mapped[int | None] = mapped_column(ForeignKey("presales_inquiry_line.id"))
    # 无单据主动报价的整条受众；有来源单据的报价继续使用来源单受众。
    offer_audience_rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    work_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("work_item.id"))
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    unit_price: Mapped[Decimal | None] = mapped_column(Money)
    is_tax_inclusive: Mapped[bool | None] = mapped_column(Boolean)
    no_stock: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    qty_available: Mapped[Decimal | None] = mapped_column(Qty)
    stock_location: Mapped[str | None] = mapped_column(String(256))
    lead_time: Mapped[str | None] = mapped_column(String(256))
    source_key: Mapped[str] = mapped_column(String(128), default="", server_default="")
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("dim_supplier.id"))
    # D-0046 起报价不设有效期：validity_days / valid_until 停用，仅为回滚保留，不参与任何判断。
    validity_days: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Excel 草稿保留采购明确选择；发布时清空，业务可见性出口不返回此内部数据。
    workbook_choice: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    valid_from: Mapped[datetime | None] = mapped_column(TZDateTime)
    valid_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    supplier_audience_rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    reporter_audience_rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    notes_audience_rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    # D-0047：成本价 / 售价只作标注，可见性仍按报价受众规则。
    price_basis: Mapped[str] = mapped_column(String(8), default="sale", server_default="sale")
    revision_no: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[int] = mapped_column(ForeignKey("sys_user.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("(inquiry_id IS NULL) = (line_id IS NULL)", name="ck_presales_offer_origin_pair"),
        CheckConstraint("status IN ('draft', 'published', 'withdrawn', 'stale')", name="ck_presales_offer_status"),
        CheckConstraint("price_basis IN ('sale', 'cost')", name="ck_presales_offer_price_basis"),
        CheckConstraint("revision_no >= 0 AND version >= 1 AND validity_days >= 1", name="ck_presales_offer_version"),
        CheckConstraint("unit_price IS NULL OR unit_price >= 0", name="ck_presales_offer_price"),
        CheckConstraint("qty_available IS NULL OR qty_available >= 0", name="ck_presales_offer_qty"),
        Index("ix_presales_offer_part_validity", "part_id", "status", "valid_until"),
        Index("ix_presales_offer_inquiry_handler", "inquiry_id", "created_by"),
        Index("ix_presales_offer_handler_source", "created_by", "part_id", "source_key"),
    )


class PresalesOfferLineLink(Base):
    """跨单更新保留原行关联，并钉住迁移前的报价版本。"""
    __tablename__ = "presales_offer_line_link"

    line_id: Mapped[int] = mapped_column(ForeignKey("presales_inquiry_line.id"), primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("presales_offer.id"), primary_key=True)
    offer_revision_no: Mapped[int | None] = mapped_column(Integer)


class PresalesLineResponse(Base):
    """每位采购的行处理结果；沿用只保存关联，不保存价格副本。"""
    __tablename__ = "presales_line_response"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("presales_inquiry_line.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("sys_user.id"))
    work_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("work_item.id"))
    line_version: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(8))
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    ref_offer_id: Mapped[int | None] = mapped_column(ForeignKey("presales_offer.id"))
    ref_offer_revision_no: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("line_id", "created_by", name="uq_presales_line_response_author"),
        CheckConstraint("line_version >= 1", name="ck_presales_line_response_version"),
        CheckConstraint("(mode = 'reuse' AND ref_offer_id IS NOT NULL AND reason IS NULL) OR "
                        "(mode = 'skip' AND ref_offer_id IS NULL AND reason IS NOT NULL AND length(trim(reason)) > 0)",
                        name="ck_presales_line_response_mode"),
    )


class PresalesOfferRevision(Base):
    __tablename__ = "presales_offer_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("presales_offer.id"))
    revision_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    changed_fields: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[int] = mapped_column(ForeignKey("sys_user.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("offer_id", "revision_no", name="uq_presales_offer_revision"),
        CheckConstraint("revision_no >= 1", name="ck_presales_offer_revision_no"),
        Index("ix_presales_offer_revision_created", "created_at", "offer_id"),
    )


class OfferPriceDaily(Base):
    __tablename__ = "offer_price_daily"

    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    min: Mapped[Decimal] = mapped_column(Money)
    max: Mapped[Decimal] = mapped_column(Money)
    avg: Mapped[Decimal] = mapped_column(Money)
    count: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("count > 0", name="ck_offer_price_daily_count"),
    )
