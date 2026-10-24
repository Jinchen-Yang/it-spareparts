"""Maintenance domain evidence, SQL scopes and complete-context execution.

The core owns identity, grants and PDP decisions. This module owns actual
project relationships and domain resolver registration. Legacy permission maps
never become canonical identity, scope or object evidence.
"""
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import json
import math
import re
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import exists, false, func, literal, select
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.catalog import get_resource_definition
from app.authz.context import (
    ContextBatch, ObjectRef, PolicySubject, ReadSnapshotProof, ResolvedContext,
    canonical_json, create_context_batch, read_snapshot, relation_revision,
)
from app.authz.context_registry import CLOSED_KINDS, COLLECTION_REFS, ENTRY_CONTRACTS
from app.authz.engine import QueryBindings
from app.authz.errors import authz_error
from app.authz.principal import Principal, VerifiedIdentity, load_principal
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectUserAssignment
from app.models.system import SysUser

from app.authz.domain_contexts import (
    DocumentAccess,
    LockedContextBatch,
    _DOCUMENT_RESOLVERS,
    _ENTRY_RESOLVERS,
    _QUERY_BINDINGS,
    _REFERENCE_RESOLVERS,
    _named_principal,
    _selection_value,
    decode_precondition,
    document_access,
    lock_context_batch,
    lock_final_users,
    query_bindings,
    read_context_snapshot,
    register_document_resolver,
    register_entry_resolver,
    register_query_bindings,
    register_reference_resolver,
    resolve_entry_context,
    resolved_context,
    revalidate_dependency,
    slot_ref
)


@dataclass(frozen=True)
class ProjectEvidence:
    project_id: str
    subject: PolicySubject
    authorization_revision: str
    content_revision: str


@dataclass(frozen=True)
class ProjectReadPlan:
    access: service.ResourceAccess
    project_ids_query: object

    def condition(self, project_id_column):
        return project_id_column.in_(self.project_ids_query)


def project_evidence(db: Session, principal: Principal, project_ids: Sequence[str]) -> Mapping[str, ProjectEvidence]:
    """Batch actual M/V/S/active and opaque revision, with no descriptive facts.

    The caller must be in the same read snapshot or complete locked write
    transaction used for grants and facts. Assignments of all accounts enter the
    revision so a topology change cannot disappear behind the current actor.
    """
    if type(principal) is not Principal:
        raise ValueError('项目关系只接受实时主体')
    if any(type(value) is not str or not value for value in project_ids):
        raise ValueError('项目标识无效')
    ids = sorted(set(project_ids))
    if not ids:
        return MappingProxyType({})
    rows = list(db.execute(select(
        MaintenanceProject.project_id, MaintenanceProject.version,
        MaintenanceProject.is_active, MaintenanceProject.lifecycle_status,
        MaintenanceProject.salesperson, MaintenanceProject.salesperson_override_active,
    ).where(MaintenanceProject.project_id.in_(ids))))
    if {row.project_id for row in rows} != set(ids):
        raise authz_error(404, 'MAINTENANCE_PROJECT_NOT_FOUND', '维保项目不存在')
    assignments = {project_id: [] for project_id in ids}
    for row in db.execute(select(
        MaintenanceProjectUserAssignment.project_id,
        MaintenanceProjectUserAssignment.assignment_id,
        MaintenanceProjectUserAssignment.user_id,
        MaintenanceProjectUserAssignment.responsibility_type,
        MaintenanceProjectUserAssignment.version,
        MaintenanceProjectUserAssignment.archived_at,
    ).where(MaintenanceProjectUserAssignment.project_id.in_(ids))
      .order_by(MaintenanceProjectUserAssignment.assignment_id)):
        assignments[row.project_id].append(row)
    result = {}
    for project in rows:
        links = assignments[project.project_id]
        active = {row.responsibility_type for row in links
                  if row.user_id == principal.id and row.archived_at is None}
        attrs = {
            'is_manager': 'primary_manager' in active,
            'is_viewer': 'viewer' in active,
            'is_sales': bool(principal.salesperson_name and project.salesperson == principal.salesperson_name),
            'is_active': project.is_active is True,
        }
        content_revision = relation_revision({
            'project': list(project),
            'assignments': [[r.assignment_id, r.user_id, r.responsibility_type, r.version,
                             r.archived_at.isoformat() if r.archived_at else None] for r in links],
        })
        revision = relation_revision({
            'content_revision': content_revision,
            'actor': [principal.id, principal.authorization_epoch, principal.salesperson_name],
        })
        result[project.project_id] = ProjectEvidence(project.project_id,
            PolicySubject(project.project_id, attrs), revision, content_revision)
    return MappingProxyType(result)


