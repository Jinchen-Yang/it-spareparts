"""Lossless frozen migration manifests, including unresolved legacy origins."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c5e8a1d4f7b0"
down_revision = "b4d7f0a3c6e9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("pn_catalog_migration_batch",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("source_schema", sa.String(128), nullable=False),
        sa.Column("writer_fence_ref", sa.String(256), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="archived"),
        sa.Column("principal_id", sa.String(256), nullable=False),
        sa.Column("audit_id", sa.BigInteger(), sa.ForeignKey("pn_catalog_audit.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('archived','materialized')", name="ck_pn_migration_batch_status"))
    op.create_table("pn_catalog_legacy_record",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(256), nullable=False),
        sa.Column("source_pn_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_revision", sa.String(256)),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("pn_catalog_migration_batch.id"), nullable=False),
        sa.Column("target_part_id", sa.Integer(), sa.ForeignKey("dim_part.id")),
        sa.Column("mapping", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("materialized_ref", postgresql.JSONB()),
        sa.UniqueConstraint("namespace", "kind", "source_id", name="uq_pn_legacy_source_record"),
        sa.CheckConstraint("state IN ('pending','materialized','unresolved','conflict','historical')", name="ck_pn_legacy_record_state"))
    op.create_index("ix_pn_legacy_discovery", "pn_catalog_legacy_record", ["namespace", "state", "kind", "id"])
    op.execute("""CREATE FUNCTION pn_legacy_evidence_immutable() RETURNS trigger AS $$
      BEGIN
        IF TG_OP = 'DELETE' OR
           ROW(NEW.namespace, NEW.kind, NEW.source_id, NEW.source_pn_id, NEW.source_revision,
               NEW.payload_sha256, NEW.payload, NEW.batch_id, NEW.mapping, NEW.target_part_id)
           IS DISTINCT FROM
           ROW(OLD.namespace, OLD.kind, OLD.source_id, OLD.source_pn_id, OLD.source_revision,
               OLD.payload_sha256, OLD.payload, OLD.batch_id, OLD.mapping, OLD.target_part_id)
        THEN RAISE EXCEPTION 'Legacy source evidence is immutable' USING ERRCODE = '55000'; END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER pn_legacy_evidence_no_mutation BEFORE UPDATE OR DELETE
      ON pn_catalog_legacy_record FOR EACH ROW EXECUTE FUNCTION pn_legacy_evidence_immutable()""")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM pn_catalog_migration_batch)")).scalar_one():
        raise RuntimeError("Migration evidence exists; reconcile explicitly before downgrade")
    op.drop_table("pn_catalog_legacy_record")
    op.execute("DROP FUNCTION pn_legacy_evidence_immutable()")
    op.drop_table("pn_catalog_migration_batch")
