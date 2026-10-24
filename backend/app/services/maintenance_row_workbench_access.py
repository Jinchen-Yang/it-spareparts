"""Read-only row contracts: one complete grant, SQL scope and same-snapshot facts."""
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import read_snapshot
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.services import maintenance_authorization as foundation
from app.services import maintenance_row_workbench as workbench
from app.services.maintenance_master_workbook_access import complete_alternative
from app.services.maintenance_workflow_authorization import require_complete
from app.services.maintenance_row_filter_registry import parse_filters
from app.services import maintenance_project_master_workbook as master
from app.services.maintenance_expense_collection_workbook import WorkbookError

GLOBAL = '/api/maintenance/spare-part-lines'
PROJECT = '/api/maintenance/projects/stable/{project_id}/master-workbook'
RESOURCES = ('maintenance_global_rows', 'maintenance_managed_rows', 'maintenance_row_workbench')


def read_rows(db, identity, *, entry_id, params, legacy_context, view=None, precondition=None,
              page=1, page_size=20):
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知的授权视图')
    branches = [name for name, rule in ENTRY_CONTRACTS[entry_id]['branches'].items()
                if not rule.get('authorization_view_only')]
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
        accesses = complete_alternative(snapshot, identity, entry_id=entry_id, params=params,
                                        proof=proof, branches=branches)
        context = accesses.batch.contexts[0]
        ref = context.ref
        if view == 'authorization':
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(foundation.decode_precondition(precondition), accesses, ref)
        resource, actions = next(iter(context.requirements.items()))
        access = accesses.require(ref, resource, actions[0])
        project_id = params.get('project_id')
        direct = ref.kind == 'project'
        projection = workbench.project_projection() if direct else 'workbook'
        if entry_id.endswith(('/fields', '/row-fields')):
            result = {'fields': workbench.field_metadata(snapshot, legacy_context, project_id,
                projection=projection, direct_project=direct, authorization_access=access),
                'amount_filter_allowed': workbench.can_view_amounts(snapshot, legacy_context, project_id,
                    authorization_access=access)}
        else:
            is_export = entry_id.endswith('/export')
            try:
                window = None if direct else master.resolve_range(params.get('range', 'this_month'), params.get('from'), params.get('to'))
            except WorkbookError as exc:
                from fastapi import HTTPException
                raise HTTPException(422, {'code': exc.code, 'message': exc.message}) from exc
            stmt = workbench.query(snapshot, legacy_context, project_id=project_id, window=window,
                filters=parse_filters(params.get('filters')), order_no=params.get('order_no'),
                contract_no=params.get('contract_no'), projection=projection, direct_project=direct,
                export_order=is_export, authorization_access=access)
            fields = workbench.visible_fields(snapshot, legacy_context, project_id, authorization_access=access)
            if is_export:
                # _download spools all rows before returning. The surrounding RR closes
                # before the first HTTP byte; no DB transaction reaches a slow client.
                from app.api.maintenance_row_workbench import _download
                return _download(snapshot, stmt, fields, params['format'], projection=projection,
                                 release_db=False)
            result = {'total': workbench.count(snapshot, stmt), 'page': page, 'page_size': page_size,
                'rows': list(workbench.read_rows(snapshot, stmt.offset((page - 1) * page_size).limit(page_size),
                                                fields, projection=projection))}
        return {**result, 'authorization_context': accesses.wire(ref)}
