"""maintenance RKD return facts (F3): canonical bad-part return lines

Revision ID: a7c3e5f9b2d1
Revises: f1a2b3c4d5e6
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a7c3e5f9b2d1"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_rkd_return_line",
        sa.Column("rkd_line_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("head_row_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("head_no", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=96), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=True),
        sa.Column("pn", sa.String(length=128), nullable=False),
        sa.Column("qty", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("test_result", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("qty > 0", name="ck_maintenance_rkd_return_qty"),
        sa.CheckConstraint(
            "char_length(btrim(pn)) > 0", name="ck_maintenance_rkd_return_pn"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["maintenance_doc_import_batch.batch_id"],
            name="fk_maintenance_rkd_return_batch",
        ),
        sa.ForeignKeyConstraint(
            ["head_row_id"],
            ["maintenance_doc_head_row.row_id"],
            name="fk_maintenance_rkd_return_head_row",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["maintenance_project.project_id"],
            name="fk_maintenance_rkd_return_project",
        ),
        sa.ForeignKeyConstraint(
            ["part_id"], ["dim_part.id"], name="fk_maintenance_rkd_return_part"
        ),
        sa.PrimaryKeyConstraint("rkd_line_id"),
        sa.UniqueConstraint("source_ref", name="uq_maintenance_rkd_return_source_ref"),
    )
    op.create_index(
        "ix_maintenance_rkd_return_project",
        "maintenance_rkd_return_line",
        ["project_id", "part_id", "occurred_at"],
    )


def downgrade() -> None:
    # 返还事实是返还率唯一分子来源；已有事实时禁止回滚（防历史口径塌陷）。
    op.execute("LOCK TABLE maintenance_rkd_return_line IN ACCESS EXCLUSIVE MODE")

    op.execute(
        """
        DO $guard$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_rkd_return_line)
          THEN
            RAISE EXCEPTION
              'a7c3e5f9b2d1 downgrade blocked: RKD return facts exist';
          END IF;
        END
        $guard$;
        """
    )
    op.drop_index(
        "ix_maintenance_rkd_return_project",
        table_name="maintenance_rkd_return_line",
    )
    op.drop_table("maintenance_rkd_return_line")
