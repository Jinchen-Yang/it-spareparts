"""maintenance CKD shipment import: raw head/line rows and batch (C1a/F1)

Revision ID: d1e3f5a7c2b9
Revises: c3b5d9e1f7a2
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d1e3f5a7c2b9"
down_revision = "c3b5d9e1f7a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_ckd_import_batch",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
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
            "status IN ('pending', 'applied', 'failed')",
            name="ck_maintenance_ckd_import_status",
        ),
        sa.CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL AND applied_by IS NOT NULL)",
            name="ck_maintenance_ckd_import_applied",
        ),
        sa.PrimaryKeyConstraint("batch_id"),
        sa.UniqueConstraint(
            "uploaded_by", "idempotency_key",
            name="uq_maintenance_ckd_import_idempotency",
        ),
    )
    op.create_index(
        "ix_maintenance_ckd_import_hash", "maintenance_ckd_import_batch", ["file_hash"]
    )
    op.create_index(
        "ix_maintenance_ckd_import_uploaded",
        "maintenance_ckd_import_batch",
        ["uploaded_at"],
    )

    op.create_table(
        "maintenance_ckd_head_row",
        sa.Column("row_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("order_no_raw", sa.String(length=64), nullable=True),
        sa.Column("order_date_raw", sa.String(length=64), nullable=True),
        sa.Column("category_raw", sa.String(length=64), nullable=True),
        sa.Column("machine_or_part_raw", sa.String(length=64), nullable=True),
        sa.Column("warehouse_raw", sa.String(length=128), nullable=True),
        sa.Column("wh_center_raw", sa.String(length=128), nullable=True),
        sa.Column("wbdd_raw", sa.String(length=64), nullable=True),
        sa.Column("wbdd_parts_raw", sa.String(length=64), nullable=True),
        sa.Column("sales_order_raw", sa.String(length=64), nullable=True),
        sa.Column("salesperson_raw", sa.String(length=64), nullable=True),
        sa.Column("project_manager_raw", sa.String(length=128), nullable=True),
        sa.Column("maintainer_raw", sa.String(length=128), nullable=True),
        sa.Column("data_status_raw", sa.String(length=64), nullable=True),
        sa.Column("remark_raw", sa.Text(), nullable=True),
        sa.Column("order_no", sa.String(length=64), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("wbdd_no", sa.String(length=64), nullable=True),
        sa.Column("wbdd_parts_no", sa.String(length=64), nullable=True),
        sa.Column("sales_order_no", sa.String(length=64), nullable=True),
        sa.Column("issues", postgresql.ARRAY(sa.String(length=128)), nullable=True),
        sa.CheckConstraint("row_no >= 1", name="ck_maintenance_ckd_head_row_no"),
        sa.ForeignKeyConstraint(["batch_id"], ["maintenance_ckd_import_batch.batch_id"]),
        sa.PrimaryKeyConstraint("row_id"),
    )
    op.create_index("ix_maintenance_ckd_head_batch", "maintenance_ckd_head_row", ["batch_id"])
    op.create_index("ix_maintenance_ckd_head_order", "maintenance_ckd_head_row", ["order_no"])

    op.create_table(
        "maintenance_ckd_line_row",
        sa.Column("row_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("head_row_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("data_id_raw", sa.String(length=64), nullable=True),
        sa.Column("seq_raw", sa.String(length=64), nullable=True),
        sa.Column("title_raw", sa.String(length=128), nullable=True),
        sa.Column("part_name_raw", sa.String(length=256), nullable=True),
        sa.Column("self_code_raw", sa.String(length=128), nullable=True),
        sa.Column("pn_raw", sa.String(length=128), nullable=True),
        sa.Column("sn_raw", sa.String(length=128), nullable=True),
        sa.Column("desc_raw", sa.Text(), nullable=True),
        sa.Column("warehouse_raw", sa.String(length=128), nullable=True),
        sa.Column("location_raw", sa.String(length=128), nullable=True),
        sa.Column("brand_raw", sa.String(length=64), nullable=True),
        sa.Column("category_major_raw", sa.String(length=64), nullable=True),
        sa.Column("category_minor_raw", sa.String(length=128), nullable=True),
        sa.Column("unit_raw", sa.String(length=16), nullable=True),
        sa.Column("out_qty_raw", sa.String(length=64), nullable=True),
        sa.Column("unit_cost_raw", sa.String(length=64), nullable=True),
        sa.Column("cost_amount_raw", sa.String(length=64), nullable=True),
        sa.Column("test_result_raw", sa.String(length=64), nullable=True),
        sa.Column("pn", sa.String(length=128), nullable=True),
        sa.Column("out_qty", sa.Numeric(14, 3), nullable=True),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("cost_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("issues", postgresql.ARRAY(sa.String(length=128)), nullable=True),
        sa.CheckConstraint("row_no >= 1", name="ck_maintenance_ckd_line_row_no"),
        sa.ForeignKeyConstraint(["batch_id"], ["maintenance_ckd_import_batch.batch_id"]),
        sa.ForeignKeyConstraint(["head_row_id"], ["maintenance_ckd_head_row.row_id"]),
        sa.PrimaryKeyConstraint("row_id"),
    )
    op.create_index("ix_maintenance_ckd_line_batch", "maintenance_ckd_line_row", ["batch_id"])
    op.create_index("ix_maintenance_ckd_line_head", "maintenance_ckd_line_row", ["head_row_id"])


def downgrade() -> None:
    op.execute("LOCK TABLE maintenance_ckd_import_batch, maintenance_ckd_head_row, maintenance_ckd_line_row, maintenance_front_stock_ledger IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
DO $guard$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_ckd_import_batch)
             OR EXISTS (SELECT 1 FROM maintenance_ckd_head_row)
             OR EXISTS (SELECT 1 FROM maintenance_ckd_line_row)
             OR EXISTS (SELECT 1 FROM maintenance_front_stock_ledger
                        WHERE source_type = 'ckd_shipment_line')
          THEN
            RAISE EXCEPTION
              'd1e3f5a7c2b9 downgrade blocked: CKD facts exist';
          END IF;
        END
        $guard$;
        """
    )
    op.drop_index("ix_maintenance_ckd_line_head", table_name="maintenance_ckd_line_row")
    op.drop_index("ix_maintenance_ckd_line_batch", table_name="maintenance_ckd_line_row")
    op.drop_table("maintenance_ckd_line_row")
    op.drop_index("ix_maintenance_ckd_head_order", table_name="maintenance_ckd_head_row")
    op.drop_index("ix_maintenance_ckd_head_batch", table_name="maintenance_ckd_head_row")
    op.drop_table("maintenance_ckd_head_row")
    op.drop_index(
        "ix_maintenance_ckd_import_uploaded", table_name="maintenance_ckd_import_batch"
    )
    op.drop_index("ix_maintenance_ckd_import_hash", table_name="maintenance_ckd_import_batch")
    op.drop_table("maintenance_ckd_import_batch")
