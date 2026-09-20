"""Demand manual maintenance: per-field override ledger + page_manual source.

Phase E of v1.36: enables in-page editing of maintenance demand lines
(f_maintenance_line) without the next WBDD upsert silently reverting them.

- manual_override JSONB default '{}': {field: {value, source_value, updated_by,
  updated_at}} — source_value snapshot makes clear_override restorable.
- extends edited_source semantics with 'page_manual' (no new boolean flag:
  one concept, one column — workbook_manual is the existing precedent).
- loader protection + snapshot_diff exclusion read this column/enum in code;
  the DB keeps only the storage.

Deploy note (must go into the release plan): FMaintenanceLine gains columns,
so the version_digest of every in-flight (armed, not executed) two-phase
delete intent becomes stale and will Conflict on execute — operators must
recreate those intents after upgrade.

Revision ID: c8f2b5d9a4e7
Revises: b5e8d1a7c3f9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8f2b5d9a4e7"
down_revision: str | None = "b5e8d1a7c3f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "f_maintenance_line"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        _TABLE,
        sa.Column(
            "manual_override",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Manual edits protected only by this ledger are lost on downgrade;
    # the WBDD upsert will revert those fields on next import. Documented.
    op.drop_column(_TABLE, "manual_override")
