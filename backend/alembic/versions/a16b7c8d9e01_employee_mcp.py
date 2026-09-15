"""Employee MCP durable workflows and audit.

Revision ID: a16b7c8d9e01
Revises: e3a7b9c2d4f6
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "a16b7c8d9e01"
down_revision = "e3a7b9c2d4f6"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "mcp_credential",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", pg.JSONB(), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_mcp_credential_owner", "mcp_credential", ["owner"])
    op.create_table(
        "mcp_record",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("key", sa.String(160)),
        sa.Column("request_hash", sa.String(64)),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "owner", "kind", "key", name="uq_mcp_record_owner_kind_key"
        ),
    )
    op.create_index("ix_mcp_record_owner_kind", "mcp_record", ["owner", "kind"])
    op.create_index("ix_mcp_record_job_state", "mcp_record", ["kind", "state"])
    op.create_table(
        "mcp_audit_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("credential_id", sa.String(36)),
        sa.Column("operation", sa.String(80), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("detail", pg.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_mcp_audit_event_request_id", "mcp_audit_event", ["request_id"])
    op.create_index(
        "ix_mcp_audit_owner_time", "mcp_audit_event", ["owner", "created_at"]
    )
    op.execute("""CREATE FUNCTION mcp_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'MCP audit events are append-only'; END $$""")
    op.execute(
        "CREATE TRIGGER mcp_audit_no_mutation BEFORE UPDATE OR DELETE ON mcp_audit_event FOR EACH ROW EXECUTE FUNCTION mcp_audit_append_only()"
    )


def downgrade():
    # Operational rollback disables MCP. Schema rollback must not erase evidence.
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM mcp_record) OR EXISTS (SELECT 1 FROM mcp_audit_event) OR EXISTS (SELECT 1 FROM mcp_credential)"
        )
    ).scalar():
        raise RuntimeError("MCP data exists; disable MCP instead of discarding records")
    op.drop_table("mcp_audit_event")
    op.execute("DROP FUNCTION mcp_audit_append_only()")
    op.drop_table("mcp_record")
    op.drop_table("mcp_credential")
