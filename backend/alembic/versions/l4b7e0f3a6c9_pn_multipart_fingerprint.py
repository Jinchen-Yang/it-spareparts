"""Keep verified legacy multipart object identity when resuming through the authority."""

from alembic import op
import sqlalchemy as sa

revision = "l4b7e0f3a6c9"
down_revision = "k3a6d9e2f5b8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "pn_media_multipart", sa.Column("storage_fingerprint", sa.String(128))
    )
    op.create_check_constraint(
        "ck_pn_multipart_legacy_fingerprint",
        "pn_media_multipart",
        "storage_fingerprint IS NULL OR storage_fingerprint ~ '^[a-f0-9]{64}$'",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM pn_media_multipart WHERE storage_fingerprint IS NOT NULL)"
        )
    ):
        raise RuntimeError("Cannot discard adopted legacy multipart identity")
    op.drop_constraint("ck_pn_multipart_legacy_fingerprint", "pn_media_multipart")
    op.drop_column("pn_media_multipart", "storage_fingerprint")
