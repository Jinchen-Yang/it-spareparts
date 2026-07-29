"""事实表：维保订单头（WBDD 备件出库需求）/ 维保出库明细行（维保项目成本核算）。

维保出库导出本身没有任何成本/金额列（实测 90 列零命中）——成本由
services/maintenance_cost.recompute 按取价瀑布回填明细行的 unit_cost 等字段。
口径详见 docs/维保出库成本核算-开发方案.md。
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, Rate, TZDateTime


_VALID_MAINTENANCE_COST_SQL = (
    "cost_amount IS NOT NULL"
    " AND cost_amount >= 0"
    " AND cost_amount < 1000000000000"
)
_ACTUAL_MAINTENANCE_SOURCE_SQL = (
    "cost_source IN ('direct', 'month_avg', 'window', 'manual')"
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
    # 财务计算固定按 13% 同时保留双口径；amount 继续保留员工实际填入的原值作审计。
    amount_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    amount_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    tax_basis: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="default_ex",
        server_default="default_ex",
    )
    tax_rate_used: Mapped[Decimal] = mapped_column(
        Rate,
        nullable=False,
        default=Decimal("0.13"),
        server_default="0.13",
    )
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "tax_basis IN ('default_ex', 'ex', 'inc')",
            name="ck_project_expense_tax_basis",
        ),
        CheckConstraint(
            "tax_rate_used = 0.13",
            name="ck_project_expense_tax_rate_used",
        ),
        CheckConstraint(
            """
            (
                amount IS NULL
                AND amount_ex_tax IS NULL
                AND amount_inc_tax IS NULL
            )
            OR (
                amount IS NOT NULL
                AND amount_ex_tax IS NOT NULL
                AND amount_inc_tax IS NOT NULL
            )
            """,
            name="ck_project_expense_tax_amount_presence",
        ),
        CheckConstraint(
            """
            (amount_ex_tax IS NULL AND amount_inc_tax IS NULL)
            OR (
                tax_basis IN ('default_ex', 'ex')
                AND amount_inc_tax
                    = round(amount_ex_tax * NUMERIC '1.13', 2)
            )
            OR (
                tax_basis = 'inc'
                AND amount_ex_tax
                    = round(amount_inc_tax / NUMERIC '1.13', 2)
            )
            """,
            name="ck_project_expense_tax_amounts_match",
        ),
        CheckConstraint(
            """
            (
                amount IS NULL
                AND amount_ex_tax IS NULL
                AND amount_inc_tax IS NULL
            )
            OR (
                tax_basis IN ('default_ex', 'ex')
                AND amount = amount_ex_tax
            )
            OR (
                tax_basis = 'inc'
                AND amount = amount_inc_tax
            )
            """,
            name="ck_project_expense_amount_matches_basis",
        ),
        Index("ix_pe_bxd", "bxd_no"),
        Index("ix_pe_linked", "linked_sales_order_no"),
        Index("ix_pe_status_date", "data_status", "expense_date"),
    )


class MaintenanceManualCostOverride(Base):
    """自动成本瀑布仍缺失时，管理员对单条维保明细提供的可审计成本证据。"""

    __tablename__ = "maintenance_manual_cost_override"

    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(
        ForeignKey("f_maintenance_line.id"),
        nullable=False,
        unique=True,
    )
    unit_cost_ex_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    unit_cost_inc_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    tax_rate_used: Mapped[Decimal] = mapped_column(
        Rate,
        nullable=False,
        default=Decimal("0.13"),
        server_default="0.13",
    )
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "unit_cost_ex_tax >= 0 AND unit_cost_ex_tax < 1000000000000",
            name="ck_maintenance_manual_cost_ex",
        ),
        CheckConstraint(
            "unit_cost_inc_tax >= 0 AND unit_cost_inc_tax < 1000000000000",
            name="ck_maintenance_manual_cost_inc",
        ),
        CheckConstraint(
            "tax_rate_used = 0.13",
            name="ck_maintenance_manual_cost_tax_rate",
        ),
        CheckConstraint(
            """
            unit_cost_inc_tax
                = round(unit_cost_ex_tax * NUMERIC '1.13', 2)
            """,
            name="ck_maintenance_manual_cost_tax_amounts_match",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_maintenance_manual_cost_version",
        ),
        Index(
            "ix_maintenance_manual_cost_override_active",
            "active",
            "line_id",
        ),
    )


class MaintenanceContractWorkbookState(Base):
    """合同往返工作簿状态；完整快照声明是贡献毛利发布的费用侧门禁。"""

    __tablename__ = "maintenance_contract_workbook_state"

    contract_no: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    expense_complete_through: Mapped[date | None] = mapped_column(Date)
    expense_snapshot_complete: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    last_export_id: Mapped[str | None] = mapped_column(String(64))
    last_import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("sys_import_batch.id"),
    )
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "revision >= 1",
            name="ck_maintenance_contract_workbook_state_revision",
        ),
        Index(
            "ix_maintenance_contract_workbook_state_complete",
            "expense_snapshot_complete",
            "contract_no",
        ),
    )


class MaintenanceRoundtripOperation(Base):
    """固定工作簿中一条显式写操作的逻辑幂等账本。

    文件级 SHA-256 只能识别完全相同的 ZIP；Excel/openpyxl 重新保存会改变 ZIP 字节。
    ``export_id + sheet_code + client_row_id`` 才是跨重新保存稳定的业务操作键，
    ``payload_hash`` 则阻止复用同一键提交不同内容。
    """

    __tablename__ = "maintenance_roundtrip_operation"

    id: Mapped[int] = mapped_column(primary_key=True)
    export_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sheet_code: Mapped[str] = mapped_column(String(32), nullable=False)
    client_row_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(8), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    import_batch_id: Mapped[int] = mapped_column(
        ForeignKey("sys_import_batch.id"),
        nullable=False,
    )
    applied_by: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'VOID')",
            name="ck_maintenance_roundtrip_operation_kind",
        ),
        CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maintenance_roundtrip_operation_payload_hash",
        ),
        UniqueConstraint(
            "export_id",
            "sheet_code",
            "client_row_id",
            name="uq_maintenance_roundtrip_operation_key",
        ),
        Index(
            "ix_maintenance_roundtrip_operation_batch",
            "import_batch_id",
            "id",
        ),
    )
