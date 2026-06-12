"""PN 主数据治理表（整改 P1，审核说明 §4）。

设计取舍（评审定稿）：
- brands 唯一键建在 normalized_name（归一后写法漂移收敛于此），brand_name 仅展示。
- product_categories 扁平两级（实测 33 大类 + 154 小类，邻接树是过度设计）；
  唯一约束用 PG15 NULLS NOT DISTINCT 防止 minor 为空的大类重复。
- product_specs 不配 spec_key 字典表：键定义与抽取正则必须同步演进，
  作为常量放 services/spec_extract.py；numeric_value 支撑容量/频率范围查询。
- 候选/问题表的幂等锚点是部分唯一索引（WHERE status='pending'/'open'），
  回填重跑用 on_conflict 防重，已审核行绝不被刷回。
- 质量问题以 (entity_type, entity_id) 定位：part 级问题 entity_id=dim_part.id，
  alias 级 entity_id=part_alias.id —— unreviewed_alias 不是 part 级问题。
- merge_logs 存全量前镜像与受影响行 id 数组：合并是本系统最危险操作，
  必须可定位影响范围、可按日志 LIFO 人工回滚（不提供任意历史自动回滚）。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class Brand(Base):
    """品牌字典：收敛「华为（HUAWEI）/华为（HUAWEI）2」类写法漂移。"""

    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_name: Mapped[str] = mapped_column(String(128))                 # 展示名（首个来源写法）
    normalized_name: Mapped[str] = mapped_column(String(128), unique=True)  # 归一键
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class ProductCategory(Base):
    """品类字典：扁平两级（大类+可空小类）。"""

    __tablename__ = "product_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_major: Mapped[str] = mapped_column(String(64))
    category_minor: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("category_major", "category_minor",
                         name="uq_category_major_minor",
                         postgresql_nulls_not_distinct=True),
    )


class ProductSpec(Base):
    """结构化规格（EAV）。spec_key 合法值由 services/spec_extract.SPEC_KEYS 约束。"""

    __tablename__ = "product_specs"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    spec_key: Mapped[str] = mapped_column(String(32))      # capacity/interface/rpm/form_factor/...
    spec_value: Mapped[str] = mapped_column(String(128))   # 展示值，如 "1.2TB"/"SAS"
    spec_unit: Mapped[str | None] = mapped_column(String(16))
    # 数值化规范值（容量统一 GB、频率 MHz、转速 RPM），支撑范围/精确条件查询
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    source: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto")  # auto/manual
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("part_id", "spec_key", name="uq_spec_part_key"),
        Index("ix_spec_key_value", "spec_key", "spec_value"),
        Index("ix_spec_key_numeric", "spec_key", "numeric_value"),
    )


class ProductMatchCandidate(Base):
    """疑似同款候选：resolver/重复组产出，等待人工审核。绝不自动合并。"""

    __tablename__ = "product_match_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_text: Mapped[str] = mapped_column(Text)                     # 源写法（pn_std 或外部文本）
    normalized_text: Mapped[str | None] = mapped_column(Text)
    source_part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))   # 疑似重复的源型号
    candidate_part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))  # 建议归并目标
    match_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    match_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    review_action: Mapped[str | None] = mapped_column(String(32))   # merge/alias/reject/independent
    reviewed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','accepted','rejected','obsolete')", name="ck_pmc_status"
        ),
        # 幂等锚点：同一 (源,目标) 仅一条待审；已审历史可留多条
        Index("ux_pmc_pending", "source_part_id", "candidate_part_id",
              unique=True, postgresql_where=text("status = 'pending'")),
        Index("ix_pmc_status", "status"),
        Index("ix_pmc_source", "source_part_id"),
        Index("ix_pmc_candidate", "candidate_part_id"),
    )


class ProductMergeLog(Base):
    """商品合并日志：前镜像 + 受影响行 id，支持人工 LIFO 回滚与审计。"""

    __tablename__ = "product_merge_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    target_part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"))
    merge_reason: Mapped[str | None] = mapped_column(Text)
    source_snapshot: Mapped[dict | None] = mapped_column(JSONB)   # 源 dim_part 行全镜像
    merged_fields: Mapped[dict | None] = mapped_column(JSONB)     # 目标被补写字段 before/after
    affected_records: Mapped[dict | None] = mapped_column(JSONB)  # 各表受影响行 id 数组
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_merge_source", "source_part_id"),
        Index("ix_merge_target", "target_part_id"),
    )


class ProductDataQualityIssue(Base):
    """主数据质量问题队列：幂等扫描产出，可关闭留痕（含"确认不是问题"）。"""

    __tablename__ = "product_data_quality_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_type: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(16), default="part", server_default="part")
    entity_id: Mapped[int] = mapped_column(BigInteger)
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))  # 冗余定位列（part 级问题）
    issue_level: Mapped[str] = mapped_column(String(16), default="warning", server_default="warning")
    issue_description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open")
    detected_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    resolution_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('open','resolved','dismissed')", name="ck_dqi_status"),
        # 幂等锚点：同一实体同类问题仅一条 open；resolved/dismissed 历史可多条
        Index("ux_dqi_open", "issue_type", "entity_type", "entity_id",
              unique=True, postgresql_where=text("status = 'open'")),
        Index("ix_dqi_status_type", "status", "issue_type"),
        Index("ix_dqi_part", "part_id"),
    )
