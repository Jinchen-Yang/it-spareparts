"""Merge B1 with S1 and add explicit short delegation trust bindings.

No account linkage, grants, or cutover is inferred or backfilled.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d6f9b2e5a8c1"
down_revision = ("c5e8a1d4f7b0", "a7c2e9f4b6d1")
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sys_catalog_subject_binding",
        sa.Column("issuer", sa.String(256), primary_key=True),
        sa.Column("subject", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("sys_user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("updated_by", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("version > 0", name="ck_catalog_subject_version"),
    )
    op.create_table(
        "sys_catalog_proof_nonce",
        sa.Column("issuer", sa.String(256), primary_key=True),
        sa.Column("nonce", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_sys_catalog_proof_nonce_expires_at",
        "sys_catalog_proof_nonce",
        ["expires_at"],
    )
    op.execute("""CREATE FUNCTION catalog_subject_monotonic() RETURNS trigger AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'disable identity bindings instead of deleting'; END IF;
        IF NEW.issuer <> OLD.issuer OR NEW.subject <> OLD.subject THEN
          RAISE EXCEPTION 'binding identity is immutable';
        END IF;
        IF (NEW.user_id,NEW.active) IS DISTINCT FROM (OLD.user_id,OLD.active)
           AND NEW.version <= OLD.version THEN
          RAISE EXCEPTION 'binding version must advance';
        END IF;
        IF NEW.version < OLD.version THEN RAISE EXCEPTION 'binding version cannot decrease'; END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER catalog_subject_monotonic BEFORE UPDATE OR DELETE ON sys_catalog_subject_binding
                  FOR EACH ROW EXECUTE FUNCTION catalog_subject_monotonic()""")


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM sys_catalog_subject_binding)")
    ):
        raise RuntimeError("Cannot remove adopted catalog identity bindings")
    op.drop_table("sys_catalog_proof_nonce")
    op.drop_table("sys_catalog_subject_binding")
    op.execute("DROP FUNCTION catalog_subject_monotonic()")
