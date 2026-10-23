"""State conversion ledger beside, never inside, immutable legacy evidence."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MigrationSnapshotState(Base):
    __tablename__ = "pn_catalog_migration_snapshot"
    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_schema: Mapped[str] = mapped_column(String(128))
    registration_sha256: Mapped[str] = mapped_column(String(64))
    inventory: Mapped[dict] = mapped_column(JSONB)
    fence_evidence: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    audit_id: Mapped[int] = mapped_column(ForeignKey("pn_catalog_audit.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_pn_migration_snapshot_revision"),
    )


class MigrationRecordApplication(Base):
    __tablename__ = "pn_catalog_migration_application"
    source_record_id: Mapped[int] = mapped_column(
        ForeignKey("pn_catalog_legacy_record.id"), primary_key=True
    )
    namespace: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str | None] = mapped_column(String(128))
    target_ref: Mapped[dict | None] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    audit_id: Mapped[int] = mapped_column(ForeignKey("pn_catalog_audit.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace", "snapshot_id"],
            [
                "pn_catalog_migration_snapshot.namespace",
                "pn_catalog_migration_snapshot.snapshot_id",
            ],
        ),
        CheckConstraint(
            "state IN ('applied','historical_terminal','recovery_required','dependency_blocked','conflict')",
            name="ck_pn_migration_application_state",
        ),
        CheckConstraint("revision >= 1", name="ck_pn_migration_application_revision"),
        Index(
            "ix_pn_migration_application_state",
            "namespace",
            "snapshot_id",
            "state",
            "source_record_id",
        ),
    )


class MigrationRecoveryDecision(Base):
    __tablename__ = "pn_catalog_migration_recovery"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    source_record_id: Mapped[int] = mapped_column(
        ForeignKey("pn_catalog_migration_application.source_record_id")
    )
    expected_revision: Mapped[int] = mapped_column(BigInteger)
    decision_kind: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSONB)
    audit_id: Mapped[int] = mapped_column(ForeignKey("pn_catalog_audit.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "expected_revision >= 1", name="ck_pn_migration_recovery_revision"
        ),
    )
