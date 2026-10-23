"""Preserve custom JSON attributes and independent profile fill state."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "j2f5a8d1e4b7"
down_revision = "i1e4a7c0d3f6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pn_catalog_state", sa.Column("fill_status", sa.String(32)))
    op.add_column("pn_catalog_state", sa.Column("fill_status_revision", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_pn_fill_status", "pn_catalog_state",
        "fill_status IS NULL OR fill_status IN ('pending','ai_filled','confirmed','needs_review')")
    op.create_check_constraint("ck_pn_fill_status_revision", "pn_catalog_state", "fill_status_revision >= 0")
    op.create_table("pn_catalog_attribute_state",
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("revision >= 0", name="ck_pn_attribute_revision"))
    op.create_table("pn_catalog_attribute",
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("pn_catalog_attribute_state.part_id"), primary_key=True),
        sa.Column("key", sa.String(256), primary_key=True),
        sa.Column("value", pg.JSONB(none_as_null=False), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("provenance", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("version >= 1", name="ck_pn_attribute_version"))


def downgrade():
    adopted = op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pn_catalog_attribute_state) OR EXISTS(SELECT 1 FROM pn_catalog_state WHERE fill_status_revision > 0)"))
    if adopted:
        raise RuntimeError("PN catalog contains adopted data: attributes or profile fill state")
    op.drop_table("pn_catalog_attribute")
    op.drop_table("pn_catalog_attribute_state")
    op.drop_constraint("ck_pn_fill_status_revision", "pn_catalog_state")
    op.drop_constraint("ck_pn_fill_status", "pn_catalog_state")
    op.drop_column("pn_catalog_state", "fill_status_revision")
    op.drop_column("pn_catalog_state", "fill_status")
