"""add bad-part return obligations and controlled return documents

Revision ID: a8d3c7e5f1b2
Revises: f4b8d2e6a1c3
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a8d3c7e5f1b2"
down_revision: str | None = "f4b8d2e6a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'action_maintenance_bad_return_manage', code = 'admin'
            )
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
                'action_maintenance_bad_return_manage', role = 'admin'
            ),
            perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides
                    ELSE '{}'::jsonb
                END - 'action_maintenance_bad_return_manage'
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'action_maintenance_bad_return_manage', role = 'admin'
            )
        WHERE permissions IS NOT NULL
        """
    )

    op.create_table(
        "maintenance_return_obligation",
        sa.Column("obligation_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("issue_id", sa.String(length=36), nullable=False),
        sa.Column("issue_line_id", sa.String(length=64), nullable=False),
        sa.Column("delivery_line_id", sa.String(length=64), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn", sa.String(length=128), nullable=False),
        sa.Column("source_quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("required_quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("classification", sa.String(length=24), nullable=False),
        sa.Column("category_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("category_major_snapshot", sa.String(length=64), nullable=True),
        sa.Column("category_minor_snapshot", sa.String(length=128), nullable=True),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("source_issue_version", sa.Integer(), nullable=False),
        sa.Column("last_source_event_id", sa.String(length=36), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "classification IN ('required', 'exempt', 'pending_category')",
            name="ck_maintenance_return_obligation_classification",
        ),
        sa.CheckConstraint(
            "source_quantity > 0 AND source_quantity < 100000000000",
            name="ck_maintenance_return_obligation_source_quantity",
        ),
        sa.CheckConstraint(
            "required_quantity >= 0 AND required_quantity < 100000000000",
            name="ck_maintenance_return_obligation_required_quantity",
        ),
        sa.CheckConstraint(
            "(classification = 'required' AND category_id_snapshot IS NOT NULL "
            "AND required_quantity = source_quantity) OR "
            "(classification = 'exempt' AND category_id_snapshot IS NOT NULL "
            "AND category_major_snapshot = '硬盘' AND required_quantity = 0) OR "
            "(classification = 'pending_category' AND category_id_snapshot IS NULL "
            "AND category_major_snapshot IS NULL AND category_minor_snapshot IS NULL "
            "AND required_quantity = 0)",
            name="ck_maintenance_return_obligation_rule_result",
        ),
        sa.CheckConstraint(
            "source_issue_version >= 1 AND version >= 1",
            name="ck_maintenance_return_obligation_versions",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(["issue_id"], ["maintenance_site_issue.issue_id"]),
        sa.ForeignKeyConstraint(
            ["last_source_event_id"], ["maintenance_site_issue_return_event.event_id"]
        ),
        sa.PrimaryKeyConstraint("obligation_id"),
        sa.UniqueConstraint(
            "issue_id",
            "delivery_line_id",
            name="uq_maintenance_return_obligation_source",
        ),
    )
    op.create_index(
        "ix_maintenance_return_obligation_project_state",
        "maintenance_return_obligation",
        ["project_id", "is_active", "classification"],
    )

    op.create_table(
        "maintenance_bad_return",
        sa.Column("return_id", sa.String(length=36), nullable=False),
        sa.Column("return_no", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="draft", nullable=False
        ),
        sa.Column("logistics_reference", sa.String(length=128), nullable=True),
        sa.Column("warehouse_reference", sa.String(length=128), nullable=True),
        sa.Column("inbound_reference", sa.String(length=128), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_transit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warehouse_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'in_transit', 'warehouse_confirmed')",
            name="ck_maintenance_bad_return_status",
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND submitted_at IS NULL AND in_transit_at IS NULL "
            "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NULL "
            "AND warehouse_reference IS NULL AND inbound_reference IS NULL) OR "
            "(status = 'submitted' AND submitted_at IS NOT NULL "
            "AND in_transit_at IS NULL AND warehouse_confirmed_at IS NULL "
            "AND logistics_reference IS NULL AND warehouse_reference IS NULL "
            "AND inbound_reference IS NULL) OR "
            "(status = 'in_transit' AND submitted_at IS NOT NULL "
            "AND in_transit_at IS NOT NULL AND warehouse_confirmed_at IS NULL "
            "AND logistics_reference IS NOT NULL AND warehouse_reference IS NULL "
            "AND inbound_reference IS NULL) OR "
            "(status = 'warehouse_confirmed' AND submitted_at IS NOT NULL "
            "AND warehouse_confirmed_at IS NOT NULL "
            "AND ((in_transit_at IS NULL AND logistics_reference IS NULL) OR "
            "(in_transit_at IS NOT NULL AND logistics_reference IS NOT NULL)) "
            "AND warehouse_reference IS NOT NULL)",
            name="ck_maintenance_bad_return_state_evidence",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_bad_return_version"),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.PrimaryKeyConstraint("return_id"),
        sa.UniqueConstraint("return_no"),
    )
    op.create_index(
        "ix_maintenance_bad_return_project_status",
        "maintenance_bad_return",
        ["project_id", "status", "created_at"],
    )
    op.create_index(
        "uq_maintenance_bad_return_inbound_reference",
        "maintenance_bad_return",
        ["inbound_reference"],
        unique=True,
        postgresql_where=sa.text("inbound_reference IS NOT NULL"),
    )

    op.create_table(
        "maintenance_bad_return_line",
        sa.Column("return_line_id", sa.String(length=36), nullable=False),
        sa.Column("return_id", sa.String(length=36), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("obligation_id", sa.String(length=36), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn", sa.String(length=128), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quantity > 0 AND quantity < 100000000000",
            name="ck_maintenance_bad_return_line_quantity",
        ),
        sa.CheckConstraint(
            "line_no >= 1", name="ck_maintenance_bad_return_line_no"
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["return_id"], ["maintenance_bad_return.return_id"]),
        sa.ForeignKeyConstraint(
            ["obligation_id"], ["maintenance_return_obligation.obligation_id"]
        ),
        sa.PrimaryKeyConstraint("return_line_id"),
        sa.UniqueConstraint(
            "return_id", "line_no", name="uq_maintenance_bad_return_line_no"
        ),
        sa.UniqueConstraint(
            "return_id",
            "obligation_id",
            name="uq_maintenance_bad_return_line_obligation",
        ),
    )
    op.create_index(
        "ix_maintenance_bad_return_line_obligation",
        "maintenance_bad_return_line",
        ["obligation_id", "return_id"],
    )

    op.create_table(
        "maintenance_bad_return_command",
        sa.Column("command_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=24), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entity_type IN ('bad_return', 'return_obligation')",
            name="ck_maintenance_bad_return_command_entity_type",
        ),
        sa.CheckConstraint(
            "action IN ('create', 'submit', 'in_transit', "
            "'warehouse_confirm', 'resolve_category')",
            name="ck_maintenance_bad_return_command_action",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.PrimaryKeyConstraint("command_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_maintenance_bad_return_command_entity_time",
        "maintenance_bad_return_command",
        ["entity_type", "entity_id", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_maintenance_bad_return_command_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'maintenance_bad_return_command is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_bad_return_command_append_only
        BEFORE UPDATE OR DELETE ON maintenance_bad_return_command
        FOR EACH ROW
        EXECUTE FUNCTION reject_maintenance_bad_return_command_mutation()
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_bad_return_command, "
        "maintenance_bad_return_line, maintenance_bad_return, "
        "maintenance_return_obligation IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_bad_return_command)
             OR EXISTS (SELECT 1 FROM maintenance_bad_return_line)
             OR EXISTS (SELECT 1 FROM maintenance_bad_return)
             OR EXISTS (SELECT 1 FROM maintenance_return_obligation)
          THEN
            RAISE EXCEPTION
              'a8d3c7e5f1b2 downgrade blocked: bad return business history is not empty';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        "DROP TRIGGER trg_maintenance_bad_return_command_append_only "
        "ON maintenance_bad_return_command"
    )
    op.execute("DROP FUNCTION reject_maintenance_bad_return_command_mutation()")
    op.drop_index(
        "ix_maintenance_bad_return_command_entity_time",
        table_name="maintenance_bad_return_command",
    )
    op.drop_table("maintenance_bad_return_command")
    op.drop_index(
        "ix_maintenance_bad_return_line_obligation",
        table_name="maintenance_bad_return_line",
    )
    op.drop_table("maintenance_bad_return_line")
    op.drop_index(
        "uq_maintenance_bad_return_inbound_reference",
        table_name="maintenance_bad_return",
    )
    op.drop_index(
        "ix_maintenance_bad_return_project_status",
        table_name="maintenance_bad_return",
    )
    op.drop_table("maintenance_bad_return")
    op.drop_index(
        "ix_maintenance_return_obligation_project_state",
        table_name="maintenance_return_obligation",
    )
    op.drop_table("maintenance_return_obligation")
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = permissions - 'action_maintenance_bad_return_manage'
        WHERE jsonb_typeof(permissions) = 'object'
        """
    )
    for column in ("template_perms", "permissions", "perm_overrides"):
        op.execute(
            f"""
            UPDATE sys_user
            SET {column} = {column} - 'action_maintenance_bad_return_manage'
            WHERE jsonb_typeof({column}) = 'object'
            """
        )
