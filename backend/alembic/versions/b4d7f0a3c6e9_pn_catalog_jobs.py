"""Durable fenced PN work and immutable logical-job inputs.

Revision ID: b4d7f0a3c6e9
Revises: a3c6e9f2b5d8
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "b4d7f0a3c6e9"
down_revision = "a3c6e9f2b5d8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pn_catalog_job",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("queue", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(256), nullable=False),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id")),
        sa.Column("resource_generation", sa.BigInteger(), nullable=False),
        sa.Column("input_revision", sa.BigInteger(), nullable=False),
        sa.Column("deduplication_key", sa.String(256), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("context", pg.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_in_cycle", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_token", pg.UUID(as_uuid=True)),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("result", pg.JSONB()),
        sa.Column("execution_usage", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("kind", "deduplication_key", name="uq_pn_catalog_job_dedupe"),
        sa.CheckConstraint("status IN ('pending','running','succeeded','failed','cancelled','stale')", name="ck_pn_catalog_job_status"),
        sa.CheckConstraint("revision >= 0 AND resource_generation >= 0 AND input_revision >= 0 AND attempt >= 0 AND attempt_in_cycle >= 0 AND retry_count >= 0 AND lease_epoch >= 0 AND max_attempts BETWEEN 1 AND 20", name="ck_pn_catalog_job_counts"),
        sa.CheckConstraint("(status = 'running' AND lease_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'running' AND lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL)", name="ck_pn_catalog_job_lease"),
    )
    op.create_index("ix_pn_catalog_job_pending", "pn_catalog_job", ["queue", "available_at", "created_at"], postgresql_where=sa.text("status = 'pending'"))
    op.create_index("ix_pn_catalog_job_expired", "pn_catalog_job", ["lease_expires_at"], postgresql_where=sa.text("status = 'running'"))
    op.create_index("ix_pn_catalog_job_resource", "pn_catalog_job", ["resource_type", "resource_id", "created_at"])
    op.create_table(
        "pn_catalog_job_event",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("job_id", pg.UUID(as_uuid=True), sa.ForeignKey("pn_catalog_job.id"), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("code", sa.String(128)),
        sa.Column("context", pg.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_pn_catalog_job_event_job", "pn_catalog_job_event", ["job_id", "id"])
    op.execute("""CREATE TRIGGER pn_catalog_job_event_no_mutation BEFORE UPDATE OR DELETE
      ON pn_catalog_job_event FOR EACH ROW EXECUTE FUNCTION pn_catalog_audit_immutable()""")
    op.execute("""CREATE FUNCTION pn_catalog_job_input_immutable() RETURNS trigger AS $$
      BEGIN
        IF ROW(OLD.id, OLD.kind, OLD.queue, OLD.resource_type, OLD.resource_id, OLD.part_id,
          OLD.resource_generation, OLD.input_revision, OLD.deduplication_key, OLD.payload,
          OLD.payload_hash, OLD.context, OLD.max_attempts, OLD.created_at)
          IS DISTINCT FROM ROW(NEW.id, NEW.kind, NEW.queue, NEW.resource_type, NEW.resource_id, NEW.part_id,
          NEW.resource_generation, NEW.input_revision, NEW.deduplication_key, NEW.payload,
          NEW.payload_hash, NEW.context, NEW.max_attempts, NEW.created_at) THEN
          RAISE EXCEPTION 'pn_catalog job inputs are immutable' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
      END;
      $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER pn_catalog_job_no_input_mutation BEFORE UPDATE
      ON pn_catalog_job FOR EACH ROW EXECUTE FUNCTION pn_catalog_job_input_immutable()""")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pn_catalog_job)")):
        raise RuntimeError("PN jobs contain durable work; reconcile before downgrade")
    op.drop_table("pn_catalog_job_event")
    op.drop_table("pn_catalog_job")
    op.execute("DROP FUNCTION pn_catalog_job_input_immutable()")