def resolve_project_subjects(db, identity, project_ids, *, read_snapshot=None, locked_user=None):
    """Shared workflow helper: live principal and per-project evidence, no roles."""
    principal = _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    return principal, project_evidence(db, principal, project_ids)


def project_relation_table(principal: Principal):
    """Derived Boolean columns satisfy the official adapter's .table contract."""
    if type(principal) is not Principal:
        raise ValueError('项目范围需要实时主体')
    project = MaintenanceProject
    assignment = MaintenanceProjectUserAssignment
    def related(kind):
        return exists(select(assignment.assignment_id).where(
            assignment.project_id == project.project_id,
            assignment.user_id == principal.id,
            assignment.responsibility_type == kind,
            assignment.archived_at.is_(None),
        )).correlate(project)
    sales = (project.salesperson == principal.salesperson_name) if principal.salesperson_name else false()
    return select(project.project_id.label('project_id'),
        related('primary_manager').label('is_manager'), related('viewer').label('is_viewer'),
        func.coalesce(sales, false()).label('is_sales'), project.is_active.label('is_active'),
    ).subquery('maintenance_authorization_projects')


def project_query_bindings(principal, resource):
    table = project_relation_table(principal)
    attributes = {f'request.resource.attr.{name}': table.c[name]
                  for name in ('is_manager', 'is_viewer', 'is_sales', 'is_active')}
    return QueryBindings(resource, table, attributes), table.c.project_id


def project_condition(access):
    """Use the exact derived table present in the returned PlanResources SQL."""
    from sqlalchemy.sql import visitors
    tables = {column.table for column in visitors.iterate(access.predicate)
              if getattr(column, 'table', None) is not None
              and getattr(column.table, 'name', None) == 'maintenance_authorization_projects'}
    if len(tables) > 1:
        raise authz_error(503, 'AUTHZ_INVALID_SCOPE_PLAN', '项目查询计划包含不一致的关系表')
    if not tables:
        # An all/deny plan can compile to a constant. It still needs the same
        # relation projection, with the current principal and no role shortcut.
        table = project_relation_table(access.principal)
    else:
        table = tables.pop()
    return MaintenanceProject.project_id.in_(select(table.c.project_id).where(access.predicate))


def project_read_plan(db, identity, batch, ref, resource, action, *, read_snapshot=None, locked_user=None):
    principal = _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    bindings, project_id = project_query_bindings(principal, resource)
    access = service.context_query_access(db, identity, batch, ref, resource, action, bindings,
                                         read_snapshot=read_snapshot, locked_user=locked_user)
    return ProjectReadPlan(access, select(project_id).where(access.predicate))


def project_collection_revision(db, principal, ref):
    """Bind query output to current project and actual fact-parent generations."""
    from app.services.maintenance_authorization_relations import collection_relation_fingerprints
    return relation_revision({'carrier': ref.to_dict(),
        'relationships': collection_relation_fingerprints(db, ref.id),
        'actor': [principal.id, principal.authorization_epoch, principal.salesperson_name]})


def project_query(access, *, include_unassigned_when_all=False):
    """SQL set for project facts; only an unconditional PDP plan permits NULL.

    Existing all-project analysis includes unassigned source facts. A scope
    label alone never suppresses a conditional policy predicate.
    """
    from sqlalchemy import true
    if include_unassigned_when_all and access.scope == 'all_projects' and access.predicate.compare(true()):
        return None
    return select(MaintenanceProject.project_id).where(project_condition(access))


