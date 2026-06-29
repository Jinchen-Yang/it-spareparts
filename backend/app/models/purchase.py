"""事实表：采购订单头 / 采购明细行（§5）。"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, Rate, TZDateTime


class FPurchaseOrder(Base):
    __tablename__ = "f_purchase_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_no: Mapped[str] = mapped_column(String(64))
    order_date: Mapped[date | None] = mapped_column(Date)
    purchaser: Mapped[str | None] = mapped_column(String(64))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("dim_supplier.id"))
    linked_sales_order_no: Mapped[str | None] = mapped_column(String(64))
    source_type: Mapped[str | None] = mapped_column(String(32))      # 标准化：销售订单/维保需求/指定采购
    source_type_raw: Mapped[str | None] = mapped_column(String(64))  # 原值留存
    amount_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    tax_rate: Mapped[Decimal | None] = mapped_column(Rate)
    # 含税口径（采购分析/含税未税切换）：氚云采购订单原列「是否含税/税金/采购金额(含税总额)」。
    # 行单价口径跟随 is_tax_inclusive：含税单 unit_price=含税价、不含单=未税价（见 services.purchase_analysis）。
    is_tax_inclusive: Mapped[bool | None] = mapped_column(Boolean)
    tax_amount: Mapped[Decimal | None] = mapped_column(Money)
    amount_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    data_status: Mapped[str | None] = mapped_column(String(16))
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_po_order_no", "order_no"),
        Index("ix_po_linked", "linked_sales_order_no"),
        # 高频过滤+排序：业务查询按 已生效 过滤、按 order_date 排序/窗口（架构体检 2026-06-29）
        Index("ix_po_status_date", "data_status", "order_date"),
    )


class FPurchaseLine(Base):
    __tablename__ = "f_purchase_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_line_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("f_purchase_order.id"))
    line_no: Mapped[int | None] = mapped_column(Integer)
    # 商品身份主键（聚合/过滤一律走 part_id）。pn_std/pn_raw 为导入原文痕迹，仅展示/追溯
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    pn_std: Mapped[str | None] = mapped_column(String(128))
    pn_raw: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(128))
    machine_or_part: Mapped[str | None] = mapped_column(String(16))
    unit: Mapped[str | None] = mapped_column(String(16))
    qty: Mapped[Decimal | None] = mapped_column(Qty)
    unit_price: Mapped[Decimal | None] = mapped_column(Money)
    line_amount: Mapped[Decimal | None] = mapped_column(Money)
    recent_purchase_price: Mapped[Decimal | None] = mapped_column(Money)
    anomaly_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))

    __table_args__ = (
        Index("ix_pl_part", "part_id"),
        Index("ix_pl_pn", "pn_std"),
    )
