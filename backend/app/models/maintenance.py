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
    # ---- WBDD 展示补全列（plan v1.3 §3.1，34 列全 nullable）----
    # 头级自报汇总四列：仅展示/与三源事实无判定并排（M4-4）；禁止进入任何聚合计算（铁律 3）
    head_demand_qty: Mapped[Decimal | None] = mapped_column(Qty)          # 需求数量（头段）
    head_purchase_qty: Mapped[Decimal | None] = mapped_column(Qty)        # 需采数量（头段尾部）
    head_shipped_qty: Mapped[Decimal | None] = mapped_column(Qty)         # 已发货数量（自报）
    head_returned_qty: Mapped[Decimal | None] = mapped_column(Qty)        # 已返货数量（自报）
    maintainer_raw: Mapped[str | None] = mapped_column(String(64))        # 维保负责人
    work_order_no: Mapped[str | None] = mapped_column(String(64))         # 维保工单
    created_by_raw: Mapped[str | None] = mapped_column(String(64))        # 制单人员
    purchaser_raw: Mapped[str | None] = mapped_column(String(64))         # 采购员
    purchaser2_raw: Mapped[str | None] = mapped_column(String(64))        # 采购人员（并存第二列）
    project_manager_raw: Mapped[str | None] = mapped_column(String(64))   # 项目经理
    project_manager_staff_raw: Mapped[str | None] = mapped_column(String(64))  # 项目经理人员
    co_salesperson_raw: Mapped[str | None] = mapped_column(String(64))    # 协同销售人员
    partner_raw: Mapped[str | None] = mapped_column(String(64))           # 合作伙伴人
    sales_dept_raw: Mapped[str | None] = mapped_column(String(64))        # 销售部门
    warehouse_keeper_raw: Mapped[str | None] = mapped_column(String(64))  # 仓管员
    storage_center: Mapped[str | None] = mapped_column(String(64))        # 仓储中心
    warehouse_raw: Mapped[str | None] = mapped_column(String(64))         # 仓库（与 warehouse=出库仓库 并存）
    change_warehouse_flag: Mapped[bool | None] = mapped_column(Boolean)   # 是否变仓库（是/否；其他→NULL+issue）
    change_warehouse: Mapped[str | None] = mapped_column(String(64))      # 变更仓库
    change_warehouse_handler: Mapped[str | None] = mapped_column(String(64))  # 变更仓承办人
    warehouse_handler: Mapped[str | None] = mapped_column(String(64))     # 仓库承办人
    supply_deadline: Mapped[date | None] = mapped_column(Date)            # 供货期限（归一化失败→NULL+issue）
    delivery_address_option: Mapped[str | None] = mapped_column(String(128))  # 选择收货地址
    receiver: Mapped[str | None] = mapped_column(String(64))              # 收货人（customer_info 组脱敏）
    receiver_phone: Mapped[str | None] = mapped_column(String(32))        # 收货人电话（同上）
    receiver_address: Mapped[str | None] = mapped_column(Text)            # 收货地址（同上）
    express_no: Mapped[str | None] = mapped_column(String(128))           # 快递单号
    express_no2: Mapped[str | None] = mapped_column(String(128))          # 快递单号#
    image_urls: Mapped[str | None] = mapped_column(Text)                  # 图片（原样 URL 文本）
    attachments: Mapped[str | None] = mapped_column(Text)                 # 附件（原样 URL 文本）
    whole_machine_check: Mapped[str | None] = mapped_column(String(16))   # 整机需采备件校验（原样）
    accept_generic_flag: Mapped[bool | None] = mapped_column(Boolean)     # 是否可以接受通用号（91 列布局独有）
    created_at_raw: Mapped[str | None] = mapped_column(String(32))        # 创建时间（原样文本，仅展示）
    modified_at_raw: Mapped[str | None] = mapped_column(String(32))       # 修改时间（原样文本，仅展示）
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
    # ---- WBDD 明细展示补全列（plan v1.3 §3.2，28 列全 nullable）----
    # 1–14 为「流转状态列」：只原样展示、不参与任何计算、不标注可信度（铁律 3，
    # 看板聚合服务以 AGGREGATE_SOURCE_COLUMNS 白名单锁死）。
    purchase_qty: Mapped[Decimal | None] = mapped_column(Qty)             # 需采数量
    change_warehouse_purchase_qty: Mapped[Decimal | None] = mapped_column(Qty)  # 变更仓需采数量
    purchased_qty: Mapped[Decimal | None] = mapped_column(Qty)            # 已采数量
    pending_purchase_qty: Mapped[Decimal | None] = mapped_column(Qty)     # 待采数量
    direct_ship_qty: Mapped[Decimal | None] = mapped_column(Qty)          # 直采直发数
    warehouse_need_qty: Mapped[Decimal | None] = mapped_column(Qty)       # 库房需发数
    warehouse_shipped_qty: Mapped[Decimal | None] = mapped_column(Qty)    # 库房发货数
    supplied_qty: Mapped[Decimal | None] = mapped_column(Qty)             # 已供数量
    pending_supply_qty: Mapped[Decimal | None] = mapped_column(Qty)       # 待供数量
    returned_qty: Mapped[Decimal | None] = mapped_column(Qty)             # 已返数量
    pending_return_qty: Mapped[Decimal | None] = mapped_column(Qty)       # 待返数量
    consumed_qty: Mapped[Decimal | None] = mapped_column(Qty)             # 领用数量（29,140 行仅 18 行非空）
    demand_pending_return_qty: Mapped[Decimal | None] = mapped_column(Qty)  # 需求待返数
    return_old_part: Mapped[str | None] = mapped_column(String(16))       # 退返旧件
    # 15–28 为展示/排查列
    whole_or_part: Mapped[str | None] = mapped_column(String(8))          # 整机/备件（斜杠单列）
    whole_machine_purchase_part: Mapped[str | None] = mapped_column(Text)  # 整机需采备件
    whole_machine_part_purchased: Mapped[str | None] = mapped_column(String(16))  # 整机备件已采
    purchase_note: Mapped[str | None] = mapped_column(Text)               # 需采备件说明
    line_note: Mapped[str | None] = mapped_column(Text)                   # 备注
    line_image_urls: Mapped[str | None] = mapped_column(Text)             # 图片/附件（斜杠单列）
    warehouse_stock_raw: Mapped[str | None] = mapped_column(Text)         # 各仓库存（原样多仓文本）
    adjust_warehouse_flag: Mapped[bool | None] = mapped_column(Boolean)   # 个别调整发货仓（是/否）
    adjust_warehouse: Mapped[str | None] = mapped_column(String(64))      # 调整仓库
    adjust_storage_center: Mapped[str | None] = mapped_column(String(64))  # 调整仓储中心
    adjust_keeper: Mapped[str | None] = mapped_column(String(64))         # 调整库管员
    ship_warehouse: Mapped[str | None] = mapped_column(String(64))        # 发货仓库
    ship_warehouse_object_id: Mapped[str | None] = mapped_column(String(64))  # 发货仓ObjectID
    ship_stock: Mapped[Decimal | None] = mapped_column(Qty)               # 发货库存（数值化失败→NULL+issue）
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))

    __table_args__ = (
        Index("ix_ml_order", "order_id"),
        Index("ix_ml_part", "part_id"),
    )


