"""库存（source/manual 拆分）与替代料关系（§5）。"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
from app.models._types import Money, Qty, TZDateTime


class Inventory(Base):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_inventory_id: Mapped[str] = mapped_column(String(64), unique=True)
    # 商品身份主键（整改 P3 起查询/聚合一律走 part_id）。
    # pn_std 是导入时归一痕迹：合并后不回写，仅展示/排查，禁止作过滤聚合键；
    # 同一 part 在同仓可有多行（不同源 pn），part 级库存口径=SUM。
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    pn_std: Mapped[str] = mapped_column(String(128))
    warehouse: Mapped[str] = mapped_column(String(64))
    source_qty: Mapped[Decimal] = mapped_column(Qty)             # 源系统真实库存，每次导入覆盖
    manual_qty: Mapped[Decimal | None] = mapped_column(Qty)      # 人工修正值（可空）
    is_qty_overridden: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )  # true 则展示 manual_qty
    safety_stock: Mapped[Decimal | None] = mapped_column(Qty)    # 人工维护（库存预警，后续期）
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(128))
    machine_or_part: Mapped[str | None] = mapped_column(String(16))
    unit: Mapped[str | None] = mapped_column(String(16))
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)     # 采购反算（§7.2）
    inventory_value: Mapped[Decimal | None] = mapped_column(Money)  # display_qty × unit_cost
    snapshot_date: Mapped[date | None] = mapped_column(Date)     # = 上传日期（§7.4）
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("sys_import_batch.id"))
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("pn_std", "warehouse", name="uq_inventory_pn_wh"),)


class PartSubstitute(Base):
    """替代料关系（整改 P1：显式方向/类型/审核状态，审核说明 §4.6）。

    行始终按 part_id_a < part_id_b 规范序存储（保留 CHECK），方向编码为相对
    规范序的枚举，一行表达三种方向，(a,b) 唯一约束语义不变：
    - both:   互替
    - a_to_b: a 的需求可用 b 满足（b 替代 a）
    - b_to_a: b 的需求可用 a 满足（a 替代 b）
    未审核(pending)的替代关系不出现在型号全景推荐里。
    """

    __tablename__ = "part_substitute"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id_a: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    part_id_b: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")
    note: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(8), default="both", server_default="both")
    substitute_type: Mapped[str | None] = mapped_column(String(32))  # original/compatible/same_spec/conditional
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    reviewed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint("part_id_a < part_id_b", name="ck_substitute_order"),
        UniqueConstraint("part_id_a", "part_id_b", name="uq_substitute_pair"),
        CheckConstraint("direction IN ('both','a_to_b','b_to_a')", name="ck_substitute_direction"),
        CheckConstraint("status IN ('pending','active','rejected')", name="ck_substitute_status"),
    )


class PartPool(Base):
    """互通 PN 池（人工池，唯一真值；互通PN池价格分析 §15.1）。

    2026-07-13 起池由人工创建和维护，替代关系变化不再自动改池（自动重算已停用，
    唯一写入路径是 services/pool_catalog）。group_id 沿用稳定序列
    part_pool_group_id_seq：单调递增、退役 ID 永不复用。
    needs_calibration/oversized 为旧自动池遗留字段，人工池不再依赖，兼容保留待后续迁移删除。
    """

    __tablename__ = "part_pool"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(128))                # 人可读池名
    description: Mapped[str | None] = mapped_column(Text)         # 功能/规格/适用场景说明
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    # manual=人工新建；legacy_generated=历史自动池（迁移回填）
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # 乐观锁
    member_count: Mapped[int] = mapped_column(Integer, default=0)  # 事务内同步的冗余计数
    needs_calibration: Mapped[bool] = mapped_column(Boolean, default=False)
    oversized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('active','archived')", name="ck_part_pool_status"),
        Index("ix_part_pool_status_updated", "status", "updated_at"),
    )


class PartPoolMember(Base):
    """互通 PN 池成员（§15.2）。

    复合主键 (group_id, part_id)：池只归档不硬删除，归档池保留成员集合，其成员可再加入
    新的有效池，因此同一 part 允许出现在多个池行里；「一个有效 PN 只能属于一个有效池」
    由 pool_catalog 写路径（advisory lock + 池行 FOR UPDATE + 有效池冲突校验）保证。
    """

    __tablename__ = "part_pool_member"

    # group_id 不再单独建索引：复合主键 (group_id, part_id) 的前导列已覆盖按池查询；
    # 旧单列索引 ix_pool_member_group 在 f2a7d9c3e6b1 里随主键改造一并删除（复审阻塞 5）
    group_id: Mapped[int] = mapped_column(
        ForeignKey("part_pool.group_id", ondelete="CASCADE"), primary_key=True
    )
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), primary_key=True)
    added_by: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_pool_member_part", "part_id"),)


class PartPoolPricePolicy(Base):
    """池价格约束历史（§15.3）：统一未税的采购最高价 / 销售最低价。

    每池仅一条 valid_to IS NULL 的当前策略（部分唯一索引 DB 级保证）；修改=关闭旧行
    并插入新行，不覆盖历史。原始录入值与含/未税口径保留可追溯（含税÷1.13 入库）。
    系统只记录展示，不做审批/拦截。
    """

    __tablename__ = "part_pool_price_policy"

    id: Mapped[int] = mapped_column(primary_key=True)
    # group_id 不单独建索引：ix_pool_policy_group_from (group_id, valid_from DESC) 的
    # 前导列已覆盖按池查询，单列索引是纯冗余（复审阻塞 5，ORM 与迁移索引对齐）
    group_id: Mapped[int] = mapped_column(
        ForeignKey("part_pool.group_id", ondelete="CASCADE")
    )
    purchase_ceiling_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    sales_floor_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    purchase_input_value: Mapped[Decimal | None] = mapped_column(Money)
    purchase_input_basis: Mapped[str | None] = mapped_column(String(8))
    sales_input_value: Mapped[Decimal | None] = mapped_column(Money)
    sales_input_basis: Mapped[str | None] = mapped_column(String(8))
    valid_from: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    valid_to: Mapped[datetime | None] = mapped_column(TZDateTime)
    changed_by: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "purchase_input_basis IS NULL OR purchase_input_basis IN ('ex_tax','inc_tax')",
            name="ck_pool_policy_purchase_basis",
        ),
        CheckConstraint(
            "sales_input_basis IS NULL OR sales_input_basis IN ('ex_tax','inc_tax')",
            name="ck_pool_policy_sales_basis",
        ),
        Index(
            "uq_pool_policy_current", "group_id",
            unique=True, postgresql_where=text("valid_to IS NULL"),
        ),
        Index("ix_pool_policy_group_from", "group_id", desc("valid_from")),
    )
