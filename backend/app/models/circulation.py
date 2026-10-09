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
# SN 台账生命周期（D-2 检测单切片扩展：检测好件入 in_stock，坏件入 bad_stock）
SN_LIFECYCLE = ("pending_detection", "in_stock", "bad_stock", "retired")
# 循环档案上架状态（D-22）：资料齐全自动可上架（list），不全需强制（force_listed）
LISTING_STATUS = ("pending", "listed", "force_listed", "delisted")
# 循环档案附件种类：照片 / 检测报告（可上架 = 两类各至少一份）
ARCHIVE_ATTACHMENT_KINDS = ("photo", "report")


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
    # 检测单明细关联（D-23）：SN 在检测环节采集，可回溯到具体检测记录
    detection_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("detection_item.item_id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('pending_detection', 'in_stock', 'bad_stock', 'retired')",
            name="ck_sn_lifecycle",
        ),
    )


class DetectionSheet(Base):
    """检测单（D-2）：针对一个回收批次的检测作业，检测人实名。

    检测单是循环入库的权威事实单据（D-23）：检测完成生成 SN 台账记录，
    好件入前置库可用、坏件入坏件标记；同一批次允许多张检测单（分批检测）。
    """

    __tablename__ = "detection_sheet"

    sheet_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("recycle_batch.batch_id"), nullable=False, index=True
    )
    inspector: Mapped[str] = mapped_column(String(64), nullable=False)  # 检测人（实名）
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )


class DetectionItem(Base):
    """检测明细：一条回收聚合行的检测结果。

    字段对齐甲方检测清单：PN/好坏/实收数量/SN（挂 circulation_sn_item）/
    实物PN/处理方式/检测人。实物 PN 与标称不符时以实物为准（D-23），
    标称值保留在本行实现修正留痕。
    """

    __tablename__ = "detection_item"

    item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sheet_id: Mapped[str] = mapped_column(
        ForeignKey("detection_sheet.sheet_id"), nullable=False, index=True
    )
    line_id: Mapped[str] = mapped_column(
        ForeignKey("recycle_line.line_id"), nullable=False, index=True
    )
    nominal_pn_raw: Mapped[str] = mapped_column(String(128), nullable=False)  # 来源清单标称 PN
    actual_pn_raw: Mapped[str | None] = mapped_column(String(128))            # 实物 PN（以实物为准）
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_part.id"), nullable=True, index=True
    )  # 按实物 PN（无则标称）解析；未收录为空
    received_qty: Mapped[Decimal] = mapped_column(Qty, nullable=False)  # 实收数量
    actual_condition: Mapped[str] = mapped_column(String(8), nullable=False)  # 实测好坏
    handling: Mapped[str] = mapped_column(String(32), nullable=False)   # 处理方式（J11 枚举确认前自由文本）
    sn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("actual_condition IN ('好件', '坏件')", name="ck_detection_item_condition"),
        CheckConstraint("received_qty > 0", name="ck_detection_item_qty_positive"),
    )


class CirculationArchive(Base):
    """循环档案（D-22）：挂在 PN 上，一个 PN 一份档案。

    可上架判定 = 照片 ≥1 且检测报告 ≥1（资料齐全自动满足上架条件；
    上架本身仍是显式动作）。资料不全时，持 action_recycle_force_list
    的账号可强制上架，留标记、原因与实名审计。
    """

    __tablename__ = "circulation_archive"

    archive_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pn_std: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_part.id"), nullable=True, index=True
    )
    listing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    force_listed_by: Mapped[str | None] = mapped_column(String(64))
    force_listed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    force_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "listing_status IN ('pending', 'listed', 'force_listed', 'delisted')",
            name="ck_circulation_archive_listing_status",
        ),
    )


class CirculationAttachment(Base):
    """循环档案附件：照片 / 检测报告。文件落本地目录，元数据入库。"""

    __tablename__ = "circulation_attachment"

    attachment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    archive_id: Mapped[str] = mapped_column(
        ForeignKey("circulation_archive.archive_id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # photo / report
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(256), nullable=False)  # 相对 circulation_files_dir
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('photo', 'report')", name="ck_circulation_attachment_kind"),
        CheckConstraint("size_bytes > 0", name="ck_circulation_attachment_size"),
    )
