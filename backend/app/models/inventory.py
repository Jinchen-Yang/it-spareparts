"""库存（source/manual 拆分）与替代料关系（§5）。"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
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
