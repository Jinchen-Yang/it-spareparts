"""Independent PN condition content and immutable source candidates.

Revision ID: f2b5d8e1a4c7
Revises: e1a4c7d9f2b6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f2b5d8e1a4c7"
down_revision = "e1a4c7d9f2b6"
branch_labels = None
depends_on = None


def upgrade():
    # Uploads and unassigned inbox items have an audited owner before they have a PN.
    for table in ("pn_catalog_audit", "pn_catalog_receipt"):
        op.alter_column(table, "part_id", existing_type=sa.Integer(), nullable=True)
    op.create_table("pn_catalog_content",
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), primary_key=True),
        sa.Column("grade", sa.String(8), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(200)), sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("field_versions", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("protected_fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("field_provenance", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("legacy_source_namespace", sa.String(128)),
        sa.Column("legacy_source_record_id", sa.String(256)),
        sa.Column("legacy_source_revision", sa.String(128)),
        sa.CheckConstraint("grade IN ('good','bad')", name="ck_pn_content_grade"),
        sa.CheckConstraint("revision >= 0", name="ck_pn_content_revision"),
        sa.CheckConstraint("status IN ('pending','ai_filled','confirmed','needs_review')", name="ck_pn_content_status"),
        sa.UniqueConstraint("legacy_source_namespace", "legacy_source_record_id", name="uq_pn_content_legacy_source"))
    op.create_table("pn_catalog_candidate",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), nullable=False),
        sa.Column("resource", sa.String(16), nullable=False), sa.Column("grade", sa.String(8)),
        sa.Column("identity_revision", sa.BigInteger(), nullable=False),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("adopted_fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("decisions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("resource IN ('profile','content')", name="ck_pn_candidate_resource"),
        sa.CheckConstraint("(resource = 'profile' AND grade IS NULL) OR (resource = 'content' AND grade IN ('good','bad'))", name="ck_pn_candidate_grade"),
        sa.CheckConstraint("revision >= 1 AND identity_revision >= 0 AND base_revision >= 0", name="ck_pn_candidate_revision"),
        sa.CheckConstraint("status IN ('pending','partially_adopted','adopted','rejected')", name="ck_pn_candidate_status"))
    op.create_index("ix_pn_candidate_part_created", "pn_catalog_candidate", ["part_id", "created_at", "id"])
    op.execute("""CREATE FUNCTION pn_catalog_candidate_evidence_immutable() RETURNS trigger AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN
          RAISE EXCEPTION 'PN candidate evidence cannot be deleted' USING ERRCODE = '55000';
        END IF;
        IF (to_jsonb(OLD) - ARRAY['revision','status','adopted_fields','decisions']) IS DISTINCT FROM
           (to_jsonb(NEW) - ARRAY['revision','status','adopted_fields','decisions']) THEN
          RAISE EXCEPTION 'PN candidate source evidence is immutable' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER pn_catalog_candidate_no_evidence_mutation BEFORE UPDATE OR DELETE
      ON pn_catalog_candidate FOR EACH ROW EXECUTE FUNCTION pn_catalog_candidate_evidence_immutable()""")


def downgrade():
    populated = op.get_bind().execute(sa.text("""SELECT
        EXISTS (SELECT 1 FROM pn_catalog_content) OR EXISTS (SELECT 1 FROM pn_catalog_candidate) OR
        EXISTS (SELECT 1 FROM pn_catalog_audit WHERE part_id IS NULL) OR
        EXISTS (SELECT 1 FROM pn_catalog_receipt WHERE part_id IS NULL)""")).scalar_one()
    if populated:
        raise RuntimeError("PN content or unassigned resource evidence exists; export and reconcile before downgrade")
    op.drop_table("pn_catalog_candidate")
    op.execute("DROP FUNCTION pn_catalog_candidate_evidence_immutable()")
    op.drop_table("pn_catalog_content")
    for table in ("pn_catalog_receipt", "pn_catalog_audit"):
        op.alter_column(table, "part_id", existing_type=sa.Integer(), nullable=False)
