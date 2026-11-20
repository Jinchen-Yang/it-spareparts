"""D-0067 维修任务单与返还来源。

Revision ID: b7d43e19af62
Revises: e8b4c1a6d925
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "b7d43e19af62"
down_revision = "e8b4c1a6d925"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE maintenance_repair_order_no_seq")
    op.create_table(
        "maintenance_repair_order",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("creator_id", sa.Integer(), sa.ForeignKey("sys_user.id"), nullable=False),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("sys_user.id")),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("route", sa.String(8), nullable=False),
        sa.Column("returned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("note", sa.String(1000), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("history", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("repair_source_line_id", sa.Integer()),
        sa.CheckConstraint("mode IN ('internal', 'external')", name="ck_repair_order_mode"),
        sa.CheckConstraint("route IN ('pool', 'direct', 'assigned', 'claimed')", name="ck_repair_order_route"),
        sa.CheckConstraint("NOT returned OR assignee_id IS NULL", name="ck_repair_order_returned"),
        sa.CheckConstraint("version >= 1", name="ck_repair_order_version"),
    )
    op.create_table(
        "maintenance_repair_order_line",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("maintenance_repair_order.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("pn", sa.String(128), nullable=False),
        sa.Column("description", sa.String(256), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("fixed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unfixed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("previously_handled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("dispositions", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source_receipt_id", sa.String(36), sa.ForeignKey("maintenance_rkd_return_line.rkd_line_id")),
        sa.Column("source_head_no", sa.String(64)),
        sa.Column("source_version", sa.Integer()),
        sa.CheckConstraint("qty > 0 AND fixed >= 0 AND unfixed >= 0 AND qty - fixed >= unfixed", name="ck_repair_order_line_qty"),
        sa.CheckConstraint("position >= 0", name="ck_repair_order_line_position"),
        sa.CheckConstraint("char_length(btrim(pn)) > 0", name="ck_repair_order_line_pn"),
        sa.CheckConstraint(
            "(source_receipt_id IS NULL AND source_head_no IS NULL AND source_version IS NULL) OR "
            "(source_receipt_id IS NOT NULL AND source_head_no IS NOT NULL "
            "AND source_version IS NOT NULL AND source_version > 0)",
            name="ck_repair_order_line_source",
        ),
        sa.UniqueConstraint("order_id", "position", name="uq_repair_order_line_position", deferrable=True, initially="DEFERRED"),
    )
    op.create_foreign_key(
        "fk_repair_order_previous_line",
        "maintenance_repair_order", "maintenance_repair_order_line", ["repair_source_line_id"], ["id"],
    )
    op.create_index("ix_repair_order_creator", "maintenance_repair_order", ["creator_id", "created_at"])
    op.create_index("ix_repair_order_assignee", "maintenance_repair_order", ["assignee_id", "created_at"])
    op.create_index("ix_repair_order_created_at", "maintenance_repair_order", ["created_at", "id"])
    op.create_index("ix_repair_order_previous_line", "maintenance_repair_order", ["repair_source_line_id"])
    op.create_index("ix_repair_order_line_source", "maintenance_repair_order_line", ["source_receipt_id"])
    op.create_table(
        "maintenance_repair_order_command",
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("sys_user.id"), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("maintenance_repair_order.id"), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("response_json", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE maintenance_repair_order, maintenance_repair_order_line IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM maintenance_repair_order LIMIT 1) THEN
                RAISE EXCEPTION 'refusing repair order downgrade with retained orders';
            END IF;
        END $$
        """
    )
    op.drop_table("maintenance_repair_order_command")
    op.drop_constraint("fk_repair_order_previous_line", "maintenance_repair_order", type_="foreignkey")
    op.drop_table("maintenance_repair_order_line")
    op.drop_table("maintenance_repair_order")
    op.execute("DROP SEQUENCE maintenance_repair_order_no_seq")
