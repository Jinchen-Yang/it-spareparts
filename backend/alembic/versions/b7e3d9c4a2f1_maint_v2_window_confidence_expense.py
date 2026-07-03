"""维保 v2（§16）：window 取价层元数据列 + 报销费用表 f_project_expense

Revision ID: b7e3d9c4a2f1
Revises: a3f8c1d6e2b9
Create Date: 2026-07-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3d9c4a2f1"
down_revision: Union[str, Sequence[str], None] = "a3f8c1d6e2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("f_maintenance_line",
                  sa.Column("price_distance_days", sa.SmallInteger(), nullable=True))
    op.add_column("f_maintenance_line",
                  sa.Column("confidence", sa.String(length=8), nullable=True))

    op.create_table(
        "f_project_expense",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("raw_line_id", sa.String(length=80), nullable=False),
        sa.Column("bxd_no", sa.String(length=64), nullable=True),
        sa.Column("line_no", sa.Integer(), nullable=True),
        sa.Column("data_status", sa.String(length=16), nullable=True),
        sa.Column("expense_date", sa.Date(), nullable=True),
        sa.Column("person", sa.String(length=64), nullable=True),
        sa.Column("expense_type", sa.String(length=64), nullable=True),
        sa.Column("fee_category", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("linked_sales_order_no", sa.String(length=64), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("import_batch_id", sa.Integer(),
                  sa.ForeignKey("sys_import_batch.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("raw_line_id"),
    )
    op.create_index("ix_pe_bxd", "f_project_expense", ["bxd_no"])
    op.create_index("ix_pe_linked", "f_project_expense", ["linked_sales_order_no"])
    op.create_index("ix_pe_status_date", "f_project_expense", ["data_status", "expense_date"])


def downgrade() -> None:
    op.drop_table("f_project_expense")
    op.drop_column("f_maintenance_line", "confidence")
    op.drop_column("f_maintenance_line", "price_distance_days")
