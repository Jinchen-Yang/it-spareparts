"""Site issue: persistent human-readable display_no (LY-YYYYMMDD-NNNN).

Task A of the 06 workbook fix: the hash-shaped auto number
LY-AUTO-<sha256[:20]>-<sha256[:24]> stays the internal identity (dedup,
replay, unique constraints untouched); a per-day locked counter allocates a
readable display number at first creation only. This migration adds the
counter table, the nullable display_no column (format-checked, uniquely
indexed), deterministically backfills existing LY-AUTO rows ordered by
created_at then issue_id per issue_date, and seeds the counters so future
allocations never reuse a backfilled number. Downgrade drops everything this
revision created — it never modifies existing issue_no values.

Revision ID: c8f3d1a7e2b9
Revises: e7c1f9a3b5d2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8f3d1a7e2b9"
down_revision: str | None = "e7c1f9a3b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ISSUE_TABLE = "maintenance_site_issue"
_COUNTER_TABLE = "maintenance_site_issue_display_counter"

_BACKFILL_SQL = sa.text(
    """
    WITH ranked AS (
        SELECT issue_id, issue_date,
               ROW_NUMBER() OVER (
                   PARTITION BY issue_date ORDER BY created_at, issue_id
               ) AS seq
        FROM maintenance_site_issue
        WHERE issue_no LIKE 'LY-AUTO-%'
    )
    UPDATE maintenance_site_issue AS msi
    SET display_no = 'LY-' || to_char(ranked.issue_date, 'YYYYMMDD') || '-'
                     || lpad(ranked.seq::text, 4, '0')
    FROM ranked
    WHERE msi.issue_id = ranked.issue_id
    """
)

# 'LY-YYYYMMDD-' 占 12 字符，序号从第 13 位起。
_SEED_COUNTER_SQL = sa.text(
    f"""
    INSERT INTO {_COUNTER_TABLE} (issue_date, last_sequence)
    SELECT issue_date, max(substr(display_no, 13)::int)
    FROM {_ISSUE_TABLE}
    WHERE display_no IS NOT NULL
    GROUP BY issue_date
    """
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        _COUNTER_TABLE,
        sa.Column("issue_date", sa.Date(), primary_key=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "last_sequence >= 1",
            name=op.f("ck_maintenance_site_issue_display_counter_seq"),
        ),
    )
    op.add_column(
        _ISSUE_TABLE,
        sa.Column("display_no", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_display_no_format",
        _ISSUE_TABLE,
        "display_no IS NULL OR display_no ~ '^LY-[0-9]{8}-[0-9]{4,}$'",
    )
    # 先回填、后建唯一索引：索引落在回填结果之上，同日序号天然互不重复。
    op.get_bind().execute(_BACKFILL_SQL)
    op.get_bind().execute(_SEED_COUNTER_SQL)
    op.create_index(
        "ux_maintenance_site_issue_display_no",
        _ISSUE_TABLE,
        ["display_no"],
        unique=True,
        postgresql_where=sa.text("display_no IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 只删本迁移新增的对象；issue_no 等既有值一律不动。
    op.drop_index(
        "ux_maintenance_site_issue_display_no",
        table_name=_ISSUE_TABLE,
        postgresql_where=sa.text("display_no IS NOT NULL"),
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_display_no_format", _ISSUE_TABLE, type_="check"
    )
    op.drop_column(_ISSUE_TABLE, "display_no")
    op.drop_table(_COUNTER_TABLE)
