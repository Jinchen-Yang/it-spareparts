"""maintenance front stock ledger: project-level front warehouse balance (B1)

Revision ID: b1e3f7d9c2a5
Revises: e7b3d9f2c1a4
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa

revision = "b1e3f7d9c2a5"
down_revision = "e7b3d9f2c1a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_front_stock",
        sa.Column("stock_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_name", sa.String(length=128), server_default="''", nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("unit_cost_ex_tax", sa.Numeric(14, 2), nullable=True),
        sa.Column("unit_cost_inc_tax", sa.Numeric(14, 2), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("qty >= 0", name="ck_maintenance_front_stock_qty_non_negative"),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_front_stock_version"),
        sa.CheckConstraint(
            "char_length(warehouse_name) <= 128",
            name="ck_maintenance_front_stock_warehouse_len",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.PrimaryKeyConstraint("stock_id"),
        sa.UniqueConstraint(
            "project_id", "part_id", "warehouse_name",
            name="uq_maintenance_front_stock_identity",
        ),
    )
    op.create_index("ix_maintenance_front_stock_project", "maintenance_front_stock", ["project_id"])
    op.create_index("ix_maintenance_front_stock_part", "maintenance_front_stock", ["part_id"])
    op.create_index("ix_maintenance_front_stock_inbound", "maintenance_front_stock", ["last_inbound_at"])

    op.create_table(
        "maintenance_front_stock_ledger",
        sa.Column("ledger_id", sa.String(length=36), nullable=False),
        sa.Column("stock_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=False),
        sa.Column("qty_change", sa.Numeric(14, 3), nullable=False),
        sa.Column("qty_after", sa.Numeric(14, 3), nullable=False),
        sa.Column("unit_cost_ex_tax", sa.Numeric(14, 2), nullable=True),
        sa.Column("unit_cost_inc_tax", sa.Numeric(14, 2), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("operated_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('shipment_in', 'purchase_in', 'return_out', 'salvage_out')",
            name="ck_maintenance_front_stock_ledger_kind",
        ),
        sa.CheckConstraint(
            "source_type IN ('f_maintenance_line', 'warehouse_document_line',"
            " 'salvage', 'manual')",
            name="ck_maintenance_front_stock_ledger_source_type",
        ),
        sa.CheckConstraint(
            "qty_change <> 0", name="ck_maintenance_front_stock_ledger_qty_change"
        ),
        sa.CheckConstraint(
            "qty_after >= 0", name="ck_maintenance_front_stock_ledger_qty_after"
        ),
        sa.CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_front_stock_ledger_operator",
        ),
        sa.ForeignKeyConstraint(["stock_id"], ["maintenance_front_stock.stock_id"]),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.PrimaryKeyConstraint("ledger_id"),
        sa.UniqueConstraint(
            "kind", "source_type", "source_ref", "part_id",
            name="uq_maintenance_front_stock_ledger_source",
        ),
    )
    op.create_index(
        "ix_maintenance_front_stock_ledger_project",
        "maintenance_front_stock_ledger",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_maintenance_front_stock_ledger_stock",
        "maintenance_front_stock_ledger",
        ["stock_id", "created_at"],
    )
    op.create_index(
        "ix_maintenance_front_stock_ledger_source",
        "maintenance_front_stock_ledger",
        ["source_type", "source_ref"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_maintenance_front_stock_ledger_source",
        table_name="maintenance_front_stock_ledger",
    )
    op.drop_index(
        "ix_maintenance_front_stock_ledger_stock",
        table_name="maintenance_front_stock_ledger",
    )
    op.drop_index(
        "ix_maintenance_front_stock_ledger_project",
        table_name="maintenance_front_stock_ledger",
    )
    op.drop_table("maintenance_front_stock_ledger")
    op.drop_index("ix_maintenance_front_stock_inbound", table_name="maintenance_front_stock")
    op.drop_index("ix_maintenance_front_stock_part", table_name="maintenance_front_stock")
    op.drop_index("ix_maintenance_front_stock_project", table_name="maintenance_front_stock")
    op.drop_table("maintenance_front_stock")
