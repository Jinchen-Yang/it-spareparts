"""maintenance collection reminder evidence (F6): upload closes reminders

Revision ID: c3e9d1b7f5a2
Revises: b9d1e7c3f5a8
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa

revision = "c3e9d1b7f5a2"
down_revision = "b9d1e7c3f5a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_collection_evidence",
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("milestone_id", sa.String(length=36), nullable=False),
        sa.Column("file_id", sa.String(length=36), nullable=False),
        sa.Column("md5", sa.String(length=32), nullable=False),
        sa.Column("uploaded_by", sa.String(length=64), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "is_active", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column("archived_by", sa.String(length=64), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "md5 ~ '^[a-f0-9]{32}$'",
            name="ck_maintenance_collection_evidence_md5",
        ),
        sa.CheckConstraint(
            "(is_active AND archived_at IS NULL AND archived_by IS NULL) OR "
            "(NOT is_active AND archived_at IS NOT NULL AND archived_by IS NOT NULL)",
            name="ck_maintenance_collection_evidence_archive_state",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_maintenance_collection_evidence_version"
        ),
        sa.ForeignKeyConstraint(
            ["milestone_id"],
            ["maintenance_collection_milestone.milestone_id"],
            name="fk_maintenance_collection_evidence_milestone",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["business_file.file_id"],
            name="fk_maintenance_collection_evidence_file",
        ),
        sa.PrimaryKeyConstraint("evidence_id"),
        sa.UniqueConstraint(
            "milestone_id",
            "file_id",
            name="uq_maintenance_collection_evidence_file",
        ),
    )
    op.create_index(
        "ix_maintenance_collection_evidence_milestone",
        "maintenance_collection_evidence",
        ["milestone_id", "is_active"],
    )


def downgrade() -> None:
    # 凭证是回款提醒关闭依据；已有凭证时禁止回滚。
    op.execute(
        """
        DO $guard$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_collection_evidence)
          THEN
            RAISE EXCEPTION
              'c3e9d1b7f5a2 downgrade blocked: collection evidence exists';
          END IF;
        END
        $guard$;
        """
    )
    op.drop_index(
        "ix_maintenance_collection_evidence_milestone",
        table_name="maintenance_collection_evidence",
    )
    op.drop_table("maintenance_collection_evidence")
