"""add server-owned site issue drafts and stable delivery sources

Revision ID: f4b8d2e6a1c3
Revises: e6a9c3f1b2d4
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4b8d2e6a1c3"
down_revision: str | None = "e6a9c3f1b2d4"
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
                'action_maintenance_site_issue_manage', code = 'admin'
            )
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
                'action_maintenance_site_issue_manage', role = 'admin'
            ),
            perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides
                    ELSE '{}'::jsonb
                END - 'action_maintenance_site_issue_manage'
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
                'action_maintenance_site_issue_manage', role = 'admin'
            )
        WHERE permissions IS NOT NULL
        """
    )
    op.create_table(
        "maintenance_site_issue_delivery_source",
        sa.Column("delivery_line_id", sa.String(length=64), nullable=False),
        sa.Column("adapter_key", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_order_id", sa.String(length=64), nullable=False),
        sa.Column("source_line_id", sa.String(length=64), nullable=False),
        sa.Column("delivery_no", sa.String(length=64), nullable=False),
        sa.Column("delivery_date", sa.Date(), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn", sa.String(length=128), nullable=False),
        sa.Column("serial_number", sa.Text(), nullable=True),
        sa.Column("delivered_quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("linked_purchase_line_id", sa.Integer(), nullable=True),
        sa.Column("mapping_state", sa.String(length=16), nullable=False),
        sa.Column("mapping_version", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "adapter_key = 'synthetic_delivery_v1'",
            name="ck_maintenance_site_issue_delivery_adapter",
        ),
        sa.CheckConstraint(
            "mapping_state IN ('ready', 'unavailable')",
            name="ck_maintenance_site_issue_delivery_mapping_state",
        ),
        sa.CheckConstraint(
            "delivered_quantity > 0 AND delivered_quantity < 1000000000000",
            name="ck_maintenance_site_issue_delivery_quantity",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(
            ["linked_purchase_line_id"], ["f_purchase_line.id"]
        ),
        sa.PrimaryKeyConstraint("delivery_line_id"),
        sa.UniqueConstraint(
            "adapter_key",
            "source_order_id",
            "source_line_id",
            name="uq_maintenance_site_issue_delivery_source_identity",
        ),
    )
    op.create_index(
        "ix_maintenance_site_issue_delivery_project_date",
        "maintenance_site_issue_delivery_source",
        ["project_id", "delivery_date", "delivery_line_id"],
    )

    for column in (
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("receiver", sa.String(length=128), nullable=True),
        sa.Column("issued_by", sa.String(length=128), nullable=True),
        sa.Column("site_location", sa.String(length=256), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("maintenance_site_issue", column)

    op.drop_constraint(
        "ck_maintenance_site_issue_normalized_status",
        "maintenance_site_issue",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_normalized_status",
        "maintenance_site_issue",
        "normalized_status IN ('draft', 'confirmed', 'corrected', 'void', 'unknown')",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_source",
        "maintenance_site_issue",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_source",
        "maintenance_site_issue",
        "source IN ('legacy', 'direct_api', 'workbook', 'site_issue_v2')",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_import_batch",
        "maintenance_site_issue",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_import_batch",
        "maintenance_site_issue",
        "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
        "(source IN ('legacy', 'direct_api', 'site_issue_v2') AND import_batch_id IS NULL)",
    )
    op.create_index(
        "uq_maintenance_site_issue_v2_no",
        "maintenance_site_issue",
        ["issue_no"],
        unique=True,
        postgresql_where=sa.text("source = 'site_issue_v2'"),
    )
    op.create_index(
        "uq_maintenance_site_issue_idempotency",
        "maintenance_site_issue",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("delivery_line_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("source_order_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("source_line_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("serial_number", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_maintenance_site_issue_line_delivery",
        "maintenance_site_issue_line",
        "maintenance_site_issue_delivery_source",
        ["delivery_line_id"],
        ["delivery_line_id"],
    )
    op.create_table(
        "maintenance_site_issue_command",
        sa.Column("command_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("issue_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('update', 'confirm', 'void', 'correct')",
            name="ck_maintenance_site_issue_command_action",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["maintenance_site_issue.issue_id"]),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.PrimaryKeyConstraint("command_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_maintenance_site_issue_command_issue_time",
        "maintenance_site_issue_command",
        ["issue_id", "created_at"],
    )
    op.create_table(
        "maintenance_site_issue_return_event",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("issue_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("issue_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("downstream_reference", sa.String(length=128), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('return_obligation_created', "
            "'return_obligation_corrected', 'return_obligation_voided')",
            name="ck_maintenance_site_issue_return_event_type",
        ),
        sa.CheckConstraint(
            "issue_version >= 1",
            name="ck_maintenance_site_issue_return_event_version",
        ),
        sa.CheckConstraint(
            "(downstream_reference IS NULL) = (consumed_at IS NULL)",
            name="ck_maintenance_site_issue_return_event_consumed_pair",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["maintenance_site_issue.issue_id"]),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "issue_id",
            "event_type",
            "issue_version",
            name="uq_maintenance_site_issue_return_event_version",
        ),
    )
    op.create_index(
        "ix_maintenance_site_issue_return_event_issue_time",
        "maintenance_site_issue_return_event",
        ["issue_id", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_maintenance_site_issue_command_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'maintenance_site_issue_command is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_site_issue_command_append_only
        BEFORE UPDATE OR DELETE ON maintenance_site_issue_command
        FOR EACH ROW
        EXECUTE FUNCTION reject_maintenance_site_issue_command_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_maintenance_site_issue_return_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.downstream_reference IS NULL
               AND OLD.consumed_at IS NULL
               AND NEW.downstream_reference IS NOT NULL
               AND NEW.consumed_at IS NOT NULL
               AND ROW(
                    NEW.event_id,
                    NEW.project_id,
                    NEW.issue_id,
                    NEW.event_type,
                    NEW.issue_version,
                    NEW.payload,
                    NEW.created_at
               ) IS NOT DISTINCT FROM ROW(
                    OLD.event_id,
                    OLD.project_id,
                    OLD.issue_id,
                    OLD.event_type,
                    OLD.issue_version,
                    OLD.payload,
                    OLD.created_at
               )
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'maintenance_site_issue_return_event is append-only except one downstream registration';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_site_issue_return_event_guard
        BEFORE UPDATE OR DELETE ON maintenance_site_issue_return_event
        FOR EACH ROW
        EXECUTE FUNCTION guard_maintenance_site_issue_return_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER trg_maintenance_site_issue_return_event_guard
        ON maintenance_site_issue_return_event
        """
    )
    op.execute(
        "DROP FUNCTION guard_maintenance_site_issue_return_event_mutation()"
    )
    op.execute(
        """
        DROP TRIGGER trg_maintenance_site_issue_command_append_only
        ON maintenance_site_issue_command
        """
    )
    op.execute("DROP FUNCTION reject_maintenance_site_issue_command_mutation()")
    op.drop_index(
        "ix_maintenance_site_issue_return_event_issue_time",
        table_name="maintenance_site_issue_return_event",
    )
    op.drop_table("maintenance_site_issue_return_event")
    op.drop_index(
        "ix_maintenance_site_issue_command_issue_time",
        table_name="maintenance_site_issue_command",
    )
    op.drop_table("maintenance_site_issue_command")
    op.drop_constraint(
        "fk_maintenance_site_issue_line_delivery",
        "maintenance_site_issue_line",
        type_="foreignkey",
    )
    for name in ("serial_number", "source_line_id", "source_order_id", "delivery_line_id"):
        op.drop_column("maintenance_site_issue_line", name)

    op.drop_index(
        "uq_maintenance_site_issue_idempotency",
        table_name="maintenance_site_issue",
    )
    op.drop_index("uq_maintenance_site_issue_v2_no", table_name="maintenance_site_issue")
    op.drop_constraint(
        "ck_maintenance_site_issue_import_batch",
        "maintenance_site_issue",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_import_batch",
        "maintenance_site_issue",
        "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
        "(source IN ('legacy', 'direct_api') AND import_batch_id IS NULL)",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_source",
        "maintenance_site_issue",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_source",
        "maintenance_site_issue",
        "source IN ('legacy', 'direct_api', 'workbook')",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_normalized_status",
        "maintenance_site_issue",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_normalized_status",
        "maintenance_site_issue",
        "normalized_status IN ('confirmed', 'void', 'unknown')",
    )
    for name in (
        "voided_at",
        "corrected_at",
        "confirmed_at",
        "created_by",
        "site_location",
        "issued_by",
        "receiver",
        "request_fingerprint",
        "idempotency_key",
    ):
        op.drop_column("maintenance_site_issue", name)

    op.drop_index(
        "ix_maintenance_site_issue_delivery_project_date",
        table_name="maintenance_site_issue_delivery_source",
    )
    op.drop_table("maintenance_site_issue_delivery_source")
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = permissions - 'action_maintenance_site_issue_manage'
        WHERE jsonb_typeof(permissions) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms - 'action_maintenance_site_issue_manage'
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET permissions = permissions - 'action_maintenance_site_issue_manage'
        WHERE jsonb_typeof(permissions) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET perm_overrides = perm_overrides - 'action_maintenance_site_issue_manage'
        WHERE jsonb_typeof(perm_overrides) = 'object'
        """
    )
