"""Keep state conversion and recovery evidence separate from frozen source archives."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "i1e4a7c0d3f6"
down_revision = "h0d3f6a9b2e5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pn_catalog_migration_snapshot",
        sa.Column("namespace", sa.String(64), primary_key=True),
        sa.Column("snapshot_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_schema", sa.String(128), nullable=False),
        sa.Column("registration_sha256", sa.String(64), nullable=False),
        sa.Column("inventory", pg.JSONB(), nullable=False),
        sa.Column("fence_evidence", pg.JSONB(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "audit_id",
            sa.BigInteger(),
            sa.ForeignKey("pn_catalog_audit.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("revision >= 1", name="ck_pn_migration_snapshot_revision"),
    )
    op.create_table(
        "pn_catalog_migration_application",
        sa.Column(
            "source_record_id",
            sa.BigInteger(),
            sa.ForeignKey("pn_catalog_legacy_record.id"),
            primary_key=True,
        ),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("snapshot_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128)),
        sa.Column("target_ref", pg.JSONB()),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "audit_id",
            sa.BigInteger(),
            sa.ForeignKey("pn_catalog_audit.id"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["namespace", "snapshot_id"],
            [
                "pn_catalog_migration_snapshot.namespace",
                "pn_catalog_migration_snapshot.snapshot_id",
            ],
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_pn_migration_application_revision"
        ),
        sa.CheckConstraint(
            "state IN ('applied','historical_terminal','recovery_required','dependency_blocked','conflict')",
            name="ck_pn_migration_application_state",
        ),
    )
    op.create_index(
        "ix_pn_migration_application_state",
        "pn_catalog_migration_application",
        ["namespace", "snapshot_id", "state", "source_record_id"],
    )
    op.create_table(
        "pn_catalog_migration_recovery",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_record_id",
            sa.BigInteger(),
            sa.ForeignKey("pn_catalog_migration_application.source_record_id"),
            nullable=False,
        ),
        sa.Column("expected_revision", sa.BigInteger(), nullable=False),
        sa.Column("decision_kind", sa.String(64), nullable=False),
        sa.Column("evidence", pg.JSONB(), nullable=False),
        sa.Column(
            "audit_id",
            sa.BigInteger(),
            sa.ForeignKey("pn_catalog_audit.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "expected_revision >= 1", name="ck_pn_migration_recovery_revision"
        ),
    )
    op.execute("""CREATE FUNCTION pn_migration_state_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'migration evidence is immutable'; END $$""")
    for table in ("pn_catalog_migration_snapshot", "pn_catalog_migration_recovery"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION pn_migration_state_immutable()"
        )
    op.execute("""CREATE FUNCTION pn_migration_application_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR (OLD.source_record_id, OLD.namespace, OLD.snapshot_id, OLD.source_sha256)
             IS DISTINCT FROM (NEW.source_record_id, NEW.namespace, NEW.snapshot_id, NEW.source_sha256)
             OR NEW.revision <= OLD.revision THEN
            RAISE EXCEPTION 'migration application requires immutable source and advancing revision';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER pn_migration_application_guard BEFORE UPDATE OR DELETE ON pn_catalog_migration_application FOR EACH ROW EXECUTE FUNCTION pn_migration_application_guard()"
    )


def downgrade():
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM pn_catalog_migration_snapshot")
    )
    if count:
        raise RuntimeError("Cannot downgrade adopted migration state evidence")
    for table in (
        "pn_catalog_migration_recovery",
        "pn_catalog_migration_application",
        "pn_catalog_migration_snapshot",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION pn_migration_application_guard()")
    op.execute("DROP FUNCTION pn_migration_state_immutable()")