class MaintenanceDemandDeleteIntent(Base):
    """一次 WBDD 整单逻辑删除的不可变复核快照与状态机。"""

    __tablename__ = "maintenance_demand_delete_intent"

    intent_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    selection_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="reviewed")
    reason: Mapped[str] = mapped_column(Text)
    operated_by: Mapped[str] = mapped_column(String(64))
    header_count: Mapped[int] = mapped_column(Integer)
    line_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    not_before: Mapped[datetime | None] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    executed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    terminal_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    result_json: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "status IN ('reviewed', 'armed_wait', 'executed', "
            "'cancelled', 'conflicted', 'expired')",
            name="ck_maintenance_demand_delete_intent_status",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maintenance_demand_delete_intent_reason",
        ),
        CheckConstraint(
            "header_count BETWEEN 1 AND 1000",
            name="ck_maintenance_demand_delete_intent_headers",
        ),
        CheckConstraint(
            "line_count BETWEEN 0 AND 20000",
            name="ck_maintenance_demand_delete_intent_lines",
        ),
        Index("ix_maintenance_demand_delete_intent_status_expiry", "status", "expires_at"),
    )


class MaintenanceDemandDeleteIntentItem(Base):
    """意图中的逐单版本与完整复核行；创建后不得改写。"""

    __tablename__ = "maintenance_demand_delete_intent_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_demand_delete_intent.intent_id")
    )
    source_order_id: Mapped[str] = mapped_column(
        ForeignKey("f_maintenance_order.raw_order_id")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    version_digest: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[dict] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint(
            "intent_id", "source_order_id",
            name="uq_maintenance_demand_delete_intent_item_source",
        ),
        UniqueConstraint(
            "intent_id", "ordinal",
            name="uq_maintenance_demand_delete_intent_item_ordinal",
        ),
        CheckConstraint(
            "ordinal >= 0",
            name="ck_maintenance_demand_delete_intent_item_ordinal",
        ),
        Index("ix_maintenance_demand_delete_intent_item_source", "source_order_id"),
    )


class MaintenanceDemandTombstone(Base):
    """WBDD 逻辑墓碑；同 raw_order_id 重导时仍保持删除，直至显式恢复。"""

    __tablename__ = "maintenance_demand_tombstone"

    source_order_id: Mapped[str] = mapped_column(
        ForeignKey("f_maintenance_order.raw_order_id"), primary_key=True
    )
    delete_intent_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_demand_delete_intent.intent_id")
    )
    version_digest: Mapped[str] = mapped_column(String(64))
    deleted_by: Mapped[str] = mapped_column(String(64))
    delete_reason: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime] = mapped_column(TZDateTime)
    restored_by: Mapped[str | None] = mapped_column(String(64))
    restore_reason: Mapped[str | None] = mapped_column(Text)
    restored_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(delete_reason)) > 0",
            name="ck_maintenance_demand_tombstone_delete_reason",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_maintenance_demand_tombstone_version",
        ),
        Index("ix_maintenance_demand_tombstone_active", "source_order_id", "restored_at"),
    )


class MaintenanceDemandDeleteEvent(Base):
    """WBDD 删除/恢复业务审计；数据库触发器保证 append-only。"""

    __tablename__ = "maintenance_demand_delete_event"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_demand_delete_intent.intent_id")
    )
    source_order_id: Mapped[str | None] = mapped_column(
        ForeignKey("f_maintenance_order.raw_order_id")
    )
    event_type: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    operated_by: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('armed', 'executed', 'cancelled', "
            "'conflicted', 'expired', 'restored')",
            name="ck_maintenance_demand_delete_event_type",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maintenance_demand_delete_event_reason",
        ),
        Index("ix_maintenance_demand_delete_event_intent_time", "intent_id", "occurred_at"),
        Index("ix_maintenance_demand_delete_event_source_time", "source_order_id", "occurred_at"),
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
