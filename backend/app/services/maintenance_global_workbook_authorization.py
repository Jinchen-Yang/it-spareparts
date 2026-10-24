"""Global source-cost files keep their complete file and query contracts."""
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.errors import authz_error
from app.authz.provenance import AccessCollector
from app.services import maintenance_authorization as foundation
from app.services import maintenance_cost_access as source_access
from app.services import maintenance_export as orders
from app.services import maintenance_workbook_export as workbooks
from app.services import maintenance_roundtrip as roundtrip
from app.services.authorization_downloads import attachment_headers, stream_materialized_file

BASE = '/api/maintenance'
ORDERS = 'GET ' + BASE + '/orders/export'
WORKBOOK = 'GET ' + BASE + '/export-workbook'
WORKBOOKS = 'GET ' + BASE + '/export-workbooks'
TEMPLATE = 'GET ' + BASE + '/roundtrip-template'
TEMPLATES = 'GET ' + BASE + '/roundtrip-templates'
RECOMPUTE = 'POST ' + BASE + '/recompute'
IMPORT = 'POST ' + BASE + '/roundtrip-import'
READS = {ORDERS: ('maintenance_cost', 'export_orders'),
         WORKBOOK: ('maintenance_cost', 'export_workbook'),
         WORKBOOKS: ('maintenance_cost', 'export_workbook'),
         TEMPLATE: ('maintenance_cost_roundtrip', 'template_download'),
         TEMPLATES: ('maintenance_cost_roundtrip', 'template_download')}


def _raise_export(exc):
    if isinstance(exc, (orders.ExcelExportBusy, workbooks.WorkbookExportBusy)):
        raise HTTPException(429, str(exc), headers={'Retry-After': '5'}) from exc
    if isinstance(exc, workbooks.WorkbookExportNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, orders.ExcelExportTooLarge):
        raise HTTPException(413, str(exc)) from exc
    if isinstance(exc, (orders.ExcelExportEmpty, orders.ExcelCellTooLong, orders.ExcelRowLimitExceeded)):
        raise HTTPException(422, str(exc)) from exc
    if isinstance(exc, (workbooks.WorkbookExportRejected, roundtrip.RoundtripWorkbookError)):
        raise HTTPException(exc.status_code, str(exc),
            headers={'Retry-After': '5'} if exc.status_code == 429 else None) from exc
    raise exc


def read(db, identity, *, entry_id, params, legacy_context, view=None, precondition=None):
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知授权视图')
    resource, action = READS[entry_id]
    output = None
    try:
        with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
                entry_id=entry_id, params=params) as (snapshot, _, batch, accesses):
            ref = batch.contexts[0].ref
            # A contract budget workbook combines full contract revenue and
            # expenses. A subset of source-sales rows cannot authorize it.
            if entry_id in (WORKBOOK, WORKBOOKS):
                access = accesses.require(ref, resource, action)
                if access.scope != 'all_sources':
                    raise authz_error(403, 'AUTHZ_FORBIDDEN', '完整合同成本工作簿需要全部来源范围')
            if view == 'authorization':
                return JSONResponse(service.context_resolution(accesses, ref), headers={'Cache-Control': 'no-store'})
            access = accesses.require(ref, resource, action)
            service.check_context_precondition(foundation.decode_precondition(precondition), accesses, ref)
            dates = {name: params.get(name) for name in ('date_from', 'date_to')}
            if entry_id == ORDERS:
                fields = service.field_context(access, legacy_context)
                output = orders.build_workbook(snapshot, fields, **dates,
                    authorization_condition=source_access.source_condition(access), strict_projection=True)
            elif entry_id == WORKBOOK:
                output = workbooks.build_contract_workbook_file(snapshot, params['contract'], **dates)
            elif entry_id == WORKBOOKS:
                output = workbooks.build_contract_workbooks_zip(snapshot, **dates)
            elif entry_id == TEMPLATE:
                output = roundtrip.build_roundtrip_template(snapshot, **dates, contract=params.get('contract'),
                    blank=params.get('blank', False), exported_by=access.principal.username)
            else:
                output = roundtrip.build_roundtrip_template_bundle(snapshot, **dates, exported_by=access.principal.username)
            collector = AccessCollector(access.principal.id, access.authorization_epoch, access.catalog_version)
            collector.record(access)
            provenance = collector.snapshot()
        # No DB connection is retained by the complete spool's iterator.
        db.rollback()
        zipped = entry_id in (WORKBOOKS, TEMPLATES)
        return stream_materialized_file(output, db.get_bind(), identity, provenance,
            media_type='application/zip' if zipped else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers=attachment_headers('maintenance_' + action + ('.zip' if zipped else '.xlsx')))
    except BaseException as exc:
        if output is not None:
            output.close()
        _raise_export(exc)


# Global mutations retain the complete server-derived input before staging any
# writes. The final identity lock is acquired only after all business locks.
from sqlalchemy import select, text
from app import config
from app.authz.context import PolicySubject, create_context_batch, relation_revision, read_snapshot
from app.services import maintenance_cost
from app.services.maintenance_workflow_authorization import require_complete


