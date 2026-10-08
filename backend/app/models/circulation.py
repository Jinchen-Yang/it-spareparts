"""备件循环台账（板块 D 第一批，D-1 回收清单导入 / D-4 SN 台账）。

已确认口径（2026-10-07 甲方微信留底，决策 D-18~D-24）：
- 回收清单是「待检测队列的来源单据」（PN 级批量），检测环节才是 SN 采集点
  与入库权威事实（D-23）；本批表只记回收与检测前的事实；
- SN 挂 PN 号下，一个 SN 对应一个物品（入库级唯一约束保证）；
- 送修环节系统不管：「送修中」仅为状态标记，不做流程不记成本；
- 本模块与 #209 仓库单据管道互不写对方事实，也不写成本/库存主账。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, TZDateTime

# 回收清单「备件状态」列的受控枚举（甲方清单原词）
CONDITIONS = ("好件", "坏件")
# SN 台账生命周期（第一批最小集；检测/出库流转随 D-2 检测单切片扩展）
SN_LIFECYCLE = ("pending_detection", "in_stock", "retired")


class RecycleBatch(Base):
    """一次回收清单导入批次（文件级，SHA-256 去重的锚点）。"""

    __tablename__ = "recycle_batch"

    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 内容级幂等锚点：同文件重传不再新建（F 清单已确认的文件级 SHA-256 事实基础）
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 业务批次信息（如「第十批」/ 305306），维护规则待 J9 残余确认，先原样记录
    batch_label: Mapped[str | None] = mapped_column(String(64))
    source_doc_no: Mapped[str | None] = mapped_column(String(64))
    imported_by: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)   # 原始有效行
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)  # 聚合后行
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)  # type: ignore[name-defined]


class RecycleLine(Base):
    """回收明细（聚合行）：同 PN×库区×货位×状态 的数量合并。

    未收录 PN（dim_part 匹配不上）不臆造主档：part_id 置空 + needs_review 标记。
    """

    __tablename__ = "recycle_line"

    line_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("recycle_batch.batch_id"), nullable=False, index=True
    )
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_part.id"), nullable=True, index=True
    )
    pn_raw: Mapped[str] = mapped_column(String(128), nullable=False)  # 清单原样（strip+upper）
    description: Mapped[str | None] = mapped_column(Text)
    category_label: Mapped[str | None] = mapped_column(String(64))  # 中文品名（分类建议，非身份键）
    warehouse_code: Mapped[str] = mapped_column(String(32), nullable=False)
    bin_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    condition: Mapped[str] = mapped_column(String(8), nullable=False)  # 好件/坏件
    qty: Mapped[Decimal] = mapped_column(Qty, nullable=False)  # type: ignore[name-defined]
    # 聚合行单价 = Σ总价/Σ数量（加权均值，四舍五入到分）；总价一致性警告行保留原值并打标
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    company_entity: Mapped[str | None] = mapped_column(String(128))
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    price_mismatch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (
        CheckConstraint("condition IN ('好件', '坏件')", name="ck_recycle_line_condition"),
        CheckConstraint("qty > 0", name="ck_recycle_line_qty_positive"),
        CheckConstraint("unit_price >= 0", name="ck_recycle_line_price_nonneg"),
        UniqueConstraint(
            "batch_id", "pn_raw", "warehouse_code", "bin_code", "condition",
            name="uq_recycle_line_aggregate",
        ),
    )


class CirculationSnItem(Base):
    """SN 台账：PN → SN 一对多，一个 SN 对应一个物品（甲方确认口径，D-23）。

    SN 在检测环节录入（采集点）；第一批只建账与唯一约束，状态流转随
    检测单切片（D-2）扩展。
    """

    __tablename__ = "circulation_sn_item"

    sn: Mapped[str] = mapped_column(String(128), primary_key=True)
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_part.id"), nullable=True, index=True
    )
    pn_std: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending_detection", server_default="pending_detection"
    )
    source_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("recycle_batch.batch_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('pending_detection', 'in_stock', 'retired')",
            name="ck_sn_lifecycle",
        ),
    )
