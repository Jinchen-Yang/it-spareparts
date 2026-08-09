"""WBDD safe logical delete workflow and tombstones

Revision ID: f4b8c2d1e7a6
Revises: e6a9c3f1b2d4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f4b8c2d1e7a6"
down_revision: str | None = "e6a9c3f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _validate_permission_json() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM sys_role_template
              WHERE permissions IS NOT NULL
                AND permissions <> 'null'::jsonb
                AND jsonb_typeof(permissions) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE template_perms IS NOT NULL
                AND template_perms <> 'null'::jsonb
                AND jsonb_typeof(template_perms) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE permissions IS NOT NULL
                AND permissions <> 'null'::jsonb
                AND jsonb_typeof(permissions) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE perm_overrides IS NOT NULL
                AND perm_overrides <> 'null'::jsonb
                AND jsonb_typeof(perm_overrides) IS DISTINCT FROM 'object'
          )
          THEN
            RAISE EXCEPTION
              'f4b8c2d1e7a6 upgrade blocked: permission JSONB payload must be an object, SQL NULL, or JSON null';
          END IF;
        END
        $migration$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _validate_permission_json()

    op.create_table(
        "maintenance_demand_delete_intent",
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("selection_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operated_by", sa.String(length=64), nullable=False),
        sa.Column("header_count", sa.Integer(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "status IN ('reviewed', 'armed_wait', 'executed', "
            "'cancelled', 'conflicted', 'expired')",
            name="ck_maintenance_demand_delete_intent_status",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maintenance_demand_delete_intent_reason",
        ),
        sa.CheckConstraint(
            "header_count BETWEEN 1 AND 1000",
            name="ck_maintenance_demand_delete_intent_headers",
        ),
        sa.CheckConstraint(
            "line_count BETWEEN 0 AND 20000",
            name="ck_maintenance_demand_delete_intent_lines",
        ),
        sa.PrimaryKeyConstraint("intent_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_maintenance_demand_delete_intent_status_expiry",
        "maintenance_demand_delete_intent",
        ["status", "expires_at"],
    )

    op.create_table(
        "maintenance_demand_delete_intent_item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("source_order_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("version_digest", sa.String(length=64), nullable=False),
        sa.Column("snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_maintenance_demand_delete_intent_item_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["maintenance_demand_delete_intent.intent_id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_order_id"], ["f_maintenance_order.raw_order_id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "intent_id", "source_order_id",
            name="uq_maintenance_demand_delete_intent_item_source",
        ),
        sa.UniqueConstraint(
            "intent_id", "ordinal",
            name="uq_maintenance_demand_delete_intent_item_ordinal",
        ),
    )
    op.create_index(
        "ix_maintenance_demand_delete_intent_item_source",
        "maintenance_demand_delete_intent_item",
        ["source_order_id"],
    )

    op.create_table(
        "maintenance_demand_tombstone",
        sa.Column("source_order_id", sa.String(length=64), nullable=False),
        sa.Column("delete_intent_id", sa.String(length=36), nullable=False),
        sa.Column("version_digest", sa.String(length=64), nullable=False),
        sa.Column("deleted_by", sa.String(length=64), nullable=False),
        sa.Column("delete_reason", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_by", sa.String(length=64), nullable=True),
        sa.Column("restore_reason", sa.Text(), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "char_length(btrim(delete_reason)) > 0",
            name="ck_maintenance_demand_tombstone_delete_reason",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_maintenance_demand_tombstone_version",
        ),
        sa.ForeignKeyConstraint(
            ["delete_intent_id"], ["maintenance_demand_delete_intent.intent_id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_order_id"], ["f_maintenance_order.raw_order_id"]
        ),
        sa.PrimaryKeyConstraint("source_order_id"),
    )
    op.create_index(
        "ix_maintenance_demand_tombstone_active",
        "maintenance_demand_tombstone",
        ["source_order_id", "restored_at"],
    )

    op.create_table(
        "maintenance_demand_delete_event",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=True),
        sa.Column("source_order_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("operated_by", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('armed', 'executed', 'cancelled', "
            "'conflicted', 'expired', 'restored')",
            name="ck_maintenance_demand_delete_event_type",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maintenance_demand_delete_event_reason",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["maintenance_demand_delete_intent.intent_id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_order_id"], ["f_maintenance_order.raw_order_id"]
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_maintenance_demand_delete_event_intent_time",
        "maintenance_demand_delete_event",
        ["intent_id", "occurred_at"],
    )
    op.create_index(
        "ix_maintenance_demand_delete_event_source_time",
        "maintenance_demand_delete_event",
        ["source_order_id", "occurred_at"],
    )

    op.execute(
        """
        CREATE FUNCTION reject_maintenance_demand_delete_history_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'maintenance demand delete history is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_maintenance_demand_delete_intent_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.intent_id IS DISTINCT FROM OLD.intent_id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
               OR NEW.selection_digest IS DISTINCT FROM OLD.selection_digest
               OR NEW.reason IS DISTINCT FROM OLD.reason
               OR NEW.operated_by IS DISTINCT FROM OLD.operated_by
               OR NEW.header_count IS DISTINCT FROM OLD.header_count
               OR NEW.line_count IS DISTINCT FROM OLD.line_count
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
            THEN
                RAISE EXCEPTION
                    'maintenance demand delete intent identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_maintenance_demand_delete_intent_item_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_count integer;
            current_count integer;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION
                    'maintenance demand delete intent item is immutable';
            END IF;

            SELECT header_count
            INTO expected_count
            FROM maintenance_demand_delete_intent
            WHERE intent_id = NEW.intent_id
            FOR UPDATE;

            IF expected_count IS NULL THEN
                RAISE EXCEPTION
                    'maintenance demand delete intent item set is invalid';
            END IF;

            SELECT count(*)
            INTO current_count
            FROM maintenance_demand_delete_intent_item
            WHERE intent_id = NEW.intent_id;

            IF current_count >= expected_count THEN
                RAISE EXCEPTION
                    'maintenance demand delete intent item set is immutable';
            END IF;
            IF NEW.ordinal < 0 OR NEW.ordinal >= expected_count THEN
                RAISE EXCEPTION
                    'maintenance demand delete intent item set is invalid';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_demand_delete_event_append_only
        BEFORE UPDATE OR DELETE ON maintenance_demand_delete_event
        FOR EACH ROW
        EXECUTE FUNCTION reject_maintenance_demand_delete_history_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_demand_delete_intent_item_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON maintenance_demand_delete_intent_item
        FOR EACH ROW
        EXECUTE FUNCTION enforce_maintenance_demand_delete_intent_item_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_demand_delete_intent_identity_immutable
        BEFORE UPDATE ON maintenance_demand_delete_intent
        FOR EACH ROW
        EXECUTE FUNCTION enforce_maintenance_demand_delete_intent_identity()
        """
    )

    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'action_maintenance_demand_delete', code = 'admin'
            )
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
                'action_maintenance_demand_delete', role = 'admin'
            ),
            perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides
                    ELSE '{}'::jsonb
                END - 'action_maintenance_demand_delete'
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
                'action_maintenance_demand_delete', role = 'admin'
            )
        WHERE permissions IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_demand_delete_event, "
        "maintenance_demand_tombstone, maintenance_demand_delete_intent_item, "
        "maintenance_demand_delete_intent IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_demand_delete_event)
             OR EXISTS (SELECT 1 FROM maintenance_demand_tombstone)
             OR EXISTS (SELECT 1 FROM maintenance_demand_delete_intent_item)
             OR EXISTS (SELECT 1 FROM maintenance_demand_delete_intent)
          THEN
            RAISE EXCEPTION
              'f4b8c2d1e7a6 downgrade blocked: WBDD delete history is not empty';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        "DROP TRIGGER trg_maintenance_demand_delete_intent_identity_immutable "
        "ON maintenance_demand_delete_intent"
    )
    op.execute(
        "DROP TRIGGER trg_maintenance_demand_delete_intent_item_immutable "
        "ON maintenance_demand_delete_intent_item"
    )
    op.execute(
        "DROP TRIGGER trg_maintenance_demand_delete_event_append_only "
        "ON maintenance_demand_delete_event"
    )
    op.execute(
        "DROP FUNCTION enforce_maintenance_demand_delete_intent_item_immutable()"
    )
    op.execute("DROP FUNCTION enforce_maintenance_demand_delete_intent_identity()")
    op.execute("DROP FUNCTION reject_maintenance_demand_delete_history_mutation()")
    op.execute(
        "UPDATE sys_role_template SET permissions = permissions - "
        "'action_maintenance_demand_delete' WHERE jsonb_typeof(permissions) = 'object'"
    )
    op.execute(
        "UPDATE sys_user SET template_perms = template_perms - "
        "'action_maintenance_demand_delete' WHERE jsonb_typeof(template_perms) = 'object'"
    )
    op.execute(
        "UPDATE sys_user SET permissions = permissions - "
        "'action_maintenance_demand_delete' WHERE jsonb_typeof(permissions) = 'object'"
    )
    op.execute(
        "UPDATE sys_user SET perm_overrides = perm_overrides - "
        "'action_maintenance_demand_delete' WHERE jsonb_typeof(perm_overrides) = 'object'"
    )
    op.drop_index(
        "ix_maintenance_demand_delete_event_source_time",
        table_name="maintenance_demand_delete_event",
    )
    op.drop_index(
        "ix_maintenance_demand_delete_event_intent_time",
        table_name="maintenance_demand_delete_event",
    )
    op.drop_table("maintenance_demand_delete_event")
    op.drop_index(
        "ix_maintenance_demand_tombstone_active",
        table_name="maintenance_demand_tombstone",
    )
    op.drop_table("maintenance_demand_tombstone")
    op.drop_index(
        "ix_maintenance_demand_delete_intent_item_source",
        table_name="maintenance_demand_delete_intent_item",
    )
    op.drop_table("maintenance_demand_delete_intent_item")
    op.drop_index(
        "ix_maintenance_demand_delete_intent_status_expiry",
        table_name="maintenance_demand_delete_intent",
    )
    op.drop_table("maintenance_demand_delete_intent")
