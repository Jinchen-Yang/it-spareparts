"""Durable single PN catalog delivery, no business facts.

Revision ID: c6e8a1b3d5f7
Revises: d6e8f0a2b4c6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c6e8a1b3d5f7"
down_revision = "d6e8f0a2b4c6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_delivery",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        sa.Column("last_error_code", sa.String(40)),
        sa.Column("receipt_status", sa.String(16)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('pending','delivering','succeeded','conflict')", name="ck_catalog_delivery_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_catalog_delivery_attempts"),
        sa.CheckConstraint("(state = 'delivering') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)", name="ck_catalog_delivery_lease"),
        sa.CheckConstraint("receipt_status IS NULL OR receipt_status IN ('applied','unchanged','conflict')", name="ck_catalog_delivery_receipt"),
    )
    op.create_index("ix_catalog_delivery_pending", "catalog_delivery", ["available_at", "created_at"], postgresql_where=sa.text("state = 'pending'"))
    op.create_index("ix_catalog_delivery_lease", "catalog_delivery", ["lease_until"], postgresql_where=sa.text("state = 'delivering'"))


def downgrade():
    op.drop_table("catalog_delivery")
