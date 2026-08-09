"""merge the three committed maintenance integration heads

Revision ID: b1c4e7a9d2f6
Revises: a6d1e9c3b7f2, e6f1a9c3b7d2, f5a7c9e1b3d4
"""

from collections.abc import Sequence


revision: str = "b1c4e7a9d2f6"
down_revision: tuple[str, str, str] = (
    "a6d1e9c3b7f2",
    "e6f1a9c3b7d2",
    "f5a7c9e1b3d4",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