def _recompute_topology(db):
    from app.models.maintenance import FMaintenanceOrder, FMaintenanceLine
    from app.models.maintenance_project import MaintenanceProject
    from app.models.maintenance_project_operations import MaintenanceSiteIssue, MaintenanceSiteIssueLine
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    from app.services.query_filters import active_orders
    sources = [list(row) for row in db.execute(active_orders(select(
        FMaintenanceOrder.id, FMaintenanceOrder.raw_order_id, FMaintenanceLine.id,
        FMaintenanceOrder.salesperson).join(FMaintenanceLine,
            FMaintenanceLine.order_id == FMaintenanceOrder.id).where(
                FMaintenanceLine.is_active.is_(True)).order_by(FMaintenanceLine.id), FMaintenanceOrder))]
    source_ids = sorted({row[1] for row in sources})
    relationships = [list(row) for row in db.execute(select(MaintenanceSourceOrderAssignment.source_order_id,
        MaintenanceSourceOrderAssignment.project_id, MaintenanceSourceOrderAssignment.assignment_id,
        MaintenanceSourceOrderAssignment.version).where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(source_ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True)).order_by(
                MaintenanceSourceOrderAssignment.source_order_id, MaintenanceSourceOrderAssignment.assignment_id))]
    sites = [list(row) for row in db.execute(select(MaintenanceSiteIssue.project_id,
        MaintenanceSiteIssue.issue_id, MaintenanceSiteIssueLine.issue_line_id).join(MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id).join(MaintenanceProject,
                MaintenanceProject.project_id == MaintenanceSiteIssue.project_id).where(
                    MaintenanceProject.is_active.is_(True), MaintenanceSiteIssue.status_mapping_state == 'mapped',
                    MaintenanceSiteIssue.normalized_status.in_(('confirmed', 'corrected')),
                    MaintenanceSiteIssueLine.is_active.is_(True)).order_by(
                        MaintenanceSiteIssue.project_id, MaintenanceSiteIssue.issue_id, MaintenanceSiteIssueLine.issue_line_id))]
    return {'sources': sources, 'relationships': relationships, 'sites': sites}


def _recompute_context(db, identity, *, entry_id=RECOMPUTE, params=None, branch=None,
                       read_snapshot=None, locked_user=None, principal=None):
    if entry_id != RECOMPUTE or params not in (None, {}):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '全局重算不接受客户端替换目标')
    principal = principal or foundation._named_principal(db, identity,
        read_snapshot=read_snapshot, locked_user=locked_user)
    topology = _recompute_topology(db)
    parent_ids = sorted({row[1] for row in topology['relationships']} | {row[0] for row in topology['sites']})
    evidence = foundation.project_evidence(db, principal, parent_ids)
    digest = relation_revision({'topology': topology,
        'parents': {pid: value.authorization_revision for pid, value in evidence.items()}})
    operation_id = relation_revision({'entry': RECOMPUTE, 'actor': principal.id,
        'epoch': principal.authorization_epoch, 'plan': digest})
    carrier = foundation.slot_ref(RECOMPUTE, 'context_1', operation_id,
        selection={'command_plan_sha256': digest})
    actual_branch = 'default' if topology['sources'] or topology['sites'] else 'empty'
    if branch not in (None, actual_branch):
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '重算目标已变化')
    context = foundation.resolved_context(db, entry_id=RECOMPUTE, branch=actual_branch,
        carrier=carrier, slots={'context_0': (foundation.slot_ref(RECOMPUTE, 'context_0',
            'maintenance_source_orders'),), 'context_1': (carrier,)},
        # The global operation always includes non-source-local work. An
        # own_source_sales grant can never stand in for all_sources, even empty.
        subjects={'maintenance_cost': (PolicySubject(operation_id, {'is_source_sales': False}),)},
        revision=relation_revision({'plan': digest, 'actor': [principal.id, principal.authorization_epoch]}),
        empty_target_set=actual_branch == 'empty')
    return create_context_batch(db, [context])


foundation.register_entry_resolver(RECOMPUTE, _recompute_context)


def recompute(db, identity, *, view=None, precondition=None):
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知授权视图')
    parsed = foundation.decode_precondition(precondition)
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
        batch = _recompute_context(snapshot, identity, read_snapshot=proof)
        accesses = service.context_accesses(snapshot, identity, batch, read_snapshot=proof)
        access = accesses.require(batch.contexts[0].ref, 'maintenance_cost', 'recompute')
        if access.scope != 'all_sources':
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '全局重算需要全部来源范围')
        if view == 'authorization':
            return JSONResponse(service.context_resolution(accesses, batch.contexts[0].ref),
                headers={'Cache-Control': 'no-store'})
        service.check_context_precondition(parsed, accesses, batch.contexts[0].ref)
        principal = accesses.principal
    try:
        db.rollback()
        if db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),
                {'key': config.DATA_CHANGE_ADVISORY_LOCK_KEY}) is not True:
            raise maintenance_cost.MaintenanceCostRecomputeBusy('维保数据导入或另一轮成本重算正在进行，请稍后重试')
        from app.services import maintenance_project_operations
        before = _recompute_topology(db)
        parents = {row[1] for row in before['relationships']} | {row[0] for row in before['sites']}
        maintenance_project_operations.lock_workbook_states(db, project_ids=parents)
        captured = _recompute_context(db, identity, principal=principal)
        transaction = db.get_transaction()
        result = maintenance_cost.recompute(db, commit=False)
        if db.get_transaction() is not transaction or _recompute_topology(db) != before:
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '重算领域关系已变化，请重新预览')
        actor = service.lock_actor(db, identity)
        final = service.context_accesses(db, identity, captured, locked_user=actor)
        require_complete(final)
        final.require(captured.contexts[0].ref, 'maintenance_cost', 'recompute')
        service.check_context_precondition(parsed, final, captured.contexts[0].ref)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
