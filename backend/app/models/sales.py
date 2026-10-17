"""事实表：销售订单头 / 销售明细行（含落库利润字段，§5/§7.3）/ 已对账快照（D-40）。"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
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
    # 含税三件套（D-41 §3：三金额并存）。口径与 f_purchase_order 同名列一致；
    # 导入写入与历史回填在 PR-2，本层只落列，空值 NULL 不当 0、不用推算值冒充。
    is_tax_inclusive: Mapped[bool | None] = mapped_column(Boolean)
    tax_amount: Mapped[Decimal | None] = mapped_column(Money)
    amount_inc_tax: Mapped[Decimal | None] = mapped_column(Money)  # 原列「订单金额」
    data_status: Mapped[str | None] = mapped_column(String(16))
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_so_order_no", "order_no"),
        # 高频过滤+排序：业务查询按 已生效 过滤、按 order_date 排序/窗口（架构体检 2026-06-29）
        Index("ix_so_status_date", "data_status", "order_date"),
    )


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
        # 看板 v2：订单列表按 order_id IN (当页) 批量装配 parts 的热路径（FK 不自动建索引）
        Index("ix_sl_order", "order_id", "id"),
    )


class FSalesReconciliationSnapshot(Base):
    """销售单已对账快照（D-40）：氚云导出单头的 已对账/待对账/退货已对账 是订单级
    **累计快照**，快照只追加、不覆盖；"最新值"取
    source_modified_at DESC NULLS LAST、其次 import_batch_id DESC、再次 id DESC
    （见 services/sales_reconciliation）。

    每次导入/回填对每个订单记**一条观测**，(raw_order_id, import_batch_id) 唯一：
    同一批次重放/回填重跑不产生重复行（ON CONFLICT 写入由 PR-2 导入落实）；不同
    批次各记一条（即使内容相同）——金额序列 A→B→A 的最后一次 A 不会被内容去重
    吞掉。content_hash 保留用于识别内容变化/报告，不参与唯一约束。
    四个金额空单元格一律 NULL，绝不当 0。
    """

    __tablename__ = "f_sales_reconciliation_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 氚云数据ID，与 f_sales_order.raw_order_id 同源；快照允许先于订单入库
    raw_order_id: Mapped[str] = mapped_column(String(64))
    order_no: Mapped[str | None] = mapped_column(String(64))
    # 可空：出现该快照时订单可能尚未入库/已被其他流程处理
    sales_order_id: Mapped[int | None] = mapped_column(ForeignKey("f_sales_order.id"))
    # 该观测所属的导入/回填批次（每批次每单至多一条观测，见唯一约束）
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    # import=常规导入，backfill=历史原件回填（D-40 §7）
    source: Mapped[str] = mapped_column(String(16))
    # 氚云「修改时间」；旧格式文件没有该列 → NULL
    source_modified_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    order_amount: Mapped[Decimal | None] = mapped_column(Money)
    reconciled_amount: Mapped[Decimal | None] = mapped_column(Money)
    pending_amount: Mapped[Decimal | None] = mapped_column(Money)
    return_reconciled_amount: Mapped[Decimal | None] = mapped_column(Money)
    # 规范化 sha256，规则单一事实源在 services/sales_reconciliation.snapshot_content_hash；
    # 仅供识别内容变化/报告，不参与唯一约束
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "source IN ('import', 'backfill')",
            name="ck_sales_recon_snapshot_source",
        ),
        UniqueConstraint(
            "raw_order_id", "import_batch_id", name="uq_sales_recon_snapshot_order_batch"
        ),
        # 取每单最新：与查询排序键 (source_modified_at DESC NULLS LAST,
        # import_batch_id DESC, id DESC) 对齐。PG 索引列裸 DESC 默认是 NULLS FIRST，
        # 必须显式 NULLS LAST 才能消排序；import_batch_id 与 id 均非空，
        # 裸 DESC 与 NULLS LAST 等价
        Index(
            "ix_sales_recon_snapshot_latest",
            "raw_order_id",
            desc("source_modified_at").nulls_last(),
            desc("import_batch_id"),
            desc("id"),
        ),
        # FK 查询路径（PG 不自动为 FK 建索引）
        Index("ix_sales_recon_snapshot_batch", "import_batch_id"),
    )
