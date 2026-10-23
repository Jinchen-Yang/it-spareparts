"""Index bounded owner-first job history without changing historical actor evidence."""

from alembic import op
import sqlalchemy as sa

revision = "f8b1d4e7a0c3"
down_revision = "e7a0c3f6b9d2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_pn_catalog_job_owner_created",
        "pn_catalog_job",
        [sa.text("(context['actor'] ->> 'principal_id')"), "created_at", "id"],
    )


def downgrade():
    op.drop_index("ix_pn_catalog_job_owner_created", table_name="pn_catalog_job")
