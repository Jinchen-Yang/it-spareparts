"""add maintenance dual-tax cost and historical reference provenance

Revision ID: e5f9a2b3c4d5
Revises: d4e8f1a2b3c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f9a2b3c4d5"
down_revision: str | None = "d4e8f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_COST_SQL = (
    "cost_amount IS NOT NULL"
    " AND cost_amount >= 0"
    " AND cost_amount < 1000000000000"
)
_ACTUAL_SOURCE_SQL = "cost_source IN ('direct', 'month_avg', 'window')"
_OLD_ESTIMATED_SOURCE_SQL = "cost_source IN ('sales_ref', 'trace_avg')"
_ESTIMATED_SOURCE_SQL = (
    "cost_source IN ("
    "'pool_purchase', 'pool_sales', 'purchase_history', 'sales_history',"
    " 'sales_ref', 'trace_avg'"
    ")"
)


def _cost_bucket_sql(estimated_sources: str) -> str:
    return (
        "CASE"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {_ACTUAL_SOURCE_SQL}"
        " AND cost_tax_basis = 'inc' THEN 1"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {_ACTUAL_SOURCE_SQL}"
        " AND cost_tax_basis = 'ex' THEN 2"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {estimated_sources}"
        " AND cost_tax_basis = 'inc' AND confidence = 'low' THEN 3"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {estimated_sources}"
        " AND cost_tax_basis = 'inc' THEN 4"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {estimated_sources}"
        " AND cost_tax_basis = 'ex' AND confidence = 'low' THEN 5"
        f" WHEN {_VALID_COST_SQL}"
        f" AND {estimated_sources}"
        " AND cost_tax_basis = 'ex' THEN 6"
        " ELSE 0 END"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # PostgreSQL 生成列表达式不可原地修改，先删后按新来源集合重建。
    op.drop_column("f_maintenance_line", "cost_bucket")
    for name in (
        "unit_cost_inc_tax",
        "unit_cost_ex_tax",
        "cost_amount_inc_tax",
        "cost_amount_ex_tax",
    ):
        op.add_column(
            "f_maintenance_line",
            sa.Column(name, sa.Numeric(14, 2), nullable=True),
        )
    op.add_column(
        "f_maintenance_line",
        sa.Column("reference_side", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "f_maintenance_line",
        sa.Column("reference_pool_group_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "f_maintenance_line",
        sa.Column("reference_pool_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "f_maintenance_line",
        sa.Column("reference_sample_count", sa.Integer(), nullable=True),
    )
    for name in (
        "reference_from_date",
        "reference_to_date",
        "reference_latest_date",
    ):
        op.add_column(
            "f_maintenance_line",
            sa.Column(name, sa.Date(), nullable=True),
        )
    op.add_column(
        "f_maintenance_line",
        sa.Column(
            "cost_bucket",
            sa.SmallInteger(),
            sa.Computed(_cost_bucket_sql(_ESTIMATED_SOURCE_SQL), persisted=True),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("f_maintenance_line", "cost_bucket")
    for name in (
        "reference_latest_date",
        "reference_to_date",
        "reference_from_date",
        "reference_sample_count",
        "reference_pool_version",
        "reference_pool_group_id",
        "reference_side",
        "cost_amount_ex_tax",
        "cost_amount_inc_tax",
        "unit_cost_ex_tax",
        "unit_cost_inc_tax",
    ):
        op.drop_column("f_maintenance_line", name)
    op.add_column(
        "f_maintenance_line",
        sa.Column(
            "cost_bucket",
            sa.SmallInteger(),
            sa.Computed(_cost_bucket_sql(_OLD_ESTIMATED_SOURCE_SQL), persisted=True),
            nullable=False,
        ),
    )
