"""D-36 catalog-v2 捕获、固定发布投影与可恢复传输。

捕获表不引用业务行：PN 被物理删除时仍须保留停批证据。
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Index, LargeBinary, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


def _uuid() -> str:
    return str(uuid4())


class CatalogV2Change(Base):
    __tablename__ = "catalog_v2_change"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    txid: Mapped[int] = mapped_column(BigInteger)
    table_name: Mapped[str] = mapped_column(String(32))
    op: Mapped[str] = mapped_column(String(1))
    part_id: Mapped[int | None] = mapped_column(BigInteger)
    old_part_id: Mapped[int | None] = mapped_column(BigInteger)
    new_part_id: Mapped[int | None] = mapped_column(BigInteger)
    alias_id: Mapped[int | None] = mapped_column(BigInteger)
    old_row: Mapped[dict | None] = mapped_column(JSONB)
    new_row: Mapped[dict | None] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    published_epoch: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    published_sequence: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint("op IN ('I', 'U', 'D')", name="ck_catalog_v2_change_op"),
        Index("ix_catalog_v2_change_pending_txid", "txid", postgresql_where=text("published_epoch IS NULL")),
        Index("ix_catalog_v2_change_part", "part_id", "txid"),
    )


class CatalogV2Epoch(Base):
    __tablename__ = "catalog_v2_epoch"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String(16), default="seeding", server_default="seeding")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    watermark: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    retained_from_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    ready_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint("status IN ('seeding', 'ready', 'stopped')", name="ck_catalog_v2_epoch_status"),
        Index("uq_catalog_v2_epoch_active", "active", unique=True, postgresql_where=text("active")),
    )


class CatalogV2Projection(Base):
    __tablename__ = "catalog_v2_projection"

    epoch_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    part_id: Mapped[int] = mapped_column(BigInteger)
    data: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    published_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("kind IN ('node', 'alias')", name="ck_catalog_v2_projection_kind"),
        Index("ix_catalog_v2_projection_part", "epoch_id", "part_id"),
    )


class CatalogV2Batch(Base):
    __tablename__ = "catalog_v2_batch"

    epoch_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    published_sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    previous_sequence: Mapped[int] = mapped_column(BigInteger)
    batch_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True, default=_uuid)
    transaction_groups: Mapped[list] = mapped_column(JSONB)
    items: Mapped[list] = mapped_column(JSONB)
    alias_moves: Mapped[list] = mapped_column(JSONB)
    batch_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class CatalogV2Snapshot(Base):
    __tablename__ = "catalog_v2_snapshot"

    snapshot_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    request_hash: Mapped[str] = mapped_column(String(64))
    epoch_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    watermark: Mapped[int] = mapped_column(BigInteger)
    node_count: Mapped[int] = mapped_column(BigInteger)
    alias_count: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(String(64))
    items: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)

    __table_args__ = (UniqueConstraint("client_id", "request_id", name="uq_catalog_v2_snapshot_request"),)


class CatalogV2PayloadFragment(Base):
    __tablename__ = "catalog_v2_payload_fragment"

    epoch_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    payload_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    part_no: Mapped[int] = mapped_column(primary_key=True)
    part_count: Mapped[int] = mapped_column()
    data: Mapped[bytes] = mapped_column(LargeBinary)
    part_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    context: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class CatalogV2Cursor(Base):
    __tablename__ = "catalog_v2_cursor"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128))
    context: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class CatalogV2Alarm(Base):
    __tablename__ = "catalog_v2_alarm"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    epoch_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    code: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
