"""Merge bad-return recovery with the maintenance integration head.

Revision ID: e4f6a8c2d1b3
Revises: d3e5f7a9c1b2, b6e2d9f4a1c7
Create Date: 2026-08-09
"""

from collections.abc import Sequence

from alembic import op


revision: str = "e4f6a8c2d1b3"
down_revision: str | Sequence[str] | None = (
    "d3e5f7a9c1b2",
    "b6e2d9f4a1c7",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge the independently reviewed schema branches."""


def downgrade() -> None:
    """Block before Alembic can partially traverse either child branch."""

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_demand_delete_event, "
        "maintenance_demand_tombstone, maintenance_demand_delete_intent_item, "
        "maintenance_demand_delete_intent, maintenance_project_user_assignment, "
        "maintenance_project_audit_log, maintenance_site_issue_return_event, "
        "maintenance_site_issue_command, maintenance_site_issue_line, "
        "maintenance_site_issue, maintenance_site_issue_delivery_source, "
        "maintenance_bad_return_command, maintenance_bad_return_line, "
        "maintenance_bad_return, maintenance_return_obligation "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_demand_delete_event)
             OR EXISTS (SELECT 1 FROM maintenance_demand_tombstone)
             OR EXISTS (SELECT 1 FROM maintenance_demand_delete_intent_item)
             OR EXISTS (SELECT 1 FROM maintenance_demand_delete_intent)
             OR EXISTS (SELECT 1 FROM maintenance_project_user_assignment)
             OR EXISTS (
                SELECT 1 FROM maintenance_project_audit_log
                WHERE entity_type = 'manager_assignment'
             )
             OR EXISTS (SELECT 1 FROM maintenance_site_issue_return_event)
             OR EXISTS (SELECT 1 FROM maintenance_site_issue_command)
             OR EXISTS (SELECT 1 FROM maintenance_site_issue_delivery_source)
             OR EXISTS (
                SELECT 1 FROM maintenance_site_issue
                WHERE source = 'site_issue_v2'
             )
             OR EXISTS (
                SELECT 1 FROM maintenance_site_issue_line
                WHERE delivery_line_id IS NOT NULL
                   OR source_order_id IS NOT NULL
                   OR source_line_id IS NOT NULL
                   OR serial_number IS NOT NULL
             )
             OR EXISTS (SELECT 1 FROM maintenance_bad_return_command)
             OR EXISTS (SELECT 1 FROM maintenance_bad_return_line)
             OR EXISTS (SELECT 1 FROM maintenance_bad_return)
             OR EXISTS (SELECT 1 FROM maintenance_return_obligation)
          THEN
            RAISE EXCEPTION
              'e4f6a8c2d1b3 downgrade blocked: combined maintenance history is not empty';
          END IF;
        END
        $migration$;
        """
    )
