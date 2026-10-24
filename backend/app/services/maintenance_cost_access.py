"""Protected source-sales cost queries and whole-contract projections.

The source predicate comes from the registered source-sales query family;
project manager/viewer relations never substitute for source ownership.
"""
from sqlalchemy import false, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors

from app.authz import service
from app.authz.engine import QueryBindings
from app.authz.errors import authz_error
from app.business_time import business_today
from app.models.maintenance import FMaintenanceOrder
from app.security import apply_field_visibility
from app.services import maintenance_authorization as authorization
from app.services import maintenance_cost
from app.services.maintenance_row_filter_registry import parse_filters

PROJECTS = 'GET /api/maintenance/projects'
LINES = 'GET /api/maintenance/lines'
BOARD = 'GET /api/maintenance/board'
SUMMARY_EXPORT = 'GET /api/maintenance/export'
LINES_EXPORT = 'GET /api/maintenance/lines/export'
BOARD_EXPORT = 'GET /api/maintenance/board/export'
_READS = {
    PROJECTS: ('maintenance_cost', 'read_projects'),
    LINES: ('maintenance_cost', 'read_lines'),
    BOARD: ('maintenance_contract_cost', 'read'),
    SUMMARY_EXPORT: ('maintenance_cost', 'export_summary'),
    LINES_EXPORT: ('maintenance_cost', 'export_lines'),
    BOARD_EXPORT: ('maintenance_contract_cost', 'export'),
}


def _source_table(principal):
    owns = (FMaintenanceOrder.salesperson == principal.salesperson_name
            if principal.salesperson_name else false())
    return select(FMaintenanceOrder.id.label('source_id'),
        func.coalesce(owns, false()).label('is_source_sales')).subquery('maintenance_authorization_sources')


def _source_bindings(db, principal, context, resource):
    table = _source_table(principal)
    return QueryBindings(resource, table, {'request.resource.attr.is_source_sales': table.c.is_source_sales})


def source_condition(access):
    tables = {column.table for column in visitors.iterate(access.predicate)
        if getattr(getattr(column, 'table', None), 'name', None) == 'maintenance_authorization_sources'}
    if len(tables) > 1:
        raise authz_error(503, 'AUTHZ_INVALID_SCOPE_PLAN', '来源查询计划包含不一致关系表')
    table = next(iter(tables)) if tables else _source_table(access.principal)
    return FMaintenanceOrder.id.in_(select(table.c.source_id).where(access.predicate))


def read(db, identity, *, entry_id, params, legacy_context, view=None, precondition=None, transform=None):
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知的授权视图')
    resource, action = _READS[entry_id]
    with authorization.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry_id, params=params) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if view == 'authorization':
            return service.context_resolution(accesses, ref)
        access = accesses.require(ref, resource, action)
        service.check_context_precondition(authorization.decode_precondition(precondition), accesses, ref)
        fields = service.field_context(access, legacy_context)
        if entry_id in (PROJECTS, SUMMARY_EXPORT):
            data = maintenance_cost.projects_aggregate(snapshot, params.get('date_from'), params.get('date_to'),
                params.get('q'), user_ctx=fields, lifecycle=params.get('lifecycle', 'ongoing'), as_of=business_today(),
                source_condition=source_condition(access), strict_projection=True,
                restrict_contract_scope=access.scope == 'own_source_sales')
        elif entry_id == LINES_EXPORT:
            args = (snapshot, params['project'], params.get('month'), params.get('date_from'), params.get('date_to'))
            options = {'user_ctx': fields, 'row_filters': parse_filters(params.get('filters')),
                'source_condition': source_condition(access), 'strict_projection': True}
            if not maintenance_cost.project_exists(snapshot, params['project'], user_ctx=fields,
                    source_condition=options['source_condition'], strict_projection=True):
                raise authz_error(404, 'MAINTENANCE_PROJECT_NOT_FOUND', '当前范围内项目不存在')
            count = maintenance_cost.project_line_count(*args, **options)
            if count == 0:
                raise authz_error(422, 'DOMAIN_INVALID', '项目存在，但所选范围内没有可导出的明细')
            if count > 1_000_000:
                raise authz_error(413, 'DOMAIN_EXPORT_LIMIT', 'CSV 数据行超过 1000000 行上限')
            data = {'rows': (apply_field_visibility(row, fields) for row in
                maintenance_cost.iter_project_lines(*args, **options))}
        elif entry_id == LINES:
            data = maintenance_cost.project_lines(snapshot, params['project'], params.get('month'),
                params.get('date_from'), params.get('date_to'), params.get('page', 1), params.get('page_size', 50),
                user_ctx=fields, row_filters=parse_filters(params.get('filters')),
                source_condition=source_condition(access), strict_projection=True)
        else:
            data = maintenance_cost.board(snapshot, params.get('date_from'), params.get('date_to'),
                params.get('status'), user_ctx=fields, lifecycle=params.get('lifecycle', 'ongoing'),
                as_of=business_today(), q_text=params.get('q'), strict_projection=True)
        if entry_id != LINES_EXPORT:
            data = apply_field_visibility(data, fields)
        data['authorization_context'] = accesses.wire(ref)
        return transform(data, fields, access) if transform is not None else data


authorization.register_query_bindings('maintenance_cost', _source_bindings)


def prepare_export(db, identity, *, entry_id, params, legacy_context, render, view=None, precondition=None):
    from app.authz.provenance import AccessCollector
    from app.services.authorization_downloads import guard_stream
    def materialize(data, fields, access):
        response = render(data, fields)
        collector = AccessCollector(access.principal.id, access.authorization_epoch, access.catalog_version)
        collector.record(access)
        return response, collector.snapshot()
    result = read(db, identity, entry_id=entry_id, params=params, legacy_context=legacy_context,
        view=view, precondition=precondition, transform=materialize)
    if view == 'authorization':
        return result
    response, provenance = result
    return guard_stream(response, db.get_bind(), identity, provenance)
