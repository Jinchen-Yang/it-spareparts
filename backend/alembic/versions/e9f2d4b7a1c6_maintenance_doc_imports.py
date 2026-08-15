"""maintenance doc imports (RKD/return/BXD): generic raw layer (C1b)

Revision ID: e9f2d4b7a1c6
Revises: d1e3f5a7c2b9
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e9f2d4b7a1c6"
down_revision = "d1e3f5a7c2b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_doc_import_batch",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("doc_type", sa.String(length=16), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("uploaded_by", sa.String(length=64), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("head_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("line_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("issue_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="'pending'", nullable=False
        ),
        sa.Column("report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("applied_by", sa.String(length=64), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "doc_type IN ('rkd_inbound', 'return_order', 'bxd_expense')",
            name="ck_maintenance_doc_import_doc_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_maintenance_doc_import_status",
        ),
        sa.CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL AND applied_by IS NOT NULL)",
            name="ck_maintenance_doc_import_applied",
        ),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    op.create_index(
        "ix_maintenance_doc_import_hash", "maintenance_doc_import_batch", ["file_hash"]
    )
    op.create_index(
        "ix_maintenance_doc_import_type_uploaded",
        "maintenance_doc_import_batch",
        ["doc_type", "uploaded_at"],
    )

    op.create_table(
        "maintenance_doc_head_row",
        sa.Column("row_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("head_no", sa.String(length=64), nullable=True),
        sa.Column("head_date", sa.Date(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("wbdd_no", sa.String(length=64), nullable=True),
        sa.Column("xsdd_no", sa.String(length=64), nullable=True),
        sa.Column("project_name", sa.String(length=256), nullable=True),
        sa.Column("data_status", sa.String(length=64), nullable=True),
        sa.Column("issues", postgresql.ARRAY(sa.String(length=128)), nullable=True),
        sa.CheckConstraint("row_no >= 1", name="ck_maintenance_doc_head_row_no"),
        sa.ForeignKeyConstraint(["batch_id"], ["maintenance_doc_import_batch.batch_id"]),
        sa.PrimaryKeyConstraint("row_id"),
    )
    op.create_index("ix_maintenance_doc_head_batch", "maintenance_doc_head_row", ["batch_id"])
    op.create_index("ix_maintenance_doc_head_no", "maintenance_doc_head_row", ["head_no"])

    op.create_table(
        "maintenance_doc_line_row",
        sa.Column("row_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("head_row_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("line_key", sa.String(length=64), nullable=True),
        sa.Column("pn", sa.String(length=128), nullable=True),
        sa.Column("qty", sa.Numeric(14, 3), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("test_result", sa.String(length=64), nullable=True),
        sa.Column("warehouse", sa.String(length=128), nullable=True),
        sa.Column("location", sa.String(length=128), nullable=True),
        sa.Column("issues", postgresql.ARRAY(sa.String(length=128)), nullable=True),
        sa.CheckConstraint("row_no >= 1", name="ck_maintenance_doc_line_row_no"),
        sa.ForeignKeyConstraint(["batch_id"], ["maintenance_doc_import_batch.batch_id"]),
        sa.ForeignKeyConstraint(["head_row_id"], ["maintenance_doc_head_row.row_id"]),
        sa.PrimaryKeyConstraint("row_id"),
    )
    op.create_index("ix_maintenance_doc_line_batch", "maintenance_doc_line_row", ["batch_id"])
    op.create_index("ix_maintenance_doc_line_head", "maintenance_doc_line_row", ["head_row_id"])


def downgrade() -> None:
    op.drop_index("ix_maintenance_doc_line_head", table_name="maintenance_doc_line_row")
    op.drop_index("ix_maintenance_doc_line_batch", table_name="maintenance_doc_line_row")
    op.drop_table("maintenance_doc_line_row")
    op.drop_index("ix_maintenance_doc_head_no", table_name="maintenance_doc_head_row")
    op.drop_index("ix_maintenance_doc_head_batch", table_name="maintenance_doc_head_row")
    op.drop_table("maintenance_doc_head_row")
    op.drop_index(
        "ix_maintenance_doc_import_type_uploaded", table_name="maintenance_doc_import_batch"
    )
    op.drop_index("ix_maintenance_doc_import_hash", table_name="maintenance_doc_import_batch")
    op.drop_table("maintenance_doc_import_batch")
