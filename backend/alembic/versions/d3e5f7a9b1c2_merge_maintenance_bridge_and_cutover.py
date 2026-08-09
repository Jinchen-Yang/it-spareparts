"""merge maintenance bridge and cutover control heads

Revision ID: d3e5f7a9b1c2
Revises: b2c4e6f8a1d3, c2f7a9d4e6b1
"""

from collections.abc import Sequence


revision: str = "d3e5f7a9b1c2"
down_revision: tuple[str, str] = ("b2c4e6f8a1d3", "c2f7a9d4e6b1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join two independently additive maintenance histories."""


def downgrade() -> None:
    """Split the heads; each parent owns its own downgrade safety checks."""
