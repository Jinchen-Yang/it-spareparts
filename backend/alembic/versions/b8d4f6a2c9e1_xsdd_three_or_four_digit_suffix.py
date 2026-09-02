"""allow three- or four-digit XSDD sequence suffixes

Revision ID: b8d4f6a2c9e1
Revises: a7c2e9f4b1d6
"""

from collections.abc import Sequence

from alembic import op


revision: str = "b8d4f6a2c9e1"
down_revision: str | None = "a7c2e9f4b1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_normalizer(sequence_pattern: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION maintenance_normalize_xsdd(raw_value text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        RETURN CASE
            WHEN regexp_replace(
                upper(regexp_replace(btrim(coalesce(raw_value, '')), '\\s+', '', 'g')),
                '^XSDD-',
                ''
            ) ~ '^[0-9]{{8}}-{sequence_pattern}$'
            THEN regexp_replace(
                upper(regexp_replace(btrim(coalesce(raw_value, '')), '\\s+', '', 'g')),
                '^XSDD-',
                ''
            )
            ELSE ''
        END
        """
    )


def upgrade() -> None:
    _replace_normalizer("[0-9]{3,4}")


def downgrade() -> None:
    _replace_normalizer("[0-9]{4}")
