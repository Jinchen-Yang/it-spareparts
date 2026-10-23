"""Media metadata only. Original objects are immutable and never deleted by commands."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

TZDateTime = DateTime(timezone=True)


class Asset(Base):
    __tablename__ = "pn_media_asset"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    etag: Mapped[str | None] = mapped_column(String(256))
    verification_status: Mapped[str] = mapped_column(
        String(24), default="cloud_verified", server_default="cloud_verified"
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(128))
    width: Mapped[int] = mapped_column()
    height: Mapped[int] = mapped_column()
    owner_principal_id: Mapped[str] = mapped_column(String(256))
    source_namespace: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "source_namespace", "source_id", name="uq_pn_media_asset_source"
        ),
        CheckConstraint(
            "verification_status IN ('legacy_metadata','cloud_verified')",
            name="ck_pn_media_asset_verification",
        ),
        CheckConstraint(
            "verification_status <> 'cloud_verified' OR etag IS NOT NULL",
            name="ck_pn_media_asset_verified_etag",
        ),
        CheckConstraint(
            "size_bytes > 0 AND width > 0 AND height > 0",
            name="ck_pn_media_asset_dimensions",
        ),
    )


class GalleryGroup(Base):
    __tablename__ = "pn_media_gallery_group"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    canonical_part_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_part.id"), unique=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    primary_entry_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    ordered_entry_ids: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class GalleryMember(Base):
    __tablename__ = "pn_media_gallery_member"
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), primary_key=True)
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("pn_media_gallery_group.id"), index=True
    )


class GalleryEntry(Base):
    __tablename__ = "pn_media_entry"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("pn_media_asset.id"), index=True)
    origin_part_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_part.id"), index=True
    )
    origin_namespace: Mapped[str] = mapped_column(String(64))
    origin_id: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(
        String(16), default="common", server_default="common"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="gallery", server_default="gallery"
    )
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    generation: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    source: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (
        Index(
            "ix_pn_media_entry_part_hash_purpose",
            "origin_part_id",
            "content_hash",
            "purpose",
        ),
        UniqueConstraint(
            "origin_namespace",
            "origin_id",
            "content_hash",
            "purpose",
            name="uq_pn_media_entry_origin_hash_purpose",
        ),
        CheckConstraint(
            "purpose IN ('common','defect')", name="ck_pn_media_entry_purpose"
        ),
        CheckConstraint(
            "status IN ('gallery','candidate','processing','deleted')",
            name="ck_pn_media_entry_status",
        ),
        CheckConstraint(
            "revision > 0 AND generation > 0", name="ck_pn_media_entry_revision"
        ),
    )


class GallerySuppression(Base):
    __tablename__ = "pn_media_suppression"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    group_id: Mapped[UUID] = mapped_column(ForeignKey("pn_media_gallery_group.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    reason: Mapped[str] = mapped_column(String(2000))
    __table_args__ = (
        UniqueConstraint(
            "group_id", "content_hash", name="uq_pn_media_suppression_group_hash"
        ),
    )


class Derivative(Base):
    __tablename__ = "pn_media_derivative"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("pn_media_asset.id"))
    recipe_id: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("asset_id", "recipe_id", name="uq_pn_media_derivative_recipe"),
        CheckConstraint(
            "status IN ('pending','ready','failed')",
            name="ck_pn_media_derivative_status",
        ),
    )


class UploadTicket(Base):
    __tablename__ = "pn_media_upload"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_principal_id: Mapped[str] = mapped_column(String(256))
    client_id: Mapped[str] = mapped_column(String(128))
    client_request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    identity_revision: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    purpose: Mapped[str] = mapped_column(
        String(16), default="common", server_default="common"
    )
    filename: Mapped[str] = mapped_column(String(512))
    declared_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(128))
    staging_key: Mapped[str] = mapped_column(String(1024), unique=True)
    status: Mapped[str] = mapped_column(
        String(24), default="issued", server_default="issued"
    )
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    generation: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("pn_media_asset.id"))
    entry_id: Mapped[UUID | None] = mapped_column(ForeignKey("pn_media_entry.id"))
    job_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "owner_principal_id",
            "client_id",
            "client_request_id",
            name="uq_pn_media_upload_client_request",
        ),
        CheckConstraint(
            "status IN ('issued','verifying','ready','committed','cancelled','failed')",
            name="ck_pn_media_upload_status",
        ),
        CheckConstraint(
            "declared_bytes > 0 AND declared_bytes <= 20971520",
            name="ck_pn_media_upload_bytes",
        ),
    )


class MultipartUpload(Base):
    __tablename__ = "pn_media_multipart"
    upload_id: Mapped[UUID] = mapped_column(
        ForeignKey("pn_media_upload.id"), primary_key=True
    )
    storage_upload_id: Mapped[str | None] = mapped_column(String(1024))
    state: Mapped[str] = mapped_column(
        String(24), default="initializing", server_default="initializing"
    )
    part_size: Mapped[int] = mapped_column(default=1048576, server_default="1048576")
    parts: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    operation_token: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid4)
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    aborted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    __table_args__ = (
        CheckConstraint(
            "state IN ('initializing','uploading','completing','completed','cancelled')",
            name="ck_pn_media_multipart_state",
        ),
    )


class InboxGroup(Base):
    __tablename__ = "pn_media_inbox_group"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_principal_id: Mapped[str] = mapped_column(String(256))
    client_id: Mapped[str] = mapped_column(String(128))
    client_group_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    capture_mode: Mapped[str] = mapped_column(String(24))
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    __table_args__ = (
        UniqueConstraint(
            "owner_principal_id",
            "client_id",
            "client_group_id",
            name="uq_pn_media_inbox_group_client",
        ),
    )


class InboxItem(Base):
    __tablename__ = "pn_media_inbox"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    upload_id: Mapped[UUID] = mapped_column(
        ForeignKey("pn_media_upload.id"), unique=True
    )
    asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("pn_media_asset.id"))
    group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("pn_media_inbox_group.id"), index=True
    )
    position: Mapped[int] = mapped_column(default=0, server_default="0")
    client_order: Mapped[int | None] = mapped_column()
    owner_principal_id: Mapped[str] = mapped_column(String(256), index=True)
    status: Mapped[str] = mapped_column(
        String(24), default="receiving", server_default="receiving"
    )
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    generation: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    claim_revision: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    claimed_by: Mapped[str | None] = mapped_column(String(256))
    claim_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    assignment_key: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), unique=True
    )
    part_id: Mapped[int | None] = mapped_column(ForeignKey("dim_part.id"))
    entry_id: Mapped[UUID | None] = mapped_column(ForeignKey("pn_media_entry.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    __table_args__ = (
        UniqueConstraint("group_id", "client_order", name="uq_pn_media_inbox_capture_order"),
        CheckConstraint("client_order IS NULL OR (group_id IS NOT NULL AND client_order BETWEEN 0 AND 1999)",
                        name="ck_pn_media_inbox_capture_order"),
        CheckConstraint(
            "status IN ('receiving','ready','assigned','failed','deleted')",
            name="ck_pn_media_inbox_status",
        ),
        Index("ix_pn_media_inbox_discovery", "status", "id"),
    )
