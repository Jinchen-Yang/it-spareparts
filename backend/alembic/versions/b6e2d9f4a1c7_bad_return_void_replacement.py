"""add audited bad-return void and replacement workflow

Revision ID: b6e2d9f4a1c7
Revises: a8d3c7e5f1b2
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b6e2d9f4a1c7"
down_revision: str | None = "a8d3c7e5f1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_STATUS = (
    "status IN ('draft', 'submitted', 'in_transit', 'warehouse_confirmed')"
)
_NEW_STATUS = (
    "status IN ('draft', 'submitted', 'in_transit', "
    "'warehouse_confirmed', 'void')"
)
_OLD_STATE_EVIDENCE = (
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
    "AND warehouse_reference IS NOT NULL)"
)
_NEW_STATE_EVIDENCE = (
    "(status = 'draft' AND submitted_at IS NULL AND in_transit_at IS NULL "
    "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NULL "
    "AND warehouse_reference IS NULL AND inbound_reference IS NULL "
    "AND voided_at IS NULL) OR "
    "(status = 'submitted' AND submitted_at IS NOT NULL "
    "AND in_transit_at IS NULL AND warehouse_confirmed_at IS NULL "
    "AND logistics_reference IS NULL AND warehouse_reference IS NULL "
    "AND inbound_reference IS NULL AND voided_at IS NULL) OR "
    "(status = 'in_transit' AND submitted_at IS NOT NULL "
    "AND in_transit_at IS NOT NULL AND warehouse_confirmed_at IS NULL "
    "AND logistics_reference IS NOT NULL AND warehouse_reference IS NULL "
    "AND inbound_reference IS NULL AND voided_at IS NULL) OR "
    "(status = 'warehouse_confirmed' AND submitted_at IS NOT NULL "
    "AND warehouse_confirmed_at IS NOT NULL "
    "AND ((in_transit_at IS NULL AND logistics_reference IS NULL) OR "
    "(in_transit_at IS NOT NULL AND logistics_reference IS NOT NULL)) "
    "AND warehouse_reference IS NOT NULL AND voided_at IS NULL) OR "
    "(status = 'void' AND voided_at IS NOT NULL "
    "AND inbound_reference IS NULL AND ("
    "(submitted_at IS NULL AND in_transit_at IS NULL "
    "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NULL "
    "AND warehouse_reference IS NULL) OR "
    "(submitted_at IS NOT NULL AND in_transit_at IS NULL "
    "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NULL "
    "AND warehouse_reference IS NULL) OR "
    "(submitted_at IS NOT NULL AND in_transit_at IS NOT NULL "
    "AND warehouse_confirmed_at IS NULL AND logistics_reference IS NOT NULL "
    "AND warehouse_reference IS NULL) OR "
    "(submitted_at IS NOT NULL AND warehouse_confirmed_at IS NOT NULL "
    "AND warehouse_reference IS NOT NULL "
    "AND ((in_transit_at IS NULL AND logistics_reference IS NULL) OR "
    "(in_transit_at IS NOT NULL AND logistics_reference IS NOT NULL)))))"
)
_OLD_COMMAND_ACTION = (
    "action IN ('create', 'submit', 'in_transit', "
    "'warehouse_confirm', 'resolve_category')"
)
_NEW_COMMAND_ACTION = (
    "action IN ('create', 'submit', 'in_transit', "
    "'warehouse_confirm', 'void', 'resolve_category')"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "maintenance_bad_return",
        sa.Column("replaces_return_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "maintenance_bad_return",
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_maintenance_bad_return_state_evidence",
        "maintenance_bad_return",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_bad_return_status",
        "maintenance_bad_return",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_bad_return_status",
        "maintenance_bad_return",
        _NEW_STATUS,
    )
    op.create_check_constraint(
        "ck_maintenance_bad_return_state_evidence",
        "maintenance_bad_return",
        _NEW_STATE_EVIDENCE,
    )
    op.create_check_constraint(
        "ck_maintenance_bad_return_replacement_not_self",
        "maintenance_bad_return",
        "replaces_return_id IS NULL OR replaces_return_id <> return_id",
    )
    op.create_foreign_key(
        "fk_maintenance_bad_return_replaces_return_id",
        "maintenance_bad_return",
        "maintenance_bad_return",
        ["replaces_return_id"],
        ["return_id"],
    )
    op.create_unique_constraint(
        "uq_maintenance_bad_return_replaces_return_id",
        "maintenance_bad_return",
        ["replaces_return_id"],
    )
    op.drop_constraint(
        "ck_maintenance_bad_return_command_action",
        "maintenance_bad_return_command",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_bad_return_command_action",
        "maintenance_bad_return_command",
        _NEW_COMMAND_ACTION,
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_bad_return_command, maintenance_bad_return "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1
              FROM maintenance_bad_return
          ) OR EXISTS (
              SELECT 1
              FROM maintenance_bad_return_command
              WHERE action = 'void'
          )
          THEN
            RAISE EXCEPTION
              'b6e2d9f4a1c7 downgrade blocked: bad return business history exists';
          END IF;
        END
        $migration$;
        """
    )
    op.drop_constraint(
        "ck_maintenance_bad_return_command_action",
        "maintenance_bad_return_command",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_bad_return_command_action",
        "maintenance_bad_return_command",
        _OLD_COMMAND_ACTION,
    )
    op.drop_constraint(
        "uq_maintenance_bad_return_replaces_return_id",
        "maintenance_bad_return",
        type_="unique",
    )
    op.drop_constraint(
        "fk_maintenance_bad_return_replaces_return_id",
        "maintenance_bad_return",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_maintenance_bad_return_replacement_not_self",
        "maintenance_bad_return",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_bad_return_state_evidence",
        "maintenance_bad_return",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_bad_return_status",
        "maintenance_bad_return",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_bad_return_status",
        "maintenance_bad_return",
        _OLD_STATUS,
    )
    op.create_check_constraint(
        "ck_maintenance_bad_return_state_evidence",
        "maintenance_bad_return",
        _OLD_STATE_EVIDENCE,
    )
    op.drop_column("maintenance_bad_return", "voided_at")
    op.drop_column("maintenance_bad_return", "replaces_return_id")
