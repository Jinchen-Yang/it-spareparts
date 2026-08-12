"""Stable maintenance project master and project-contract relationships (#195)."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._types import Money, TZDateTime


class MaintenanceProject(Base):
    """Canonical project identity; display names are never relationship keys."""

    __tablename__ = "maintenance_project"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    project_manager_id: Mapped[str | None] = mapped_column(String(64))
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    contracts: Mapped[list["MaintenanceProjectContract"]] = relationship(
        back_populates="project",
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_maintenance_project_version"),
        Index(
            "ux_maintenance_project_code_ci",
            func.lower(project_code),
            unique=True,
        ),
        Index("ix_maintenance_project_display_name", "display_name"),
        Index("ix_maintenance_project_active_code", "is_active", "project_code"),
        Index(
            "ix_maintenance_project_code_trgm",
            project_code,
            postgresql_using="gin",
            postgresql_ops={"project_code": "gin_trgm_ops"},
        ),
        Index(
            "ix_maintenance_project_display_name_trgm",
            display_name,
            postgresql_using="gin",
            postgresql_ops={"display_name": "gin_trgm_ops"},
        ),
    )


class MaintenanceProjectContract(Base):
    """Versioned, auditable relationship between one project and one contract."""

    __tablename__ = "maintenance_project_contract"

    project_contract_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"),
        nullable=False,
    )
    contract_id: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_no: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_amount: Mapped[Decimal | None] = mapped_column(Money)
    # Raw source status is preserved.  Inclusion is never guessed from this text.
    contract_status: Mapped[str | None] = mapped_column(String(64))
    status_mapping_state: Mapped[str] = mapped_column(String(16), nullable=False)
    status_mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    included_in_total: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    project: Mapped[MaintenanceProject] = relationship(back_populates="contracts")

    __table_args__ = (
        CheckConstraint(
            "status_mapping_state IN ('mapped', 'unmapped')",
            name="ck_maintenance_project_contract_status_mapping",
        ),
        CheckConstraint(
            "char_length(btrim(status_mapping_version)) > 0",
            name="ck_maintenance_project_contract_mapping_version",
        ),
        CheckConstraint(
            "status_mapping_state = 'mapped' OR included_in_total = false",
            name="ck_maintenance_project_contract_unmapped_excluded",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_maintenance_project_contract_interval",
        ),
        CheckConstraint(
            "contract_amount IS NULL OR "
            "(contract_amount >= 0 AND contract_amount < 1000000000000)",
            name="ck_maintenance_project_contract_amount",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_maintenance_project_contract_version",
        ),
        # Exact duplicate relationship versions are rejected in storage.  Overlapping
        # non-identical intervals remain auditable and fail closed in the read model.
        UniqueConstraint(
            "project_id",
            "contract_id",
            "effective_from",
            name="uq_maintenance_project_contract_identity",
        ),
        Index("ix_maintenance_project_contract_project", "project_id"),
        Index("ix_maintenance_project_contract_contract", "contract_id"),
        Index(
            "ix_maintenance_project_contract_effective",
            "included_in_total",
            "effective_from",
            "effective_to",
        ),
    )


class MaintenanceProjectUserAssignment(Base):
    """Auditable link from one stable project to its explicit primary manager."""

    __tablename__ = "maintenance_project_user_assignment"

    assignment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"),
        nullable=False,
    )
    responsibility_type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id"), nullable=False)
    source_manager_text: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    assigned_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
    )
    assigned_by: Mapped[str] = mapped_column(String(64), nullable=False)
    assignment_reason: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    archived_by: Mapped[str | None] = mapped_column(String(64))
    archive_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "responsibility_type = 'primary_manager'",
            name="ck_maintenance_project_user_assignment_type",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_maintenance_project_user_assignment_version",
        ),
        CheckConstraint(
            "char_length(btrim(assigned_by)) > 0",
            name="ck_maintenance_project_user_assignment_assigner",
        ),
        CheckConstraint(
            "char_length(btrim(assignment_reason)) > 0",
            name="ck_maintenance_project_user_assignment_reason",
        ),
        CheckConstraint(
            "(archived_at IS NULL AND archived_by IS NULL AND archive_reason IS NULL) OR "
            "(archived_at IS NOT NULL AND archived_by IS NOT NULL AND "
            "archive_reason IS NOT NULL AND char_length(btrim(archived_by)) > 0 AND "
            "char_length(btrim(archive_reason)) > 0 AND archived_at >= assigned_at)",
            name="ck_maintenance_project_user_assignment_archive",
        ),
        Index(
            "ux_maintenance_project_primary_manager_active",
            "project_id",
            unique=True,
            postgresql_where=text(
                "archived_at IS NULL AND responsibility_type = 'primary_manager'"
            ),
        ),
        Index(
            "ix_maintenance_project_user_assignment_user_active",
            "user_id",
            "archived_at",
        ),
        Index(
            "ix_maintenance_project_user_assignment_project_time",
            "project_id",
            "assigned_at",
            "assignment_id",
        ),
    )


class MaintenanceProjectAuditLog(Base):
    """Business audit for string-identified maintenance project facts."""

    __tablename__ = "maintenance_project_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    operated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ("
            "'project', 'project_contract', 'manager_assignment', "
            "'source_order_assignment'"
            ")",
            name="ck_maintenance_project_audit_entity_type",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maintenance_project_audit_reason",
        ),
        CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_project_audit_operator",
        ),
        Index(
            "ix_maintenance_project_audit_project_time",
            "project_id",
            "operated_at",
            "id",
        ),
        Index(
            "ix_maintenance_project_audit_entity_time",
            "entity_type",
            "entity_id",
            "operated_at",
            "id",
        ),
    )
