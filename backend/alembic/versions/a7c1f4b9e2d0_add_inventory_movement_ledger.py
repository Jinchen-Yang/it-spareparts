"""add inventory_movement ledger (perpetual inventory, §7.6)

Revision ID: a7c1f4b9e2d0
Revises: 2de8eaf62e48
Create Date: 2026-06-14 23:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c1f4b9e2d0"
down_revision: Union[str, Sequence[str], None] = "2de8eaf62e48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_movement",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("raw_movement_id", sa.String(length=64), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn_std", sa.String(length=128), nullable=False),
        sa.Column("pn_raw", sa.String(length=256), nullable=True),
        sa.Column("warehouse", sa.String(length=64), nullable=False),
        sa.Column("movement_date", sa.Date(), nullable=True),
        sa.Column("doc_type", sa.String(length=24), nullable=False),
        sa.Column("doc_type_raw", sa.String(length=64), nullable=True),
        sa.Column("doc_no", sa.String(length=64), nullable=True),
        sa.Column("direction", sa.SmallInteger(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("is_absolute", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("counterpart_warehouse", sa.String(length=64), nullable=True),
        sa.Column("ledger_kind", sa.String(length=8), server_default="part", nullable=False),
        sa.Column("snapshot_balance", sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["import_batch_id"], ["sys_import_batch.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_movement_id", name="uq_movement_raw_id"),
        sa.CheckConstraint("direction IN (-1, 0, 1)", name="ck_movement_direction"),
    )
    op.create_index("ix_movement_part_wh", "inventory_movement", ["part_id", "warehouse"])
    op.create_index("ix_movement_date", "inventory_movement", ["movement_date"])


def downgrade() -> None:
    op.drop_index("ix_movement_date", table_name="inventory_movement")
    op.drop_index("ix_movement_part_wh", table_name="inventory_movement")
    op.drop_table("inventory_movement")
