"""Boss-board projections share the scoped SQL and each resource's own fields."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.authz.context import PolicySubject, create_context_batch, relation_revision
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.authz import service
from app.authz.errors import authz_error
from app.services import maintenance_authorization as authorization
from app.services import maintenance_boss_board as board

_READS = {
    'orders': ('GET /api/maintenance/boss-board/projects/{project_id}/orders', 'maintenance_summary', 'read_orders'),
    'lines': ('GET /api/maintenance/boss-board/orders/{source_order_id}/lines', 'maintenance_summary', 'read_order_lines'),
    'export/options': ('GET /api/maintenance/boss-board/projects/export/options', 'maintenance_summary', 'read_export_options'),
    'health': ('GET /api/maintenance/boss-board/health', 'maintenance_source_health', 'read'),
    'summary': ('GET /api/maintenance/boss-board/summary', 'maintenance_summary', 'read_summary'),
    'attention': ('GET /api/maintenance/boss-board/attention', 'maintenance_summary', 'read_attention'),
    'projects': ('GET /api/maintenance/boss-board/projects', 'maintenance_project_cards', 'read'),
    'projects/search': ('POST /api/maintenance/boss-board/projects/search', 'maintenance_project_cards', 'read'),
    'project': ('GET /api/maintenance/boss-board/projects/{project_id}', 'maintenance_summary', 'read_project'),
}


def read(db, identity, *, endpoint, params, legacy_context, view=None, precondition=None):
    if endpoint not in _READS:
        raise ValueError('unknown board endpoint')
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知的授权视图')
    entry, resource, action = _READS[endpoint]
    selection = dict(params)
    for source, target in [('date_from','from'), ('date_to','to')]:
        if source in selection: selection[target] = selection.pop(source)
    with authorization.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry, params=selection, branch='unassigned' if endpoint=='orders' and params['project_id']==board.UNASSIGNED_BUCKET else None) as (snapshot, proof, batch, accesses):
        ref = batch.contexts[0].ref
        if batch.contexts[0].branch == 'unassigned':
            unassigned_access = accesses.require(ref, resource, action)
            if unassigned_access.scope != 'all_projects' or (endpoint=='orders' and authorization.project_query(unassigned_access, include_unassigned_when_all=True) is not None):
                raise authz_error(403, 'AUTHZ_FORBIDDEN', '未归属单据需要明确的全部项目范围')
        if view == 'authorization':
            return service.context_resolution(accesses, ref)
        access = accesses.require(ref, resource, action)
        service.check_context_precondition(authorization.decode_precondition(precondition), accesses, ref)
        context = service.field_context(access, legacy_context)
        scope = ({params['project_id']} if endpoint == 'project' else
                 authorization.project_query(access, include_unassigned_when_all=True) if endpoint not in ('health','lines','orders') else None)
        try:
            if endpoint == 'export/options':
                from app.services.maintenance_project_export import export_options
                result = export_options(context)
            elif endpoint == 'orders':
                result = board.project_orders(snapshot, user_ctx=context, strict_projection=True,
                    **{key:params[key] for key in ('project_id','page','page_size','contract_no') if key in params})
            elif endpoint == 'lines':
                result = board.order_lines(snapshot, user_ctx=context, strict_projection=True,
                    **{key:params[key] for key in ('source_order_id','page','page_size') if key in params})
            elif endpoint == 'health':
                result = board.health(snapshot)
            elif endpoint == 'summary':
                result = board.summary(snapshot, user_ctx=context, date_from=params.get('date_from'),
                    date_to=params.get('date_to'), allowed_project_ids=scope)
            elif endpoint == 'attention':
                result = board.attention(snapshot, user_ctx=context, limit=params.get('limit',10),
                    allowed_project_ids=scope, strict_projection=True)
            else:
                if endpoint == 'projects' and params.get('q') is not None:
                    raise authz_error(422, 'use_search_endpoint', '项目搜索请使用 POST /maintenance/boss-board/projects/search')
                query = {key:params[key] for key in ('page','page_size','lifecycle','sort','has_activity',
                    'date_from','date_to','business_type') if key in params}
                if endpoint == 'projects/search': query['q_text'] = params['q']
                if 'card_status' in params: query['card_status_filter'] = params['card_status']
                if endpoint == 'project': query.update(page=1,page_size=1,lifecycle='all')
                result = board.projects(snapshot, user_ctx=context, allowed_project_ids=scope,
                    strict_projection=True, **query)
                if endpoint == 'project':
                    result = next((row for row in result['rows'] if row['project_id']==params['project_id']),None)
                    if result is None: raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','项目不存在')
                    # Command abilities are obtained from their exact auth view;
                    # this read never manufactures a master-workbook write allow.
                    result['can_edit_master_workbook'] = None
        except board.BoardSortNotPermitted as exc:
            raise authz_error(422,'sort_requires_cost_permission','按成本排序需要本模块成本字段') from exc
        except board.BoardCostContractNotPermitted as exc:
            raise authz_error(422,'cost_contract_permission_required','此筛选需要本模块成本和合同字段') from exc
        return {**result, 'authorization_context': accesses.wire(ref)}


def _source_order_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    principal, _ = authorization.resolve_project_subjects(db, identity, [], read_snapshot=read_snapshot, locked_user=locked_user)
    source_id = params.get('source_order_id')
    order = db.execute(select(FMaintenanceOrder.id, FMaintenanceOrder.raw_order_id,
        FMaintenanceOrder.import_batch_id, FMaintenanceOrder.created_at).where(
        FMaintenanceOrder.raw_order_id == source_id)).one_or_none()
    if order is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '单据不存在')
    assignments = list(db.execute(select(MaintenanceSourceOrderAssignment.assignment_id,
        MaintenanceSourceOrderAssignment.project_id, MaintenanceSourceOrderAssignment.is_active,
        MaintenanceSourceOrderAssignment.version, MaintenanceSourceOrderAssignment.archived_at)
        .where(MaintenanceSourceOrderAssignment.source_order_id == source_id)
        .order_by(MaintenanceSourceOrderAssignment.assignment_id)))
    parents = sorted({row.project_id for row in assignments if row.is_active})
    selected = 'default' if parents else 'unassigned'
    evidence = authorization.project_evidence(db, principal, parents)
    carrier = authorization.slot_ref(entry_id, 'context_0', source_id)
    subjects = tuple(item.subject for item in evidence.values()) if parents else (
        PolicySubject(source_id, dict(is_manager=False,is_viewer=False,is_sales=False,is_active=False)),)
    context = authorization.resolved_context(db, entry_id=entry_id, branch=selected,
        carrier=carrier, slots={'context_0':(carrier,)}, subjects={'maintenance_summary':subjects},
        revision=relation_revision({'carrier':carrier.to_dict(),'source':[str(v) for v in order],
            'assignments':[[str(v) for v in row] for row in assignments],
            'parents':{pid:item.authorization_revision for pid,item in evidence.items()},
            'actor':[principal.id,principal.authorization_epoch,principal.salesperson_name]}))
    return create_context_batch(db,[context])


def _source_order_reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    if ref.kind != 'source_order':
        raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','单据来源类型无效')
    return _source_order_context(db,identity,entry_id=entry_id,params={'source_order_id':ref.id},
        branch=branch,read_snapshot=read_snapshot,locked_user=locked_user)


authorization.register_entry_resolver(_READS['lines'][0],_source_order_context,reference_resolver=_source_order_reference)


EXPORT = 'POST /api/maintenance/boss-board/projects/export'


def prepare_export(db, identity, *, params, legacy_context, view=None, precondition=None):
    from app.authz.provenance import AccessCollector, validate_provenance
    from app.services import maintenance_project_export as exporter
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知的授权视图')
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=EXPORT,params=params) as (snapshot,proof,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':
            return service.context_resolution(accesses,ref)
        access=accesses.require(ref,'maintenance_summary','export')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        context=service.field_context(access,legacy_context)
        content,count=exporter.build_project_export(snapshot,user_ctx=context,field_keys=params['fields'],
            q_text=params.get('q'),lifecycle=params.get('lifecycle','all'),card_status=params.get('card_status'),
            sort=params.get('sort','name'),business_type=params.get('business_type','all'),
            allowed_project_ids=authorization.project_query(access,include_unassigned_when_all=True),strict_projection=True)
        collector=AccessCollector(access.principal.id,access.authorization_epoch,access.catalog_version)
        collector.record(access)
        provenance=collector.snapshot()
    # Bytes are materialized in the authorized snapshot; recheck current rights
    # and complete relations before releasing this one-body response.
    with Session(db.get_bind()) as current:
        validate_provenance(current,identity,provenance)
    return content,count
