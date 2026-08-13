"""Tritium project import: batch tracking and source-to-system identity links.

Each import batch records one uploaded tritium export file.  Source links
bind a tritium XSDD (sales order number) to an IT_data maintenance_project
so that re-imports of the same XSDD update the same project identity.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, TZDateTime


class MaintenanceProjectImportBatch(Base):
    """One uploaded tritium export file and its processing status."""

    __tablename__ = "maintenance_project_import_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="preview", server_default="preview"
    )
    preview_json: Mapped[dict | None] = mapped_column(JSONB)
    applied_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('preview', 'applied', 'error')",
            name="ck_project_import_batch_status",
        ),
        CheckConstraint(
            "char_length(btrim(filename)) > 0",
            name="ck_project_import_batch_filename",
        ),
        Index("ix_project_import_batch_hash", "file_hash"),
        Index("ix_project_import_batch_status", "status", "created_at"),
    )


class MaintenanceProjectSourceLink(Base):
    """Stable binding: tritium XSDD → IT_data maintenance_project."""

    __tablename__ = "maintenance_project_source_link"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"),
        nullable=False,
    )
    first_batch_id: Mapped[int] = mapped_column(
        ForeignKey("maintenance_project_import_batch.id"),
        nullable=False,
    )
    latest_batch_id: Mapped[int] = mapped_column(
        ForeignKey("maintenance_project_import_batch.id"),
        nullable=False,
    )
    source_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(source_id)) > 0",
            name="ck_project_source_link_source_id",
        ),
        UniqueConstraint("project_id", name="uq_project_source_link_project"),
        Index("ix_project_source_link_project", "project_id"),
    )
