"""Keep capture order independent from upload completion and later manual ordering."""
from alembic import op
import sqlalchemy as sa

revision = "g9c2e5f8a1d4"
down_revision = "f8b1d4e7a0c3"
branch_labels = None
depends_on = None


def upgrade():
    # NULL preserves unknown historical capture order; dense position is not evidence.
    op.add_column("pn_media_inbox", sa.Column("client_order", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_pn_media_inbox_capture_order", "pn_media_inbox", ["group_id", "client_order"])
    op.create_check_constraint("ck_pn_media_inbox_capture_order", "pn_media_inbox",
        "client_order IS NULL OR (group_id IS NOT NULL AND client_order BETWEEN 0 AND 1999)")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM pn_media_inbox WHERE client_order IS NOT NULL)")).scalar_one():
        raise RuntimeError("Inbox capture order requires explicit reconciliation before downgrade")
    op.drop_constraint("ck_pn_media_inbox_capture_order", "pn_media_inbox", type_="check")
    op.drop_constraint("uq_pn_media_inbox_capture_order", "pn_media_inbox", type_="unique")
    op.drop_column("pn_media_inbox", "client_order")
