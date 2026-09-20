"""RKD return ledger: serial number evidence for manual registrations.

Adds maintenance_rkd_return_line.serial_numbers (JSONB, default '[]') so
manual batch entry can attach per-unit SN evidence. Rule (enforced in the
service layer, not the DB): when SNs are present, qty must equal the number
of serials; rkd_import rows keep an empty list.

Revision ID: b5e8d1a7c3f9
Revises: e3a7b9c2d4f6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5e8d1a7c3f9"
down_revision: str | None = "e3a7b9c2d4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "maintenance_rkd_return_line"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        _TABLE,
        sa.Column(
            "serial_numbers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    with_data = op.get_bind().execute(sa.text(
        f"SELECT count(*) FROM {_TABLE} "
        f"WHERE jsonb_array_length(serial_numbers) > 0"
    )).scalar()
    if with_data:
        raise RuntimeError(
            f"downgrade refused: {with_data} return receipt rows carry recorded "
            "serial numbers; export them before dropping the column"
        )
    op.drop_column(_TABLE, "serial_numbers")
