"""事实表：维保订单头（WBDD 备件出库需求）/ 维保出库明细行（维保项目成本核算）。

维保出库导出本身没有任何成本/金额列（实测 90 列零命中）——成本由
services/maintenance_cost.recompute 按取价瀑布回填明细行的 unit_cost 等字段。
口径详见 docs/维保出库成本核算-开发方案.md。
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Computed,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, TZDateTime


_VALID_MAINTENANCE_COST_SQL = (
    "cost_amount IS NOT NULL"
    " AND cost_amount >= 0"
    " AND cost_amount < 1000000000000"
)
_ACTUAL_MAINTENANCE_SOURCE_SQL = (
    "cost_source IN ('direct', 'month_avg', 'window')"
)
_ESTIMATED_MAINTENANCE_SOURCE_SQL = (
    "cost_source IN ("
    "'pool_purchase', 'pool_sales', 'purchase_history', 'sales_history',"
    " 'sales_ref', 'trace_avg'"
    ")"
)
MAINTENANCE_COST_BUCKET_SQL = (
    "CASE"
    f" WHEN {_VALID_MAINTENANCE_COST_SQL}"
    f" AND {_ACTUAL_MAINTENANCE_SOURCE_SQL}"
    " AND cost_tax_basis = 'inc' THEN 1"
    f" WHEN {_VALID_MAINTENANCE_COST_SQL}"
    f" AND {_ACTUAL_MAINTENANCE_SOURCE_SQL}"
    " AND cost_tax_basis = 'ex' THEN 2"
    f" WHEN {_VALID_MAINTENANCE_COST_SQL}"
    f" AND {_ESTIMATED_MAINTENANCE_SOURCE_SQL}"
    " AND cost_tax_basis = 'inc' AND confidence = 'low' THEN 3"
    f" WHEN {_VALID_MAINTENANCE_COST_SQL}"
    f" AND {_ESTIMATED_MAINTENANCE_SOURCE_SQL}"
    " AND cost_tax_basis = 'inc' THEN 4"
    f" WHEN {_VALID_MAINTENANCE_COST_SQL}"
    f" AND {_ESTIMATED_MAINTENANCE_SOURCE_SQL}"
    " AND cost_tax_basis = 'ex' AND confidence = 'low' THEN 5"
    f" WHEN {_VALID_MAINTENANCE_COST_SQL}"
    f" AND {_ESTIMATED_MAINTENANCE_SOURCE_SQL}"
    " AND cost_tax_basis = 'ex' THEN 6"
    " ELSE 0 END"
)


class FMaintenanceOrder(Base):
    __tablename__ = "f_maintenance_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_no: Mapped[str] = mapped_column(String(64))                    # WBDD-xxxx
    order_date: Mapped[date | None] = mapped_column(Date)                # 制单日期
    linked_sales_order_no: Mapped[str | None] = mapped_column(String(64))  # XSDD，连合同/项目
    project_raw: Mapped[str | None] = mapped_column(String(256))         # 项目名原值
    project_std: Mapped[str | None] = mapped_column(String(256))         # 剥「预交付-」前缀，聚合键
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("dim_customer.id"))
    end_customer: Mapped[str | None] = mapped_column(String(256))
    demand_type: Mapped[str | None] = mapped_column(String(16))          # 报修供货/补库供货
    business_type: Mapped[str | None] = mapped_column(String(16))        # 备件维保/整体维保/算力运维
    salesperson: Mapped[str | None] = mapped_column(String(64))
    warehouse: Mapped[str | None] = mapped_column(String(64))            # 出库仓库
    maint_start: Mapped[date | None] = mapped_column(Date)
    maint_end: Mapped[date | None] = mapped_column(Date)
    data_status: Mapped[str | None] = mapped_column(String(16))
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_mo_order_no", "order_no"),
        Index("ix_mo_linked", "linked_sales_order_no"),
        Index("ix_mo_project", "project_std"),
        Index("ix_mo_status_date", "data_status", "order_date"),
    )


class FMaintenanceLine(Base):
    __tablename__ = "f_maintenance_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_line_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("f_maintenance_order.id"))
    line_no: Mapped[int | None] = mapped_column(Integer)
    # 商品身份主键（聚合/取价一律走 part_id）。pn_std/pn_raw 为导入原文痕迹，仅展示/追溯
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    pn_std: Mapped[str | None] = mapped_column(String(128))
    pn_raw: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[Decimal | None] = mapped_column(Qty)                     # 需求数量
    return_qty: Mapped[Decimal | None] = mapped_column(Qty)              # 退货数量（冲抵）
    serial_numbers: Mapped[str | None] = mapped_column(Text)
    # 成本结果（maintenance_cost.recompute 回填；loader upsert 白名单排除，同 matched_cost 约定）
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    cost_amount: Mapped[Decimal | None] = mapped_column(Money)           # (qty-return_qty)×unit_cost
    # 双税计算底座：所有成功取价来源均同时落两套结果；legacy 字段继续保留旧口径与兼容展示。
    unit_cost_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    unit_cost_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    cost_amount_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    cost_amount_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    # 旧五层：direct/window/month_avg/trace_avg/sales_ref；
    # 缺失补价：pool_purchase/pool_sales/purchase_history/sales_history；最终 none。
    # 起算日(MAINT_COST_START_DATE)之前的行恒为 NULL（不计价，区别于"算了但没算出来"的 none）
    cost_source: Mapped[str | None] = mapped_column(String(16))
    cost_tax_basis: Mapped[str | None] = mapped_column(String(4))        # inc/ex（原值口径，Q4）
    price_month: Mapped[str | None] = mapped_column(String(7))           # 取价月份 YYYY-MM
    trace_months: Mapped[int | None] = mapped_column(SmallInteger)       # 0=当月；≥1 前端须标注
    linked_purchase_order_no: Mapped[str | None] = mapped_column(String(64))  # direct 命中的采购单
    # v2（§16.1）：window 层取价日距出库日的天数（direct=0，window=0..7，其余 NULL）
    price_distance_days: Mapped[int | None] = mapped_column(SmallInteger)
    # v2：置信度 high(direct/window 近乎精确)/medium(当月加权)/low(追溯/销售参考，中位偏差 25%+)
    confidence: Mapped[str | None] = mapped_column(String(8))
    # 可审计取价证据。旧五层按可得信息回填；来源单日期缺失等无法证明的证据保持 NULL，
    # 对应双税成本 fail-closed，不伪造采购日期。
    reference_side: Mapped[str | None] = mapped_column(String(16))
    reference_pool_group_id: Mapped[int | None] = mapped_column(Integer)
    reference_pool_version: Mapped[int | None] = mapped_column(Integer)
    reference_sample_count: Mapped[int | None] = mapped_column(Integer)
    reference_from_date: Mapped[date | None] = mapped_column(Date)
    reference_to_date: Mapped[date | None] = mapped_column(Date)
    reference_latest_date: Mapped[date | None] = mapped_column(Date)
    # 查询加速用 schema snapshot：0=missing；其余桶的唯一业务解释在
    # services.maintenance_cost_quality。生成列只缓存严格分类，不接受导入/接口写入。
    cost_bucket: Mapped[int] = mapped_column(
        SmallInteger,
        Computed(MAINTENANCE_COST_BUCKET_SQL, persisted=True),
    )
    anomaly_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))

    __table_args__ = (
        Index("ix_ml_order", "order_id"),
        Index("ix_ml_part", "part_id"),
    )


class FProjectExpense(Base):
    """维保报销单（BXD）费用行（§16.3）：经 XSDD 归集到合同/项目，盈亏看板"已花"的费用侧。

    正式数据源=项目追踪工作簿的报销明细页；数据ID仅作历史兼容，无数据ID时以
    bxd_no#line_no 复合键幂等。
    金额在行级；生效口径 流程状态=MAINT_EXPENSE_ACTIVE_STATUS（'已结束'）。
    """

    __tablename__ = "f_project_expense"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_line_id: Mapped[str] = mapped_column(String(80), unique=True)    # 数据ID 或 bxd_no#line_no
    bxd_no: Mapped[str | None] = mapped_column(String(64))               # 数据标题正则提取 BXD-\d{8}-\d+
    line_no: Mapped[int | None] = mapped_column(Integer)
    data_status: Mapped[str | None] = mapped_column(String(16))          # 流程状态
    expense_date: Mapped[date | None] = mapped_column(Date)              # 报销日期
    person: Mapped[str | None] = mapped_column(String(64))               # 报销人员
    expense_type: Mapped[str | None] = mapped_column(String(64))         # 报销类别（头）
    fee_category: Mapped[str | None] = mapped_column(String(64))         # 费用分类（行）
    reason: Mapped[str | None] = mapped_column(Text)                     # 支出事由
    linked_sales_order_no: Mapped[str | None] = mapped_column(String(64))  # XSDD，项目/合同归集键
    amount: Mapped[Decimal | None] = mapped_column(Money)                # 报销金额（行级）
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_pe_bxd", "bxd_no"),
        Index("ix_pe_linked", "linked_sales_order_no"),
        Index("ix_pe_status_date", "data_status", "expense_date"),
    )