def project_read_capabilities(db, identity, project_ids, *, read_snapshot):
    """Batch narrow navigation checks without loading any project facts."""
    entry_id = 'GET /api/maintenance/projects/stable/{project_id}'
    principal, evidence = resolve_project_subjects(db, identity, sorted(set(project_ids)), read_snapshot=read_snapshot)
    contexts = []
    for project_id, item in evidence.items():
        ref = slot_ref(entry_id, 'context_0', project_id, selection={})
        contexts.append(resolved_context(db, entry_id=entry_id, branch='default', carrier=ref,
            slots={'context_0': (ref,)}, subjects={'maintenance_project': (item.subject,)},
            revision=relation_revision({'project': item.authorization_revision, 'carrier': ref.to_dict()})))
    if not contexts: return {}
    accesses = service.context_accesses(db, identity, create_context_batch(db, contexts), read_snapshot=read_snapshot)
    return {context.ref.id: accesses.get(context.ref, 'maintenance_project', 'read') is not None for context in contexts}


def _resolve_registered_project_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    principal = _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    contract = ENTRY_CONTRACTS.get(entry_id)
    selected = branch or ('default' if contract and 'default' in contract['branches'] else None)
    rule = contract and contract['branches'].get(selected)
    if rule is None:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '此入口需要真实输入选择授权分支')
    if rule['slots'] and all(contract['slots'][key]['kind'] == 'collection'
                              and contract['slots'][key]['authorizing'] for key in rule['slots']):
        slots = {}
        for key in rule['slots']:
            slot = contract['slots'][key]
            selection = {name: params[name] for name in slot['selector_schema']['properties'] if name in params}
            slots[key] = (slot_ref(entry_id, key, slot['collection_ref'], selection=selection),)
        carrier = slots[rule['carrier_slots'][0]][0]
        subjects = {resource: (() if get_resource_definition(resource).query_attributes else
                              (PolicySubject(carrier.id, {}),)) for resource in rule['requirements']}
        context = resolved_context(db, entry_id=entry_id, branch=selected, carrier=carrier, slots=slots,
            subjects=subjects, revision=project_collection_revision(db, principal, carrier))
        return create_context_batch(db, [context])
    if not rule['slots'] or any(contract['slots'][key]['kind'] != 'project' for key in rule['slots']):
        raise authz_error(503, 'AUTHZ_DOMAIN_RESOLVER_UNAVAILABLE', '领域对象解析器尚未注册')
    project_id = params.get('project_id')
    evidence = project_evidence(db, principal, [project_id])[project_id]
    slots = {key: (slot_ref(entry_id, key, project_id, selection={name: params[name]
        for name in contract['slots'][key]['selector_schema']['properties'] if name in params}),)
        for key in rule['slots']}
    carrier = slots[rule['carrier_slots'][0]][0]
    subjects = {}
    for resource in rule['requirements']:
        attributes = get_resource_definition(resource).query_attributes
        if attributes:
            subjects[resource] = (evidence.subject,)
        else:
            subjects[resource] = (PolicySubject(project_id, {}),)
    context = resolved_context(db, entry_id=entry_id, branch=selected, carrier=carrier,
                               slots=slots, subjects=subjects, revision=relation_revision({
                                   'project': evidence.authorization_revision, 'carrier': carrier.to_dict()}))
    return create_context_batch(db, [context])


def _resolve_registered_project_reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    selection = ref.selector.get('selection', {})
    if not isinstance(selection, Mapping) or 'project_id' in selection:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史选择器无效')
    if ref.kind == 'project':
        params = {**selection, 'project_id': ref.id}
    elif ref.kind == 'collection':
        contract = ENTRY_CONTRACTS.get(entry_id, {})
        rule = contract.get('branches', {}).get(branch, {})
        if not rule.get('slots') or any(contract['slots'][key]['kind'] != 'collection' for key in rule['slots']):
            raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史集合不能替代对象或输入证据')
        params = dict(selection)
    else:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史对象类型与项目合同不匹配')
    return resolve_entry_context(db, identity, entry_id=entry_id, params=params, branch=branch,
                                 read_snapshot=read_snapshot, locked_user=locked_user)


def _registered_project_query_bindings(db, principal, context, resource):
    return project_query_bindings(principal, resource)[0]

