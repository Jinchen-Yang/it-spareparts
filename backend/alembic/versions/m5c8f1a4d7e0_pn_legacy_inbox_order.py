"""Preserve valid historical capture indices without increasing new-upload limits."""

from alembic import op
import sqlalchemy as sa

revision = "m5c8f1a4d7e0"
down_revision = "l4b7e0f3a6c9"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_pn_media_inbox_capture_order", "pn_media_inbox", type_="check"
    )
    op.create_check_constraint(
        "ck_pn_media_inbox_capture_order",
        "pn_media_inbox",
        "client_order IS NULL OR (group_id IS NOT NULL AND client_order BETWEEN 0 AND 2147483647)",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pn_media_inbox WHERE client_order > 1999)"
        )
    ):
        raise RuntimeError(
            "Historical capture order must be reconciled before downgrade"
        )
    op.drop_constraint(
        "ck_pn_media_inbox_capture_order", "pn_media_inbox", type_="check"
    )
    op.create_check_constraint(
        "ck_pn_media_inbox_capture_order",
        "pn_media_inbox",
        "client_order IS NULL OR (group_id IS NOT NULL AND client_order BETWEEN 0 AND 1999)",
    )
