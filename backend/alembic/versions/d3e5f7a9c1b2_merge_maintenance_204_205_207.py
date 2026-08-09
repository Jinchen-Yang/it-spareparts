"""Merge maintenance issues 204, 205, and 207 schema heads.

Revision ID: d3e5f7a9c1b2
Revises: a6c8d2e4f1b7, f4b8c2d1e7a6, f4b8d2e6a1c3
Create Date: 2026-08-09
"""

from collections.abc import Sequence

from alembic import op


revision: str = "d3e5f7a9c1b2"
down_revision: str | Sequence[str] | None = (
    "a6c8d2e4f1b7",
    "f4b8c2d1e7a6",
    "f4b8d2e6a1c3",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge the independently reviewed schema branches."""


def downgrade() -> None:
    """Preflight every child branch before Alembic traverses any of them."""

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_demand_delete_event, "
        "maintenance_demand_tombstone, maintenance_demand_delete_intent_item, "
        "maintenance_demand_delete_intent, maintenance_project_user_assignment, "
        "maintenance_project_audit_log, maintenance_site_issue_return_event, "
        "maintenance_site_issue_command, maintenance_site_issue_line, "
        "maintenance_site_issue, maintenance_site_issue_delivery_source "
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
          THEN
            RAISE EXCEPTION
              'd3e5f7a9c1b2 downgrade blocked: combined maintenance history is not empty';
          END IF;
        END
        $migration$;
        """
    )
