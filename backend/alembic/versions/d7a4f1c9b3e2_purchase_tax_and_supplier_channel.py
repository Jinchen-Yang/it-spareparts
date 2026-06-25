"""purchase tax columns (是否含税/税金/含税金额) + supplier source_channel (采购分析面板)

Revision ID: d7a4f1c9b3e2
Revises: c1d7e3a9f2b4
Create Date: 2026-06-25 12:00:00.000000

仅新增可空列（additive），不回填、不改既有行；新口径靠重导采购填充。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a4f1c9b3e2"
down_revision: Union[str, Sequence[str], None] = "c1d7e3a9f2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("f_purchase_order", sa.Column("is_tax_inclusive", sa.Boolean(), nullable=True))
    op.add_column("f_purchase_order", sa.Column("tax_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("f_purchase_order", sa.Column("amount_inc_tax", sa.Numeric(14, 2), nullable=True))
    op.add_column("dim_supplier", sa.Column("source_channel", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("dim_supplier", "source_channel")
    op.drop_column("f_purchase_order", "amount_inc_tax")
    op.drop_column("f_purchase_order", "tax_amount")
    op.drop_column("f_purchase_order", "is_tax_inclusive")
