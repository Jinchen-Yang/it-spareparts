"""Exact external references remain stable across renames and canonical merges.

Revision ID: h0d3f6a9b2e5
Revises: g9c2e5f8a1d4
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "h0d3f6a9b2e5"
down_revision = "g9c2e5f8a1d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("pn_catalog_external_reference",
        sa.Column("namespace", sa.String(64), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), nullable=False),
        sa.Column("identity_revision", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("decision_ref", sa.String(256), nullable=False),
        sa.Column("audit_id", sa.BigInteger(), sa.ForeignKey("pn_catalog_audit.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("version >= 1 AND identity_revision >= 0", name="ck_pn_reference_versions"))
    op.create_index("ix_pn_catalog_external_reference_part_id", "pn_catalog_external_reference", ["part_id"])
    op.execute("""CREATE FUNCTION pn_reference_immutable() RETURNS trigger AS $$
      BEGIN RAISE EXCEPTION 'external PN reference is immutable; reconcile dependent facts explicitly' USING ERRCODE='55000'; END;
      $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER pn_reference_no_mutation BEFORE UPDATE OR DELETE
      ON pn_catalog_external_reference FOR EACH ROW EXECUTE FUNCTION pn_reference_immutable()""")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM pn_catalog_external_reference)")).scalar_one():
        raise RuntimeError("External PN bindings require explicit reconciliation before downgrade")
    op.drop_table("pn_catalog_external_reference")
    op.execute("DROP FUNCTION pn_reference_immutable()")
