"""新增通用待办引擎和只追加主体时间线。

Revision ID: e8a2c4f6b1d3
Revises: d6f8a0b2c4e6
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8a2c4f6b1d3"
down_revision: str | None = "d6f8a0b2c4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "work_item",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("pool_key", sa.String(32), nullable=False),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("sys_user.id"), nullable=True),
        sa.Column("claimed_by", sa.Integer(), sa.ForeignKey("sys_user.id"), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("paused", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("sys_user.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'done', 'declined', 'cancelled')",
            name="ck_work_item_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_work_item_version"),
    )
    op.create_index("ix_work_item_assignee_status", "work_item", ["assignee_id", "status"])
    op.create_index(
        "ix_work_item_pool_status", "work_item", ["pool_key", "status"],
        postgresql_where=sa.text("assignee_id IS NULL"),
    )
    op.create_index("ix_work_item_subject", "work_item", ["subject_type", "subject_id"])
    op.create_index(
        "uq_work_item_open_subject_assignee", "work_item",
        ["subject_type", "subject_id", "assignee_id"], unique=True,
        postgresql_where=sa.text("status IN ('pending', 'in_progress')"),
    )
    op.create_table(
        "subject_event",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("work_item_id", sa.BigInteger(), sa.ForeignKey("work_item.id"), nullable=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("actor", sa.Integer(), sa.ForeignKey("sys_user.id"), nullable=True),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_subject_event_subject_id", "subject_event", ["subject_type", "subject_id", "id"],
    )
    op.execute("""
        CREATE FUNCTION reject_subject_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'subject_event is append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_subject_event_append_only
        BEFORE UPDATE OR DELETE ON subject_event
        FOR EACH ROW EXECUTE FUNCTION reject_subject_event_mutation()
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TRIGGER trg_subject_event_append_only ON subject_event")
    op.execute("DROP FUNCTION reject_subject_event_mutation()")
    op.drop_table("subject_event")
    op.drop_table("work_item")
