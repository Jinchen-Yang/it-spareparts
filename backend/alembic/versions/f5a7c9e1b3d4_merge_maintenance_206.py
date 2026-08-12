"""Merge the manager-workbook and acceptance branch into maintenance.

Revision ID: f5a7c9e1b3d4
Revises: e4f6a8c2d1b3, c8f2d4a6b9e1
Create Date: 2026-08-10
"""

from collections.abc import Sequence

from alembic import op


revision: str = "f5a7c9e1b3d4"
down_revision: str | Sequence[str] | None = (
    "e4f6a8c2d1b3",
    "c8f2d4a6b9e1",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge the independently reviewed schema branches."""


def downgrade() -> None:
    """Preflight all child histories before Alembic can traverse either one."""

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_demand_delete_event, "
        "maintenance_demand_tombstone, maintenance_demand_delete_intent_item, "
        "maintenance_demand_delete_intent, maintenance_project_user_assignment, "
        "maintenance_project_audit_log, maintenance_site_issue_return_event, "
        "maintenance_site_issue_command, maintenance_site_issue_line, "
        "maintenance_site_issue, maintenance_site_issue_delivery_source, "
        "maintenance_bad_return_command, maintenance_bad_return_line, "
        "maintenance_bad_return, maintenance_return_obligation, "
        "maintenance_manager_upload_batch, maintenance_manager_upload_batch_project, "
        "maintenance_service_period, maintenance_collection_milestone, "
        "maintenance_acceptance_deliverable, business_file, business_file_link, "
        "maintenance_acceptance_operation, business_file_download_audit, "
        "maintenance_project_operation_audit "
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
             OR EXISTS (SELECT 1 FROM maintenance_manager_upload_batch)
             OR EXISTS (SELECT 1 FROM maintenance_manager_upload_batch_project)
             OR EXISTS (SELECT 1 FROM maintenance_service_period)
             OR EXISTS (SELECT 1 FROM maintenance_collection_milestone)
             OR EXISTS (SELECT 1 FROM maintenance_acceptance_deliverable)
             OR EXISTS (SELECT 1 FROM business_file)
             OR EXISTS (SELECT 1 FROM business_file_link)
             OR EXISTS (SELECT 1 FROM maintenance_acceptance_operation)
             OR EXISTS (SELECT 1 FROM business_file_download_audit)
             OR EXISTS (
                SELECT 1 FROM maintenance_project_operation_audit
                WHERE entity_type IN (
                    'service_period',
                    'acceptance_deliverable',
                    'collection_milestone'
                )
             )
          THEN
            RAISE EXCEPTION
              'f5a7c9e1b3d4 downgrade blocked: combined maintenance history is not empty';
          END IF;
        END
        $migration$;
        """
    )
