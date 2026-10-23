"""Freeze inbox assignment intent independently of later gallery edits.

Revision ID: k3a6d9e2f5b8
Revises: j2f5a8d1e4b7
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "k3a6d9e2f5b8"
down_revision = "j2f5a8d1e4b7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pn_media_inbox", sa.Column("assignment_intent", postgresql.JSONB(), nullable=True))
    op.create_check_constraint("ck_pn_inbox_assignment_intent", "pn_media_inbox",
        "assignment_intent IS NULL OR jsonb_typeof(assignment_intent) = 'object'")
    # Do not reconstruct old intent from mutable gallery state. Original command
    # receipts still recover outcomes; a fresh request requires explicit recovery.


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM pn_media_inbox WHERE assignment_intent IS NOT NULL)")).scalar_one():
        raise RuntimeError("PN inbox assignment evidence requires explicit recovery before downgrade")
    op.drop_constraint("ck_pn_inbox_assignment_intent", "pn_media_inbox", type_="check")
    op.drop_column("pn_media_inbox", "assignment_intent")
