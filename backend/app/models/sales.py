"""事实表：销售订单头 / 销售明细行（含落库利润字段，§5/§7.3）。"""
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
from app.models._types import Margin, Money, Qty, Rate, TZDateTime


class FSalesOrder(Base):
    __tablename__ = "f_sales_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_no: Mapped[str] = mapped_column(String(64))
    order_date: Mapped[date | None] = mapped_column(Date)
    salesperson: Mapped[str | None] = mapped_column(String(64))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("dim_customer.id"))
    business_type: Mapped[str | None] = mapped_column(String(32))
    warehouse: Mapped[str | None] = mapped_column(String(64))
    amount_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    tax_rate: Mapped[Decimal | None] = mapped_column(Rate)
    data_status: Mapped[str | None] = mapped_column(String(16))
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (Index("ix_so_order_no", "order_no"),)


class FSalesLine(Base):
    __tablename__ = "f_sales_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_line_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("f_sales_order.id"))
    line_no: Mapped[int | None] = mapped_column(Integer)
    # 商品身份主键（聚合/过滤一律走 part_id）。pn_std/pn_raw 为导入原文痕迹，仅展示/追溯
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    pn_std: Mapped[str | None] = mapped_column(String(128))
    pn_raw: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(128))
    category_major: Mapped[str | None] = mapped_column(String(64))
    category_minor: Mapped[str | None] = mapped_column(String(128))
    machine_or_part: Mapped[str | None] = mapped_column(String(16))
    unit: Mapped[str | None] = mapped_column(String(16))
    qty: Mapped[Decimal | None] = mapped_column(Qty)
    unit_price: Mapped[Decimal | None] = mapped_column(Money)   # 含税单价
    line_amount: Mapped[Decimal | None] = mapped_column(Money)
    generic_product: Mapped[str | None] = mapped_column(String(256))
    serial_numbers: Mapped[str | None] = mapped_column(Text)
    # 利润结果（recompute 回填，§7.3）
    matched_cost: Mapped[Decimal | None] = mapped_column(Money)      # active 法(COST_METHOD)单位成本
    cost_moving_avg: Mapped[Decimal | None] = mapped_column(Money)   # 移动加权单位成本
    cost_fifo: Mapped[Decimal | None] = mapped_column(Money)         # FIFO 单位成本
    cost_source: Mapped[str | None] = mapped_column(String(16))      # moving_avg/fifo/fallback/none
    revenue_amount: Mapped[Decimal | None] = mapped_column(Money)
    cost_amount: Mapped[Decimal | None] = mapped_column(Money)
    gross_profit: Mapped[Decimal | None] = mapped_column(Money)
    gross_margin: Mapped[Decimal | None] = mapped_column(Margin)
    counts_revenue: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    anomaly_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))

    __table_args__ = (
        Index("ix_sl_part", "part_id"),
        Index("ix_sl_pn", "pn_std"),
        Index("ix_sl_flags", "anomaly_flags", postgresql_using="gin"),
    )