for _registered_id in (
    'GET /api/maintenance/analytics/explorer',
    'GET /api/maintenance/analytics/filter-options',
    'GET /api/maintenance/analytics/maintenance-dashboard/consumption',
    'GET /api/maintenance/analytics/maintenance-dashboard/consumption/issues',
    'GET /api/maintenance/analytics/maintenance-dashboard/export',
    'GET /api/maintenance/analytics/maintenance-dashboard/options',
    'GET /api/maintenance/analytics/maintenance-dashboard/orders',
    'GET /api/maintenance/analytics/maintenance-dashboard/parts',
    'GET /api/maintenance/analytics/maintenance-dashboard/projects',
    'GET /api/maintenance/analytics/maintenance-dashboard/quantity-checks',
    'GET /api/maintenance/analytics/maintenance-dashboard/receipts',
    'GET /api/maintenance/analytics/maintenance-dashboard/summary',
    'GET /api/maintenance/analytics/maintenance-dashboard/valuation',
    'GET /api/maintenance/analytics/pn-ranking',
    'GET /api/maintenance/analytics/spend-trend',
    'GET /api/maintenance/as-of',
    'GET /api/maintenance/board',
    'GET /api/maintenance/board/export',
    'GET /api/maintenance/boss-board/attention',
    'GET /api/maintenance/boss-board/health',
    'GET /api/maintenance/boss-board/projects',
    'GET /api/maintenance/boss-board/projects/export/options',
    'GET /api/maintenance/boss-board/projects/{project_id}',
    'GET /api/maintenance/boss-board/projects/{project_id}/orders',
    'GET /api/maintenance/boss-board/summary',
    'GET /api/maintenance/export',
    'GET /api/maintenance/export-workbook',
    'GET /api/maintenance/export-workbooks',
    'GET /api/maintenance/lines',
    'GET /api/maintenance/lines/export',
    'GET /api/maintenance/match-audit',
    'GET /api/maintenance/orders/export',
    'GET /api/maintenance/project-assignments/orders',
    'GET /api/maintenance/project-batch-transfer/options',
    'GET /api/maintenance/projects',
    'GET /api/maintenance/projects/stable',
    'GET /api/maintenance/projects/stable/business-type-backfill/preview',
    'GET /api/maintenance/projects/stable/operations',
    'GET /api/maintenance/projects/stable/{project_id}',
    'GET /api/maintenance/projects/stable/{project_id}/acceptance',
    'GET /api/maintenance/projects/stable/{project_id}/acceptance/checklist',
    'GET /api/maintenance/projects/stable/{project_id}/acceptance/checklist/template',
    'GET /api/maintenance/projects/stable/{project_id}/collection-milestones',
    'GET /api/maintenance/projects/stable/{project_id}/cost-gaps',
    'GET /api/maintenance/projects/stable/{project_id}/expense-collection-workbook.xlsx',
    'GET /api/maintenance/projects/stable/{project_id}/expense-rows',
    'GET /api/maintenance/projects/stable/{project_id}/front-stock',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook.xlsx',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook/collection-plan',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook/row-fields',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook/rows',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook/rows/export',
    'GET /api/maintenance/projects/stable/{project_id}/purchases',
    'GET /api/maintenance/projects/stable/{project_id}/recovery-summary',
    'GET /api/maintenance/projects/stable/{project_id}/tasks',
    'GET /api/maintenance/projects/stable/{project_id}/workbook',
    'GET /api/maintenance/projects/stable/{project_id}/workbook-v3.xlsx',
    'GET /api/maintenance/projects/stable/{project_id}/workspace',
    'GET /api/maintenance/reconcile/expenses',
    'GET /api/maintenance/roundtrip-template',
    'GET /api/maintenance/roundtrip-templates',
    'GET /api/maintenance/spare-part-lines.xlsx',
    'GET /api/maintenance/spare-part-lines/export',
    'GET /api/maintenance/spare-part-lines/fields',
    'GET /api/maintenance/spare-part-lines/rows',
    'GET /api/maintenance/wbdd-imports/latest',
    'GET /api/maintenance/wbdd-imports/latest/missing',
    'POST /api/maintenance/acceptance-deliverables/search',
    'POST /api/maintenance/boss-board/projects/export',
    'POST /api/maintenance/boss-board/projects/search',
    'POST /api/maintenance/collection-reminders/search',
    'POST /api/maintenance/project-batch-transfer/download',
    'POST /api/maintenance/project-manager-assignments/search',
    'POST /api/maintenance/projects/stable',
    'POST /api/maintenance/projects/stable/operations/search',
    'POST /api/maintenance/projects/stable/search',
    'POST /api/maintenance/projects/stable/{project_id}/acceptance/attachments',
    'POST /api/maintenance/projects/stable/{project_id}/acceptance/checklist/preview',
    'POST /api/maintenance/projects/stable/{project_id}/acceptance/submit',
    'POST /api/maintenance/projects/stable/{project_id}/expense-collection-workbook/apply',
    'POST /api/maintenance/projects/stable/{project_id}/expense-collection-workbook/validate',
):
    register_entry_resolver(_registered_id, _resolve_registered_project_context)

