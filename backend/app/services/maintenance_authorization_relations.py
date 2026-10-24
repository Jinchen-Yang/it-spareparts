"""Closed project-query relationship fingerprints, materialized entirely in SQL.

The common relation family is deliberately conservative: an unrelated parent
move invalidates existing collection output too. It reads only relationship,
status and version columns, never result rows or financial/quantity payloads.
"""
from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject, MaintenanceProjectContract, MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot, MaintenanceProjectExpenseAttribution,
    MaintenanceSiteIssue, MaintenanceSiteIssueLine,
)
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment

# Actual project-query collections. Context-only dictionaries/diagnostic or
# owner-only import lists do not implicitly enter this query family.
PROJECT_RELATION_COLLECTIONS = frozenset({
    'maintenance_summary_unassigned_orders',
    'maintenance_acceptance_projects', 'maintenance_analysis_projects',
    'maintenance_followup_directory', 'maintenance_source_orders',
    'maintenance_site_issue_cost_targets', 'maintenance_dashboard_projects',
    'maintenance_demand_selection', 'document_import_relink_candidates',
    'maintenance_expense_attribution_targets', 'maintenance_global_rows',
    'maintenance_global_workbook_content', 'maintenance_manager_monthly_projects',
    'maintenance_business_type_targets', 'maintenance_project_cards',
    'maintenance_project_directory', 'maintenance_operation_projects',
    'maintenance_project_export_selection', 'maintenance_source_order_directory',
    'wbdd_latest_missing', 'maintenance_summary_projects',
    'maintenance_warehouse_documents', 'warehouse_ambiguities', 'replenishment_projects',
})

_BASE = (
    (MaintenanceProject, ('project_id', 'version', 'is_active', 'lifecycle_status',
                         'salesperson', 'salesperson_override_active')),
    (MaintenanceProjectUserAssignment, ('assignment_id', 'project_id', 'user_id',
        'responsibility_type', 'version', 'archived_at')),
)
_PROJECT_FACT_RELATIONS = (
    (MaintenanceSourceOrderAssignment, ('assignment_id', 'source_order_id', 'project_id',
        'is_active', 'version', 'archived_at')),
    (MaintenanceSiteIssue, ('issue_id', 'project_id', 'version', 'normalized_status',
        'status_mapping_state', 'status_mapping_version')),
    (MaintenanceSiteIssueLine, ('issue_line_id', 'issue_id', 'source_order_id',
        'source_line_id', 'part_id', 'delivery_line_id', 'version', 'is_active')),
    (MaintenanceRkdReturnLine, ('rkd_line_id', 'project_id', 'source_order_id',
        'batch_id', 'head_row_id', 'part_id', 'version', 'line_status', 'source')),
    (MaintenanceProjectContract, ('project_contract_id', 'project_id', 'contract_id',
        'version', 'included_in_total', 'status_mapping_state', 'status_mapping_version')),
    (MaintenanceCollectionSnapshot, ('collection_id', 'project_id', 'project_contract_id',
        'version', 'status')),
    (MaintenanceProjectExpenseAttribution, ('expense_id', 'project_id', 'project_contract_id',
        'raw_expense_line_id', 'version', 'normalized_status', 'status_mapping_state',
        'ownership_mapping_state', 'ownership_mapping_version')),
)


def collection_relation_statement(collection_ref):
    """Build the fixed SQL projection for execution and EXPLAIN diagnostics."""
    relations = _BASE + (_PROJECT_FACT_RELATIONS if collection_ref in PROJECT_RELATION_COLLECTIONS else ())
    if collection_ref in {'maintenance_source_orders', 'maintenance_raw_contracts'}:
        # Source ownership/grouping can change without touching a stable project.
        relations += ((FMaintenanceOrder, ('id', 'raw_order_id', 'salesperson', 'project_std',
            'linked_sales_order_no', 'import_batch_id')),)
        if collection_ref == 'maintenance_raw_contracts':
            relations += (_PROJECT_FACT_RELATIONS[4],)
    expressions = []
    for model, names in relations:
        columns = [getattr(model, name) for name in names]
        row = cast(func.json_build_array(*columns), String)
        serialized = func.coalesce(func.string_agg(
            row, aggregate_order_by(literal('|'), columns[0])), literal(''))
        digest = func.encode(func.sha256(func.convert_to(serialized, 'UTF8')), 'hex')
        expressions.append(select(digest).select_from(model).scalar_subquery().label(model.__tablename__))
    return select(*expressions).prefix_with('/* maintenance_authorization_relationship_digest */')


def collection_relation_fingerprints(db, collection_ref):
    """One statement, one digest per table; no memoization across observations."""
    return dict(db.execute(collection_relation_statement(collection_ref)).mappings().one())
