"""agent artifact delivery v2 metadata

Revision ID: ad8f6c2e1b47
Revises: c4e8a1d7f2b6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "ad8f6c2e1b47"
down_revision: str | None = "c4e8a1d7f2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_artifact",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("owner_sub", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column(
            "source_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "access_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "extra_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('prepared', 'validating', 'ready', 'failed', 'expired')",
            name="ck_agent_artifact_status",
        ),
        sa.CheckConstraint(
            "kind IN ('upload', 'generated')",
            name="ck_agent_artifact_kind",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('low', 'medium', 'high', 'critical')",
            name="ck_agent_artifact_sensitivity",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_agent_artifact_size"),
        sa.CheckConstraint("char_length(sha256) = 64", name="ck_agent_artifact_sha256"),
        sa.CheckConstraint(
            "char_length(btrim(owner_sub)) > 0",
            name="ck_agent_artifact_owner",
        ),
        sa.CheckConstraint(
            "char_length(btrim(filename)) > 0",
            name="ck_agent_artifact_filename",
        ),
        sa.CheckConstraint(
            "char_length(btrim(storage_key)) > 0",
            name="ck_agent_artifact_storage_key",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_agent_artifact_expiry",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_agent_artifact_storage_key"),
    )
    op.create_index(
        "ix_agent_artifact_owner_created",
        "agent_artifact",
        ["owner_sub", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_artifact_status_expiry",
        "agent_artifact",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM agent_artifact LIMIT 1) THEN
            RAISE EXCEPTION
              'ad8f6c2e1b47 downgrade blocked: agent_artifact is not empty; disable v2 routes and use a forward deploy';
          END IF;
        END
        $$;
        """
    )
    op.drop_index("ix_agent_artifact_status_expiry", table_name="agent_artifact")
    op.drop_index("ix_agent_artifact_owner_created", table_name="agent_artifact")
    op.drop_table("agent_artifact")
