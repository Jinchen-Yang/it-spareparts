"""Explicit resource grants and customer ownership, independent of legacy role snapshots."""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class SysAuthorizationArtifact(Base):
    """Stable producer/command binding; existence alone never grants access."""
    __tablename__ = 'sys_authorization_artifact'
    __table_args__ = (
        CheckConstraint('generation >= 1 AND generation <= 9007199254740991', name='ck_authorization_artifact_generation'),
        CheckConstraint('enqueued_epoch >= 0 AND enqueued_epoch <= 9007199254740991', name='ck_authorization_artifact_epoch'),
        CheckConstraint("input_digest ~ '^[0-9a-f]{64}$'", name='ck_authorization_artifact_digest'),
        CheckConstraint("(command_namespace IS NULL AND idempotency_key IS NULL) OR "
                        "(command_namespace IS NOT NULL AND command_namespace <> '' AND "
                        "idempotency_key IS NOT NULL AND idempotency_key <> '')", name='ck_authorization_artifact_command'),
        Index('ux_authorization_artifact_command', 'command_namespace', 'owner_user_id', 'idempotency_key',
              unique=True, postgresql_where=text('idempotency_key IS NOT NULL')),
        Index('ix_authorization_artifact_owner_updated', 'owner_user_id', 'updated_at'),
    )
    domain_kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    domain_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey('sys_user.id', ondelete='RESTRICT'))
    generation: Mapped[int] = mapped_column(BigInteger)
    enqueued_epoch: Mapped[int] = mapped_column(BigInteger)
    input_digest: Mapped[str] = mapped_column(String(64))
    command_namespace: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    authorization_provenance: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    artifact_manifests: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), onupdate=func.now())


class SysAuthorizationMigrationReceipt(Base):
    """Migration before/after evidence and monotonic artifact-format marker."""

    __tablename__ = 'sys_authorization_migration_receipt'
    migration_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    before_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    after_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class SysAuthorizationCutover(Base):
    """Persistent resource authority; only trusted startup initialization activates it."""

    __tablename__ = "sys_authorization_cutover"

    resource: Mapped[str] = mapped_column(String(64), primary_key=True)
    activated_catalog_version: Mapped[str] = mapped_column(String(128))
    activated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class SysAuthorizationGrant(Base):
    __tablename__ = "sys_authorization_grant"
    __table_args__ = (
        UniqueConstraint("user_id", "resource", name="uq_authorization_grant_user_resource"),
        CheckConstraint("version >= 1", name="ck_authorization_grant_version"),
        CheckConstraint("scope IN ('all', 'all_contracts', 'all_imports', 'all_projects', 'all_sources', 'assigned_customers', 'assigned_or_sales_projects', 'assigned_projects', 'canonical_sales_projects', 'own_applications', 'own_imports', 'own_proposals', 'own_source_sales', 'primary_managed_active_projects', 'primary_managed_projects')", name="ck_authorization_grant_scope"),
        CheckConstraint("jsonb_typeof(actions) = 'array'", name="ck_authorization_grant_actions_array"),
        CheckConstraint("jsonb_typeof(field_permissions) = 'array'", name="ck_authorization_grant_fields_array"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"))
    resource: Mapped[str] = mapped_column(String(64))
    actions: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    scope: Mapped[str] = mapped_column(String(32), default="assigned_customers", server_default="assigned_customers")
    field_permissions: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class SysCustomerAssignment(Base):
    __tablename__ = "sys_customer_assignment"
    __table_args__ = (Index("ix_customer_assignment_customer", "customer_id"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("dim_customer.id", ondelete="CASCADE"), primary_key=True)
