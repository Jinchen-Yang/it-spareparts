"""Keep the demand reference separate from the site issue identity."""
from alembic import op
import sqlalchemy as sa

revision = "c9e5a1b7d3f8"
down_revision = "b7d3f9a1c5e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("maintenance_site_issue_line", sa.Column("demand_order_no", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("maintenance_site_issue_line", "demand_order_no")
