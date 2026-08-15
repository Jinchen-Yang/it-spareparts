"""maintenance AI mapping proposal staging (C3)

Revision ID: f1a2b3c4d5e6
Revises: e9f2d4b7a1c6
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f1a2b3c4d5e6"
down_revision = "e9f2d4b7a1c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_ai_mapping_proposal",
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("doc_type", sa.String(length=24), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("header_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sample_rows", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="'pending'", nullable=False
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("accepted_batch_id", sa.String(length=36), nullable=True),
        sa.Column("accepted_by", sa.String(length=64), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "doc_type IN ('ckd_shipment', 'rkd_inbound', 'return_order',"
            " 'bxd_expense', 'ledger')",
            name="ck_maintenance_ai_proposal_doc_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_maintenance_ai_proposal_status",
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (accepted_at IS NOT NULL"
            " AND accepted_by IS NOT NULL)",
            name="ck_maintenance_ai_proposal_accepted",
        ),
        sa.PrimaryKeyConstraint("proposal_id"),
    )
    op.create_index(
        "ix_maintenance_ai_proposal_hash", "maintenance_ai_mapping_proposal", ["file_hash"]
    )
    op.create_index(
        "ix_maintenance_ai_proposal_type_created",
        "maintenance_ai_mapping_proposal",
        ["doc_type", "created_at"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $guard$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_ai_mapping_proposal)
          THEN
            RAISE EXCEPTION
              'f1a2b3c4d5e6 downgrade blocked: AI mapping proposals exist';
          END IF;
        END
        $guard$;
        """
    )
    op.drop_index(
        "ix_maintenance_ai_proposal_type_created",
        table_name="maintenance_ai_mapping_proposal",
    )
    op.drop_index(
        "ix_maintenance_ai_proposal_hash", table_name="maintenance_ai_mapping_proposal"
    )
    op.drop_table("maintenance_ai_mapping_proposal")
