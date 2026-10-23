"""Shared PN revisions, provenance, append-only audit and transactional receipts.

Revision ID: e1a4c7d9f2b6
Revises: d8f6a2c4b9e7
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e1a4c7d9f2b6"
down_revision = "d8f6a2c4b9e7"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("sys_import_job", "sys_import_batch"):
        op.add_column(table, sa.Column("actor_user_id", sa.Integer(), nullable=True))
        op.create_check_constraint(f"ck_{table}_actor_user_id", table, "actor_user_id IS NULL OR actor_user_id > 0")
    op.create_table(
        "pn_catalog_state",
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), primary_key=True),
        sa.Column("identity_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("profile_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("availability_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("availability", sa.String(16), nullable=False, server_default="active"),
        sa.Column("manufacturer", sa.String(256)),
        sa.Column("field_versions", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("deleted_specs", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("field_provenance", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("availability IN ('active','inactive')", name="ck_pn_catalog_availability"),
        sa.CheckConstraint("identity_revision >= 0 AND profile_revision >= 0 AND availability_revision >= 0",
                           name="ck_pn_catalog_revisions"),
    )
    # A baseline is not a fabricated historical edit or human confirmation.
    op.execute("INSERT INTO pn_catalog_state (part_id) SELECT id FROM dim_part")
    op.create_table(
        "pn_catalog_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("principal_id", sa.String(256), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", postgresql.JSONB(), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("authorization", postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("before_revision", sa.BigInteger(), nullable=False),
        sa.Column("after_revision", sa.BigInteger(), nullable=False),
        sa.Column("changes", postgresql.JSONB(), nullable=False),
        sa.Column("revisions", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_pn_catalog_audit_part_id", "pn_catalog_audit", ["part_id", "id"])
    op.create_table(
        "pn_catalog_receipt",
        sa.Column("principal_id", sa.String(256), primary_key=True),
        sa.Column("client_id", sa.String(128), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("applied_revision", sa.BigInteger(), nullable=False),
        sa.Column("audit_id", sa.BigInteger(), sa.ForeignKey("pn_catalog_audit.id"), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Append-only applies to business DML; isolated test TRUNCATE remains possible.
    op.execute("""CREATE FUNCTION pn_catalog_audit_immutable() RETURNS trigger AS $$
      BEGIN RAISE EXCEPTION 'pn_catalog audit is append-only' USING ERRCODE = '55000'; END;
      $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER pn_catalog_audit_no_mutation BEFORE UPDATE OR DELETE
      ON pn_catalog_audit FOR EACH ROW EXECUTE FUNCTION pn_catalog_audit_immutable()""")


def downgrade():
    # Do not silently throw away adopted data, provenance, deletion protection or receipts.
    bind = op.get_bind()
    populated = bind.execute(sa.text("""SELECT
      EXISTS (SELECT 1 FROM sys_import_job WHERE actor_user_id IS NOT NULL) OR
      EXISTS (SELECT 1 FROM sys_import_batch WHERE actor_user_id IS NOT NULL) OR
      EXISTS (SELECT 1 FROM pn_catalog_audit) OR
      EXISTS (SELECT 1 FROM pn_catalog_receipt) OR
      EXISTS (SELECT 1 FROM pn_catalog_state WHERE profile_revision <> 0 OR
        identity_revision <> 0 OR availability_revision <> 0 OR manufacturer IS NOT NULL OR
        availability <> 'active' OR deleted_specs <> '[]'::jsonb OR
        field_versions <> '{}'::jsonb OR field_provenance <> '{}'::jsonb)""")).scalar_one()
    if populated:
        raise RuntimeError("PN catalog contains adopted data; export and explicitly reconcile before downgrade")
    op.drop_table("pn_catalog_receipt")
    op.drop_table("pn_catalog_audit")
    op.execute("DROP FUNCTION pn_catalog_audit_immutable()")
    op.drop_table("pn_catalog_state")
    for table in ("sys_import_job", "sys_import_batch"):
        op.drop_constraint(f"ck_{table}_actor_user_id", table, type_="check")
        op.drop_column(table, "actor_user_id")
