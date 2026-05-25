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
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
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
    __tablename__ = "part_substitute"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id_a: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    part_id_b: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint("part_id_a < part_id_b", name="ck_substitute_order"),
        UniqueConstraint("part_id_a", "part_id_b", name="uq_substitute_pair"),
    )
