"""maintenance bad-part salvage registry (F5)

Revision ID: b9d1e7c3f5a8
Revises: a7c3e5f9b2d1
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa

revision = "b9d1e7c3f5a8"
down_revision = "a7c3e5f9b2d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_bad_salvage",
        sa.Column("salvage_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=True),
        sa.Column("pn", sa.String(length=128), nullable=False),
        sa.Column("qty", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("revenue", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("salvage_date", sa.Date(), nullable=False),
        sa.Column("buyer_note", sa.String(length=256), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("operated_by", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("voided_by", sa.String(length=64), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("qty > 0", name="ck_maintenance_bad_salvage_qty"),
        sa.CheckConstraint(
            "revenue >= 0 AND revenue < 100000000000",
            name="ck_maintenance_bad_salvage_revenue",
        ),
        sa.CheckConstraint(
            "char_length(btrim(pn)) > 0", name="ck_maintenance_bad_salvage_pn"
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[a-f0-9]{64}$'",
            name="ck_maintenance_bad_salvage_payload_digest",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_bad_salvage_version"),
        sa.CheckConstraint(
            "(is_active AND voided_at IS NULL AND voided_by IS NULL) OR "
            "(NOT is_active AND voided_at IS NOT NULL AND voided_by IS NOT NULL)",
            name="ck_maintenance_bad_salvage_void_state",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["maintenance_project.project_id"],
            name="fk_maintenance_bad_salvage_project",
        ),
        sa.ForeignKeyConstraint(
            ["part_id"], ["dim_part.id"], name="fk_maintenance_bad_salvage_part"
        ),
        sa.PrimaryKeyConstraint("salvage_id"),
        sa.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_maintenance_bad_salvage_idempotency",
        ),
    )
    op.create_index(
        "ix_maintenance_bad_salvage_project_date",
        "maintenance_bad_salvage",
        ["project_id", "salvage_date"],
    )


def downgrade() -> None:
    # 变卖登记是回收监控事实；已有事实时禁止回滚。
    op.execute(
        """
        DO $guard$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_bad_salvage)
          THEN
            RAISE EXCEPTION
              'b9d1e7c3f5a8 downgrade blocked: bad salvage facts exist';
          END IF;
        END
        $guard$;
        """
    )
    op.drop_index(
        "ix_maintenance_bad_salvage_project_date",
        table_name="maintenance_bad_salvage",
    )
    op.drop_table("maintenance_bad_salvage")
