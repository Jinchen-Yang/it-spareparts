"""整机的备件目录。库存始终由 Shop 提供。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class MachineModel(Base):
    __tablename__ = "machine_model"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(192))


class MachineModelName(Base):
    """规范名与别名占用同一个品牌内的名字空间。"""

    __tablename__ = "machine_model_name"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("machine_model.id", ondelete="CASCADE"), index=True)
    brand_key: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(192))
    is_primary: Mapped[bool]
    __table_args__ = (UniqueConstraint("brand_key", "name_key", name="uq_machine_model_name"),)


class MachineModelMember(Base):
    __tablename__ = "machine_model_member"
    model_id: Mapped[int] = mapped_column(ForeignKey("machine_model.id"), primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), primary_key=True)
    position: Mapped[str] = mapped_column(Text)
    condition: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    raw_pns: Mapped[list] = mapped_column(JSONB)
    __table_args__ = (Index("ix_machine_model_member_part", "part_id"),)


class MachineModelPending(Base):
    __tablename__ = "machine_model_pending"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("machine_model.id"))
    raw_key: Mapped[str] = mapped_column(Text)
    raw_pn: Mapped[str] = mapped_column(String(256))
    position: Mapped[str] = mapped_column(Text)
    condition: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(16))
    candidate_ids: Mapped[list] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("model_id", "raw_key", name="uq_machine_model_pending_raw"),
        CheckConstraint("reason in ('unknown','ambiguous')", name="ck_machine_model_pending_reason"),
    )


class MachineModelCommand(Base):
    """已提交命令及其回执同时构成修订记录；变更前的被移出/核定行也留档。"""

    __tablename__ = "machine_model_command"
    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(16))
    command: Mapped[dict] = mapped_column(JSONB)
    before: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
