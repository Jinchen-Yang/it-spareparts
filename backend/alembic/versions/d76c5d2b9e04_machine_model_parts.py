"""D-0076 机型备件池及实名维护回执。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d76c5d2b9e04"
down_revision = "ab73d15c804e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "machine_model",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand", sa.String(128), nullable=False),
        sa.Column("name", sa.String(192), nullable=False),
    )
    op.create_table(
        "machine_model_name",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "model_id", sa.Integer(), sa.ForeignKey("machine_model.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("brand_key", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("name", sa.String(192), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("brand_key", "name_key", name="uq_machine_model_name"),
    )
    op.create_index("ix_machine_model_name_model_id", "machine_model_name", ["model_id"])
    op.create_table(
        "machine_model_member",
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("machine_model.id"), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), primary_key=True),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("condition", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("raw_pns", JSONB(), nullable=False),
    )
    op.create_index("ix_machine_model_member_part", "machine_model_member", ["part_id"])
    op.create_table(
        "machine_model_pending",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("machine_model.id"), nullable=False),
        sa.Column("raw_key", sa.Text(), nullable=False),
        sa.Column("raw_pn", sa.String(256), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("condition", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("candidate_ids", JSONB(), nullable=False),
        sa.UniqueConstraint("model_id", "raw_key", name="uq_machine_model_pending_raw"),
        sa.CheckConstraint("reason in ('unknown','ambiguous')", name="ck_machine_model_pending_reason"),
    )
    op.create_table(
        "machine_model_command",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("command", JSONB(), nullable=False),
        sa.Column("before", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_machine_model_command_revision", "machine_model_command", ["revision"])


def downgrade() -> None:
    for table in (
        "machine_model_command",
        "machine_model_pending",
        "machine_model_member",
        "machine_model_name",
        "machine_model",
    ):
        op.drop_table(table)
