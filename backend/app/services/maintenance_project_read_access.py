"""Project facts with one resource's fields and a transaction-bound object proof."""
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.errors import authz_error
from app.services import maintenance_authorization as authorization
from app.services import maintenance_front_stock as front_stock
from app.services import maintenance_recovery as recovery
from app.services import maintenance_project_operations as operations

_COST_KEYS = ('unit_cost_ex_tax','unit_cost_inc_tax','value_ex_tax','value_inc_tax')


def front_stock_payload(db, project_id, *, include_ledger, can_view_cost):
    rows = front_stock.balance_rows(db, project_id)
    if not can_view_cost:
        for row in rows:
            for key in _COST_KEYS: row[key] = None
    ex, inc = [r['value_ex_tax'] for r in rows], [r['value_inc_tax'] for r in rows]
    complete_ex, complete_inc = all(v is not None for v in ex), all(v is not None for v in inc)
    result = {'project_id':project_id,'rows':rows,'total_qty':round(sum(r['qty'] for r in rows),3),
        'total_value_ex_tax':round(sum(ex),2) if can_view_cost and complete_ex else None,
        'total_value_inc_tax':round(sum(inc),2) if can_view_cost and complete_inc else None,
        'value_completeness':('complete' if complete_ex and complete_inc else 'incomplete') if can_view_cost else 'not_visible',
        'cost_visible':can_view_cost,'stale_90d_count':sum(1 for r in rows if r['stale_90d'])}
    if include_ledger: result['ledger'] = front_stock.ledger_entries(db,project_id)
    return result


def _read(db,identity,*,entry_id,resource,project_id,params,view,precondition,project,action='read'):
    if view not in (None,'authorization'):
        raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知的授权视图')
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=entry_id,params={'project_id':project_id,**params}) as (snapshot,_,batch,accesses):
        ref = batch.contexts[0].ref
        if view == 'authorization':
            return service.context_resolution(accesses, ref)
        access = accesses.require(ref,resource,action)
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        result = project(snapshot,access)
        return {**result,'authorization_context':accesses.wire(ref)}


def read_tasks(db, identity, project_id, *, as_of, legacy_context, view=None, precondition=None):
    def payload(snapshot, access):
        fields = service.field_context(access, legacy_context)
        workspace = operations.project_workspace(snapshot, project_id=project_id, as_of=as_of,
            user_ctx=fields, block_contexts={'operation_summary':fields})
        return {'project_id':project_id,'rows':workspace['reminders'],
                'total':len(workspace['reminders']),'as_of':as_of.isoformat(),
                'data_version':workspace['data_version']}
    return _read(db,identity,entry_id='GET /api/maintenance/projects/stable/{project_id}/tasks',
        resource='maintenance_project_operations',action='read_tasks',project_id=project_id,
        params={'as_of':as_of},view=view,precondition=precondition,project=payload)


def read_purchases(db, identity, project_id, *, source_order_id=None, page=None, page_size=None,
                   view=None, precondition=None):
    from app.services import maintenance_project_procurement as procurement
    def payload(snapshot,access):
        result=procurement.list_project_procurement(snapshot,project_id,
            source_order_id=source_order_id,page=page,page_size=page_size)
        masked='data_purchase_cost' not in access.field_permissions
        for order in result['rows']:
            for line in order.get('lines',[]):
                if masked: line['unit_price']=None
                line['unit_price_masked']=masked
        return {'project_id':project_id,'purchases':result['rows'],'total':result['total'],
                'page':result['page'],'page_size':result['page_size']}
    return _read(db,identity,entry_id='GET /api/maintenance/projects/stable/{project_id}/purchases',
        resource='maintenance_project_purchase',project_id=project_id,params={'source_order_id':source_order_id},
        view=view,precondition=precondition,project=payload)


def read_cost_gaps(db,identity,project_id,*,page=1,page_size=20,view=None,precondition=None):
    return _read(db,identity,entry_id='GET /api/maintenance/projects/stable/{project_id}/cost-gaps',
        resource='maintenance_cost_fill',action='read_gaps',project_id=project_id,params={},
        view=view,precondition=precondition,project=lambda snapshot,access:
            operations.list_cost_gaps(snapshot,project_id=project_id,page=page,page_size=page_size))


def read_front_stock(db,identity,project_id,*,include_ledger=False,view=None,precondition=None):
    return _read(db,identity,entry_id='GET /api/maintenance/projects/stable/{project_id}/front-stock',
        resource='maintenance_front_stock',project_id=project_id,params={'include_ledger':include_ledger},
        view=view,precondition=precondition,project=lambda snapshot,access:front_stock_payload(snapshot,project_id,
            include_ledger=include_ledger,can_view_cost='data_purchase_cost' in access.field_permissions))


def read_recovery(db,identity,project_id,*,view=None,precondition=None):
    def payload(snapshot,access):
        result = recovery.recovery_summary(snapshot,project_id)
        if 'data_purchase_cost' not in access.field_permissions:
            for row in result['remaining_stock']:
                for key in _COST_KEYS: row[key] = None
        return result
    return _read(db,identity,entry_id='GET /api/maintenance/projects/stable/{project_id}/recovery-summary',
        resource='maintenance_recovery',project_id=project_id,params={},view=view,precondition=precondition,project=payload)


WORKSPACE_BLOCKS = {
    'project_identity':('maintenance_project','read'),
    'operation_summary':('maintenance_project_operations','read_workspace'),
    'contracts':('maintenance_contract','read'),
    'collection_snapshots':('maintenance_collection','read_snapshots'),
    'requisitions':('maintenance_issue','read_workspace'),
    'approved_expenses':('maintenance_expense','read_workspace'),
}


def read_workspace(db,identity,project_id,*,legacy_context,as_of,view=None,precondition=None,**paging):
    if view not in (None,'authorization'):
        raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知的授权视图')
    entry='GET /api/maintenance/projects/stable/{project_id}/workspace'
    params={'project_id':project_id,'as_of':as_of,**paging}
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=entry,params=params) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view == 'authorization':
            return service.context_resolution(accesses, ref)
        block_contexts={block:service.field_context(access,legacy_context)
            for block,(resource,action) in WORKSPACE_BLOCKS.items()
            if (access:=accesses.get(ref,resource,action)) is not None}
        usable_blocks = [block for block in block_contexts if block != 'approved_expenses' or
            'data_profit' in accesses.get(ref,'maintenance_expense','read_workspace').field_permissions]
        if not usable_blocks:
            raise authz_error(403,'AUTHZ_FORBIDDEN','未获得任何工作区区块的读取权限')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        result=operations.project_workspace(snapshot,project_id=project_id,as_of=as_of,
            user_ctx=next(iter(block_contexts.values())),block_contexts=block_contexts,**paging)
        result['block_access']={}
        for block,(resource,action) in WORKSPACE_BLOCKS.items():
            access=accesses.get(ref,resource,action)
            allowed=access is not None and (block!='approved_expenses' or 'data_profit' in access.field_permissions)
            result['block_access'][block]={'effect':'allow' if allowed else 'deny','reason':None if allowed else
                {'code':'FIELD_NOT_GRANTED' if access else 'NOT_GRANTED','message':'当前账号无权读取此区块'}}
        return {**result,'authorization_context':accesses.wire(ref)}
