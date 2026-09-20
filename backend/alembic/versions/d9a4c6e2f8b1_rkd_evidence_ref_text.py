"""Return receipt evidence_ref: 128 → Text (16384) for multi-SN scanning.

Users scan many SNs / paste courier numbers into the manual registration's
evidence field; String(128) filled after a few scans. Widening to Text keeps
existing values untouched (no rewrite, no truncation). Downgrade refuses when
any row exceeds 128 chars (data-preserving rollback contract).

Revision ID: d9a4c6e2f8b1
Revises: c8f2b5d9a4e7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9a4c6e2f8b1"
down_revision: str | None = "c8f2b5d9a4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "maintenance_rkd_return_line"
_COL = "evidence_ref"
_LIMIT = 128


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # varchar → text 是放宽，无需 USING 重写；显式 cast 仅为防御性明确。
    op.alter_column(
        _TABLE, _COL,
        existing_type=sa.String(128),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using=f"{_COL}::text",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    too_long = op.get_bind().execute(
        sa.text(
            f"SELECT count(*) FROM {_TABLE} "
            f"WHERE {_COL} IS NOT NULL AND length({_COL}) > {_LIMIT}"
        )
    ).scalar()
    if too_long:
        raise RuntimeError(
            f"downgrade refused: {too_long} return receipt rows carry "
            f"evidence_ref longer than {_LIMIT} chars; export them first"
        )
    op.alter_column(
        _TABLE, _COL,
        existing_type=sa.Text(),
        type_=sa.String(128),
        existing_nullable=True,
    )
