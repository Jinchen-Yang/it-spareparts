"""Retain return receipt source evidence and fractional quantity review markers."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e3a7b9c2d4f6"
down_revision = "d2f8b4e6c9a1"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Also repair installations that already ran the original d2 revision:
    # quoted Python strings there produced literal quote characters in defaults.
    for column, value in (
        ("source", "rkd_import"),
        ("line_status", "active"),
        ("created_by", "rkd_import"),
    ):
        op.alter_column(
            "maintenance_rkd_return_line", column, server_default=sa.text(f"'{value}'")
        )
    op.add_column(
        "maintenance_rkd_return_line", sa.Column("source_payload", postgresql.JSONB())
    )
    op.add_column(
        "maintenance_rkd_return_line",
        sa.Column("receipt_kind", sa.String(16), nullable=False, server_default="part"),
    )
    op.add_column(
        "maintenance_rkd_return_line",
        sa.Column(
            "review_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE maintenance_rkd_return_line IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM maintenance_rkd_return_line
                 WHERE source_payload IS NOT NULL OR review_required OR receipt_kind <> 'part') THEN
        RAISE EXCEPTION 'downgrade refused: return receipt import evidence exists; export and review first';
      END IF;
    END $$""")
    op.drop_column("maintenance_rkd_return_line", "review_required")
    op.drop_column("maintenance_rkd_return_line", "receipt_kind")
    op.drop_column("maintenance_rkd_return_line", "source_payload")
