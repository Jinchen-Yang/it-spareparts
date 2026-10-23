"""Explicit resource grants and customer ownership, independent of legacy role snapshots."""
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SysAuthorizationGrant(Base):
    __tablename__ = "sys_authorization_grant"
    __table_args__ = (
        UniqueConstraint("user_id", "resource", name="uq_authorization_grant_user_resource"),
        CheckConstraint("version >= 1", name="ck_authorization_grant_version"),
        CheckConstraint("scope IN ('all', 'assigned_customers')", name="ck_authorization_grant_scope"),
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
