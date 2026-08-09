"""Manual assignment history from source maintenance orders to stable projects."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class MaintenanceSourceOrderAssignment(Base):
    """One immutable relationship generation; inactive rows remain as history."""

    __tablename__ = "maintenance_source_order_assignment"

    assignment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_order_id: Mapped[str] = mapped_column(
        ForeignKey("f_maintenance_order.raw_order_id"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
    )
    archived_by: Mapped[str | None] = mapped_column(String(64))
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint(
            "version >= 1",
            name="ck_maintenance_source_assignment_version",
        ),
        CheckConstraint(
            "char_length(btrim(created_by)) > 0",
            name="ck_maintenance_source_assignment_creator",
        ),
        CheckConstraint(
            "(is_active AND archived_at IS NULL AND archived_by IS NULL) OR "
            "(NOT is_active AND archived_at IS NOT NULL "
            "AND char_length(btrim(archived_by)) > 0 "
            "AND archived_at >= created_at)",
            name="ck_maintenance_source_assignment_archive_state",
        ),
        Index(
            "ux_maintenance_source_assignment_active_order",
            "source_order_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index(
            "ix_maintenance_source_assignment_project_active",
            "project_id",
            "is_active",
        ),
        Index(
            "ix_maintenance_source_assignment_order_created",
            "source_order_id",
            "created_at",
            "assignment_id",
        ),
    )
