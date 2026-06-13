"""merge chat-persistence(85424) + rename-deferrable(f7c2) heads

Revision ID: 2de8eaf62e48
Revises: 85424bb7cc56, f7c2a9b4e1d8
Create Date: 2026-06-13 20:05:33.779149

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2de8eaf62e48'
down_revision: Union[str, Sequence[str], None] = ('85424bb7cc56', 'f7c2a9b4e1d8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
