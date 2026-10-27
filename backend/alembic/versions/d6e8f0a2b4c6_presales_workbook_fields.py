"""售前工作簿授权范围、期望交期和草稿重复报价选择。

Revision ID: d6e8f0a2b4c6
Revises: c4e6f8a0b2d5
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d6e8f0a2b4c6"
down_revision = "c4e6f8a0b2d5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("presales_workbook_export",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("schema_name", sa.String(64), nullable=False),
        sa.Column("export_id", sa.String(128), nullable=False, unique=True),
        sa.Column("actor", sa.String(64), sa.ForeignKey("sys_user.username"), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_presales_workbook_export_actor_schema", "presales_workbook_export", ["actor", "schema_name"])
    op.add_column("presales_inquiry_line", sa.Column("expected_delivery", sa.Date(), nullable=True))
    op.add_column("presales_offer", sa.Column("workbook_choice", postgresql.JSONB(),
                                            server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("presales_line_response", sa.Column("is_draft", sa.Boolean(),
                                                     server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column("presales_line_response", "is_draft")
    op.drop_column("presales_offer", "workbook_choice")
    op.drop_column("presales_inquiry_line", "expected_delivery")
    op.drop_index("ix_presales_workbook_export_actor_schema", table_name="presales_workbook_export")
    op.drop_table("presales_workbook_export")
