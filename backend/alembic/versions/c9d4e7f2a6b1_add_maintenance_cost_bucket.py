"""add generated maintenance cost bucket

Revision ID: c9d4e7f2a6b1
Revises: f8c3d1a6b2e4

The single STORED column caches the strict cost-quality classification used by
maintenance aggregates. PostgreSQL rewrites the table while adding it, so the
production migration must run inside the normal maintenance window.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d4e7f2a6b1"
down_revision: str | None = "f8c3d1a6b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Schema snapshot of app.models.maintenance.MAINTENANCE_COST_BUCKET_SQL.
# maintenance_cost_quality parity tests guard all source/tax/amount/confidence
# combinations against this generated expression.
_VALID_COST_SQL = (
    "cost_amount IS NOT NULL"
    " AND cost_amount >= 0"
    " AND cost_amount < 1000000000000"
)
_ACTUAL_SOURCE_SQL = "cost_source IN ('direct', 'month_avg', 'window')"
_ESTIMATED_SOURCE_SQL = "cost_source IN ('sales_ref', 'trace_avg')"
_COST_BUCKET_SQL = (
    "CASE"
    f" WHEN {_VALID_COST_SQL}"
    f" AND {_ACTUAL_SOURCE_SQL}"
    " AND cost_tax_basis = 'inc' THEN 1"
    f" WHEN {_VALID_COST_SQL}"
    f" AND {_ACTUAL_SOURCE_SQL}"
    " AND cost_tax_basis = 'ex' THEN 2"
    f" WHEN {_VALID_COST_SQL}"
    f" AND {_ESTIMATED_SOURCE_SQL}"
    " AND cost_tax_basis = 'inc' AND confidence = 'low' THEN 3"
    f" WHEN {_VALID_COST_SQL}"
    f" AND {_ESTIMATED_SOURCE_SQL}"
    " AND cost_tax_basis = 'inc' THEN 4"
    f" WHEN {_VALID_COST_SQL}"
    f" AND {_ESTIMATED_SOURCE_SQL}"
    " AND cost_tax_basis = 'ex' AND confidence = 'low' THEN 5"
    f" WHEN {_VALID_COST_SQL}"
    f" AND {_ESTIMATED_SOURCE_SQL}"
    " AND cost_tax_basis = 'ex' THEN 6"
    " ELSE 0 END"
)


def upgrade() -> None:
    # Fail fast instead of waiting behind live traffic while requesting the
    # AccessExclusiveLock required by PostgreSQL's STORED-column table rewrite.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "f_maintenance_line",
        sa.Column(
            "cost_bucket",
            sa.SmallInteger(),
            sa.Computed(_COST_BUCKET_SQL, persisted=True),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("f_maintenance_line", "cost_bucket")
