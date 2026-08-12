"""merge maintenance assignment/warehouse heads and enable exact shipment bridge

Revision ID: b2c4e6f8a1d3
Revises: a6d1e9c3b7f2, e6f1a9c3b7d2, f5a7c9e1b3d4
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b2c4e6f8a1d3"
down_revision: tuple[str, str, str] = (
    "a6d1e9c3b7f2",
    "e6f1a9c3b7d2",
    "f5a7c9e1b3d4",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _project_audit_union() -> None:
    op.drop_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        "entity_type IN ("
        "'project', 'project_contract', 'manager_assignment', "
        "'source_order_assignment'"
        ")",
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The two parent branches each extended the same legacy constraint.  Their
    # combined state must retain both entity kinds regardless of upgrade order.
    _project_audit_union()

    op.drop_constraint(
        "ck_maintenance_wh_audit_action",
        "maintenance_warehouse_audit_event",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_wh_audit_action",
        "maintenance_warehouse_audit_event",
        "action IN ('import_applied', 'ambiguity_resolved', "
        "'integration_reconciled')",
    )

    op.drop_constraint(
        "ck_maintenance_site_issue_delivery_adapter",
        "maintenance_site_issue_delivery_source",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_delivery_adapter",
        "maintenance_site_issue_delivery_source",
        "adapter_key IN ('synthetic_delivery_v1', 'warehouse_shipment_v1')",
    )
    op.alter_column(
        "maintenance_site_issue_delivery_source",
        "source_line_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.alter_column(
        "maintenance_site_issue_delivery_source",
        "delivery_no",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.alter_column(
        "maintenance_site_issue_line",
        "source_line_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_warehouse_audit_event, "
        "maintenance_warehouse_ambiguity, maintenance_warehouse_document_link, "
        "maintenance_warehouse_document_line, maintenance_warehouse_document, "
        "maintenance_warehouse_import_batch, maintenance_source_order_assignment, "
        "maintenance_demand_delete_event, maintenance_demand_tombstone, "
        "maintenance_demand_delete_intent_item, maintenance_demand_delete_intent, "
        "maintenance_project_user_assignment, maintenance_project_audit_log, "
        "maintenance_site_issue_return_event, maintenance_site_issue_command, "
        "maintenance_site_issue_line, maintenance_site_issue, "
        "maintenance_site_issue_delivery_source, maintenance_bad_return_command, "
        "maintenance_bad_return_line, maintenance_bad_return, "
        "maintenance_return_obligation, maintenance_manager_upload_batch, "
        "maintenance_manager_upload_batch_project, maintenance_service_period, "
        "maintenance_collection_milestone, maintenance_acceptance_deliverable, "
        "business_file, business_file_link, maintenance_acceptance_operation, "
        "business_file_download_audit, maintenance_project_operation_audit "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_warehouse_audit_event)
             OR EXISTS (SELECT 1 FROM maintenance_warehouse_ambiguity)
             OR EXISTS (SELECT 1 FROM maintenance_warehouse_document_link)
             OR EXISTS (SELECT 1 FROM maintenance_warehouse_document_line)
             OR EXISTS (SELECT 1 FROM maintenance_warehouse_document)
             OR EXISTS (SELECT 1 FROM maintenance_warehouse_import_batch)
             OR EXISTS (SELECT 1 FROM maintenance_source_order_assignment)
             OR EXISTS (
                SELECT 1 FROM maintenance_project_audit_log
                WHERE entity_type = 'source_order_assignment'
             )
             OR EXISTS (SELECT 1 FROM maintenance_demand_delete_event)
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
             OR EXISTS (
                SELECT 1
                FROM maintenance_site_issue_delivery_source
                WHERE adapter_key = 'warehouse_shipment_v1'
             )
             OR EXISTS (
                SELECT 1
                FROM maintenance_warehouse_audit_event
                WHERE action = 'integration_reconciled'
             )
             OR EXISTS (
                SELECT 1
                FROM maintenance_site_issue_delivery_source
                WHERE char_length(source_line_id) > 64
                   OR char_length(delivery_no) > 64
             )
             OR EXISTS (
                SELECT 1
                FROM maintenance_site_issue_line
                WHERE char_length(source_line_id) > 64
             )
          THEN
            RAISE EXCEPTION
              'b2c4e6f8a1d3 downgrade blocked: combined maintenance history is not empty';
          END IF;
        END
        $migration$;
        """
    )

    op.alter_column(
        "maintenance_site_issue_line",
        "source_line_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "maintenance_site_issue_delivery_source",
        "delivery_no",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "maintenance_site_issue_delivery_source",
        "source_line_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_delivery_adapter",
        "maintenance_site_issue_delivery_source",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_delivery_adapter",
        "maintenance_site_issue_delivery_source",
        "adapter_key = 'synthetic_delivery_v1'",
    )
    op.drop_constraint(
        "ck_maintenance_wh_audit_action",
        "maintenance_warehouse_audit_event",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_wh_audit_action",
        "maintenance_warehouse_audit_event",
        "action IN ('import_applied', 'ambiguity_resolved')",
    )
    # Keep the audit union while all three parent heads coexist.  Reverting to
    # either parent's narrower constraint would invalidate the other parent's
    # legitimate append-only history.
    _project_audit_union()