for _registered_id in (
    'GET /api/maintenance/analytics/explorer',
    'GET /api/maintenance/analytics/filter-options',
    'GET /api/maintenance/analytics/maintenance-dashboard/consumption',
    'GET /api/maintenance/analytics/maintenance-dashboard/consumption/issues',
    'GET /api/maintenance/analytics/maintenance-dashboard/export',
    'GET /api/maintenance/analytics/maintenance-dashboard/options',
    'GET /api/maintenance/analytics/maintenance-dashboard/orders',
    'GET /api/maintenance/analytics/maintenance-dashboard/parts',
    'GET /api/maintenance/analytics/maintenance-dashboard/projects',
    'GET /api/maintenance/analytics/maintenance-dashboard/quantity-checks',
    'GET /api/maintenance/analytics/maintenance-dashboard/receipts',
    'GET /api/maintenance/analytics/maintenance-dashboard/summary',
    'GET /api/maintenance/analytics/maintenance-dashboard/valuation',
    'GET /api/maintenance/analytics/pn-ranking',
    'GET /api/maintenance/analytics/spend-trend',
    'GET /api/maintenance/as-of',
    'GET /api/maintenance/board',
    'GET /api/maintenance/board/export',
    'GET /api/maintenance/boss-board/attention',
    'GET /api/maintenance/boss-board/health',
    'GET /api/maintenance/boss-board/projects',
    'GET /api/maintenance/boss-board/projects/export/options',
    'GET /api/maintenance/boss-board/projects/{project_id}',
    'GET /api/maintenance/boss-board/projects/{project_id}/orders',
    'GET /api/maintenance/boss-board/summary',
    'GET /api/maintenance/export',
    'GET /api/maintenance/export-workbook',
    'GET /api/maintenance/export-workbooks',
    'GET /api/maintenance/lines',
    'GET /api/maintenance/lines/export',
    'GET /api/maintenance/match-audit',
    'GET /api/maintenance/orders/export',
    'GET /api/maintenance/project-assignments/orders',
    'GET /api/maintenance/project-batch-transfer/options',
    'GET /api/maintenance/projects',
    'GET /api/maintenance/projects/stable',
    'GET /api/maintenance/projects/stable/business-type-backfill/preview',
    'GET /api/maintenance/projects/stable/operations',
    'GET /api/maintenance/projects/stable/{project_id}',
    'GET /api/maintenance/projects/stable/{project_id}/acceptance',
    'GET /api/maintenance/projects/stable/{project_id}/acceptance/checklist',
    'GET /api/maintenance/projects/stable/{project_id}/acceptance/checklist/template',
    'GET /api/maintenance/projects/stable/{project_id}/collection-milestones',
    'GET /api/maintenance/projects/stable/{project_id}/cost-gaps',
    'GET /api/maintenance/projects/stable/{project_id}/expense-collection-workbook.xlsx',
    'GET /api/maintenance/projects/stable/{project_id}/expense-rows',
    'GET /api/maintenance/projects/stable/{project_id}/front-stock',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook.xlsx',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook/collection-plan',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook/row-fields',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook/rows',
    'GET /api/maintenance/projects/stable/{project_id}/master-workbook/rows/export',
    'GET /api/maintenance/projects/stable/{project_id}/purchases',
    'GET /api/maintenance/projects/stable/{project_id}/recovery-summary',
    'GET /api/maintenance/projects/stable/{project_id}/tasks',
    'GET /api/maintenance/projects/stable/{project_id}/workbook',
    'GET /api/maintenance/projects/stable/{project_id}/workbook-v3.xlsx',
    'GET /api/maintenance/projects/stable/{project_id}/workspace',
    'GET /api/maintenance/projects/{project_id}/source-cancellations',
    'GET /api/maintenance/reconcile/expenses',
    'GET /api/maintenance/roundtrip-template',
    'GET /api/maintenance/roundtrip-templates',
    'GET /api/maintenance/spare-part-lines.xlsx',
    'GET /api/maintenance/spare-part-lines/export',
    'GET /api/maintenance/spare-part-lines/fields',
    'GET /api/maintenance/spare-part-lines/rows',
    'GET /api/maintenance/wbdd-imports/latest',
    'GET /api/maintenance/wbdd-imports/latest/missing',
    'PATCH /api/maintenance/projects/stable/{project_id}',
    'PATCH /api/maintenance/projects/stable/{project_id}/cost-gaps',
    'POST /api/maintenance/acceptance-deliverables/search',
    'POST /api/maintenance/boss-board/projects/export',
    'POST /api/maintenance/boss-board/projects/search',
    'POST /api/maintenance/collection-reminders/search',
    'POST /api/maintenance/project-batch-transfer/download',
    'POST /api/maintenance/project-manager-assignments/search',
    'POST /api/maintenance/projects/stable',
    'POST /api/maintenance/projects/stable/operations/search',
    'POST /api/maintenance/projects/stable/search',
    'POST /api/maintenance/projects/stable/{project_id}/acceptance/attachments',
    'POST /api/maintenance/projects/stable/{project_id}/acceptance/checklist/preview',
    'POST /api/maintenance/projects/stable/{project_id}/acceptance/submit',
    'POST /api/maintenance/projects/stable/{project_id}/archive',
    'POST /api/maintenance/projects/stable/{project_id}/collection-milestones/restore',
    'POST /api/maintenance/projects/stable/{project_id}/collections',
    'POST /api/maintenance/projects/stable/{project_id}/contracts',
    'POST /api/maintenance/projects/stable/{project_id}/cost-gaps/recompute',
    'POST /api/maintenance/projects/stable/{project_id}/expense-collection-workbook/apply',
    'POST /api/maintenance/projects/stable/{project_id}/expense-collection-workbook/validate',
    'POST /api/maintenance/projects/stable/{project_id}/expenses',
    'POST /api/maintenance/projects/stable/{project_id}/manager-assignment',
    'POST /api/maintenance/projects/stable/{project_id}/restore',
    'PUT /api/maintenance/projects/stable/{project_id}/expenses/readiness',
):
    register_reference_resolver(_registered_id, _resolve_registered_project_reference)

