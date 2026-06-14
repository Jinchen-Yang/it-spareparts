"""库存（source/manual 拆分）、出入流水与替代料关系（§5/§7.6）。"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
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


class InventoryMovement(Base):
    """库存出入流水（永续库存的事实层，§7.6）。

    一行一笔库存动作，来自源系统「备件库存流水 / 整机库存流水」导出。在库数量
    由本表按 (part_id, warehouse) 时间回放求得，不再依赖快照覆盖；inventory.source_qty
    退化为「期初锚点 + 对账基准」。商品身份解析与订单/快照同口径（别名/合并重定向，见 loader）。

    字段语义：
    - direction：+1 入库 / -1 出库 / 0 不影响在库（直发、退返通知等仅留痕）。
    - qty：单据数量（非负幅度）；is_absolute=True（盘点）时为「盘点后绝对数量」。
    - is_absolute：盘点类单据——回放时把结存重置为 qty，而非按 direction 累加。
    - doc_type：标准化单据类型（见 etl.mapping.MOVEMENT_DOC_TYPES）；doc_type_raw 留原文。
    - ledger_kind：part（备件库）/ machine（整机库），两套流水入同一张表按此区分。
    - snapshot_balance：源系统导出的「结存」列（有则存），供对账核对回放结果。
    """

    __tablename__ = "inventory_movement"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_movement_id: Mapped[str] = mapped_column(String(64), unique=True)   # 源流水ID，幂等键
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    pn_std: Mapped[str] = mapped_column(String(128))     # 源 pn 归一痕迹；聚合一律走 part_id
    pn_raw: Mapped[str | None] = mapped_column(String(256))
    warehouse: Mapped[str] = mapped_column(String(64))
    movement_date: Mapped[date | None] = mapped_column(Date)
    doc_type: Mapped[str] = mapped_column(String(24))    # receipt/issue/transfer_in/.../stocktake/direct_ship/other
    doc_type_raw: Mapped[str | None] = mapped_column(String(64))
    doc_no: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[int] = mapped_column(SmallInteger)  # +1 / -1 / 0
    qty: Mapped[Decimal] = mapped_column(Qty)
    is_absolute: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    unit_price: Mapped[Decimal | None] = mapped_column(Money)  # 入库成本（部分单据带）
    counterpart_warehouse: Mapped[str | None] = mapped_column(String(64))  # 调拨对方仓（仅记录）
    ledger_kind: Mapped[str] = mapped_column(String(8), default="part", server_default="part")
    snapshot_balance: Mapped[Decimal | None] = mapped_column(Qty)   # 源系统结存（对账用）
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("sys_import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_movement_part_wh", "part_id", "warehouse"),
        Index("ix_movement_date", "movement_date"),
        CheckConstraint("direction IN (-1, 0, 1)", name="ck_movement_direction"),
    )


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