for _registered_id in (
    'maintenance_acceptance',
    'maintenance_acceptance_directory',
    'maintenance_analysis',
    'maintenance_assignments',
    'maintenance_collection',
    'maintenance_collection_followup',
    'maintenance_collection_followup_directory',
    'maintenance_contract',
    'maintenance_contract_amount_write',
    'maintenance_cost_fill',
    'maintenance_dashboard',
    'maintenance_demand',
    'maintenance_document_import_targets',
    'maintenance_document_relink',
    'maintenance_expense',
    'maintenance_finance_workbook',
    'maintenance_front_stock',
    'maintenance_general_workbook',
    'maintenance_general_workbook_write',
    'maintenance_global_rows',
    'maintenance_global_workbook',
    'maintenance_issue',
    'maintenance_managed_rows',
    'maintenance_managed_workbook',
    'maintenance_manager_workbook',
    'maintenance_operation_directory',
    'maintenance_project',
    'maintenance_project_backfill',
    'maintenance_project_cards',
    'maintenance_project_directory',
    'maintenance_project_operations',
    'maintenance_project_purchase',
    'maintenance_project_roundtrip',
    'maintenance_project_transfer',
    'maintenance_project_transfer_export',
    'maintenance_recovery',
    'maintenance_return_import',
    'maintenance_return_obligation',
    'maintenance_return_receipt',
    'maintenance_row_workbench',
    'maintenance_salvage',
    'maintenance_shipment_import_targets',
    'maintenance_source_assignment',
    'maintenance_source_cancellation',
    'maintenance_summary',
    'maintenance_wbdd_reconciliation',
    'replenishment_project_commands',
    'replenishment_project_directory',
):
    register_query_bindings(_registered_id, _registered_project_query_bindings)

del _registered_id
