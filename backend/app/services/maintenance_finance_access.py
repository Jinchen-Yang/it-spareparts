"""Finance commands bind actual parents and recheck after all domain locks.

Primitive callbacks are server-only and run in the current transaction. They
never replace business validation, commit, or acquire further domain locks.
"""
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.authz.principal import VerifiedIdentity
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot, MaintenanceProjectWorkbookState, MaintenanceProjectExpenseAttribution, MaintenanceSiteIssue, MaintenanceSiteIssueLine
from app.services import maintenance_authorization as authorization
from app.services import maintenance_project_operations as operations

CONTRACT_CREATE='POST /api/maintenance/projects/stable/{project_id}/contracts'
CONTRACT_UPDATE='PATCH /api/maintenance/projects/stable/contracts/{project_contract_id}'
CONTRACT_ARCHIVE='POST /api/maintenance/projects/stable/contracts/{project_contract_id}/archive'
COLLECTION_CREATE='POST /api/maintenance/projects/stable/{project_id}/collections'
COLLECTION_UPDATE='PATCH /api/maintenance/projects/stable/collections/{collection_id}'
EXPENSE_CREATE='POST /api/maintenance/projects/stable/{project_id}/expenses'
EXPENSE_READY='PUT /api/maintenance/projects/stable/{project_id}/expenses/readiness'
EXPENSE_STATUS='PATCH /api/maintenance/projects/stable/expenses/{expense_id}/status'
COST_FILL='PATCH /api/maintenance/projects/stable/{project_id}/cost-gaps'
COST_RECOMPUTE='POST /api/maintenance/projects/stable/{project_id}/cost-gaps/recompute'
_COMMANDS={CONTRACT_CREATE:('maintenance_contract','create',operations.create_contract),
    CONTRACT_UPDATE:('maintenance_contract','update',operations.update_contract),
    CONTRACT_ARCHIVE:('maintenance_contract','archive',operations.archive_contract),EXPENSE_STATUS:('maintenance_expense','update_status',operations.update_expense_status),
    COST_FILL:('maintenance_cost_fill','fill',operations.fill_manual_cost),
    COST_RECOMPUTE:('maintenance_cost_fill','recompute',operations.recompute_cost_gaps),COLLECTION_CREATE:('maintenance_collection','create',operations.create_collection),
    COLLECTION_UPDATE:('maintenance_collection','update',operations.update_collection),
    EXPENSE_CREATE:('maintenance_expense','create',operations.create_expense),
    EXPENSE_READY:('maintenance_expense','mark_readiness',operations.mark_expense_readiness)}


def _resolve(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    project_id=params.get('project_id');carrier_id=project_id;child=None;contract_id=params.get('project_contract_id')
    if entry_id in {CONTRACT_UPDATE,CONTRACT_ARCHIVE}:
        row=db.execute(select(MaintenanceProjectContract.project_contract_id,
            MaintenanceProjectContract.project_id,MaintenanceProjectContract.version).where(
                MaintenanceProjectContract.project_contract_id==params['project_contract_id'])).one_or_none()
        if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','合同关系不存在')
        child=dict(row._mapping);project_id=row.project_id;carrier_id=row.project_contract_id;contract_id=row.project_contract_id
    if entry_id==COLLECTION_UPDATE:
        row=db.execute(select(MaintenanceCollectionSnapshot.collection_id,
            MaintenanceCollectionSnapshot.project_id,MaintenanceCollectionSnapshot.project_contract_id,
            MaintenanceCollectionSnapshot.version).where(MaintenanceCollectionSnapshot.collection_id==params['collection_id'])).one_or_none()
        if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','回款记录不存在')
        child=dict(row._mapping);project_id=row.project_id;carrier_id=row.collection_id;contract_id=row.project_contract_id
    if entry_id==EXPENSE_STATUS:
        row=db.execute(select(MaintenanceProjectExpenseAttribution.expense_id,
            MaintenanceProjectExpenseAttribution.project_id,MaintenanceProjectExpenseAttribution.project_contract_id,
            MaintenanceProjectExpenseAttribution.version).where(
                MaintenanceProjectExpenseAttribution.expense_id==params['expense_id'])).one_or_none()
        if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','报销记录不存在')
        child=dict(row._mapping);project_id=row.project_id;carrier_id=row.expense_id;contract_id=row.project_contract_id
    if entry_id==COST_FILL:
        row=db.execute(select(MaintenanceSiteIssueLine.issue_line_id,MaintenanceSiteIssueLine.version,
            MaintenanceSiteIssue.issue_id,MaintenanceSiteIssue.project_id,MaintenanceSiteIssue.version.label('issue_version'))
            .join(MaintenanceSiteIssue,MaintenanceSiteIssue.issue_id==MaintenanceSiteIssueLine.issue_id)
            .where(MaintenanceSiteIssueLine.issue_line_id==params['line_id'],
                   MaintenanceSiteIssue.project_id==project_id)).one_or_none()
        if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','领用行不存在或不属于当前项目')
        child=dict(row._mapping)
    principal,evidence=authorization.resolve_project_subjects(db,identity,[project_id],
        read_snapshot=read_snapshot,locked_user=locked_user)
    contract=None
    if contract_id:
        row=db.execute(select(MaintenanceProjectContract.project_contract_id,MaintenanceProjectContract.project_id,
            MaintenanceProjectContract.version).where(MaintenanceProjectContract.project_contract_id==contract_id)).one_or_none()
        if row is None or row.project_id!=project_id:
            raise authz_error(409,'AUTHZ_CONTEXT_CHANGED','合同已不存在或不属于当前项目')
        contract=dict(row._mapping)
    state=None
    if entry_id in {EXPENSE_READY,COST_FILL,COST_RECOMPUTE}:
        state=int(db.scalar(select(MaintenanceProjectWorkbookState.revision).where(MaintenanceProjectWorkbookState.project_id==project_id)) or 0)
    rule=ENTRY_CONTRACTS[entry_id];schema=rule['slots']['context_0']['selector_schema']
    selection=json.loads(json.dumps({key:params[key] for key in schema['properties'] if key in params},default=str))
    carrier=authorization.slot_ref(entry_id,'context_0',carrier_id,selection=selection)
    resource=_COMMANDS[entry_id][0]
    context=authorization.resolved_context(db,entry_id=entry_id,branch='default',carrier=carrier,
        slots={'context_0':(carrier,)},subjects={resource:(evidence[project_id].subject,)},
        revision=relation_revision({'project':evidence[project_id].authorization_revision,'carrier':carrier.to_dict(),
                                    'child':child,'contract':contract,'workbook_revision':state}))
    return create_context_batch(db,[context])


for _entry in _COMMANDS:
    authorization.register_entry_resolver(_entry,_resolve)


def execute(db,identity,*,entry_id,command,project_id=None,collection_id=None,expense_id=None,project_contract_id=None,view=None,precondition=None):
    if not isinstance(identity,VerifiedIdentity):
        raise authz_error(401,'AUTHN_SESSION_INVALID','需要真实验签身份')
    if entry_id not in _COMMANDS:raise ValueError('finance command is not registered')
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知的授权视图')
    resource,action,operation=_COMMANDS[entry_id]
    params={**command,**({'project_id':project_id} if project_id is not None else {'collection_id':collection_id,'expense_id':expense_id,'project_contract_id':project_contract_id})}
    # An initial read-only denial avoids executing domain commands for callers
    # with no action; the final callback remains the authority for committing.
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=entry_id,params=params) as (_,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':
            return service.context_resolution(accesses, ref)
        accesses.require(ref,resource,action)
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
    captured={}
    def authorize():
        actor,_=authorization.lock_final_users(db,identity)
        batch=authorization.resolve_entry_context(db,identity,entry_id=entry_id,params=params,locked_user=actor)
        accesses=service.context_accesses(db,identity,batch,locked_user=actor);ref=batch.contexts[0].ref
        accesses.require(ref,resource,action)
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        captured['authorization_context']=accesses.wire(ref)
    try:
        kwargs=dict(command)
        if entry_id==CONTRACT_UPDATE:
            kwargs={'version':kwargs.pop('version'),'reason':kwargs.pop('reason'),'updates':kwargs,'project_contract_id':project_contract_id}
        elif entry_id==CONTRACT_ARCHIVE:
            kwargs['project_contract_id']=project_contract_id
        elif entry_id==COLLECTION_UPDATE:
            kwargs={'version':kwargs.pop('version'),'reason':kwargs.pop('reason'),'updates':kwargs,'collection_id':collection_id}
        elif entry_id==EXPENSE_STATUS:
            kwargs['expense_id']=expense_id
        else:
            kwargs['project_id']=project_id
            if entry_id==COST_FILL:
                kwargs['issue_line_id']=kwargs.pop('line_id')
                kwargs['manual_unit_cost']=kwargs.pop('unit_cost_ex_tax')
        result=operation(db,**kwargs,operated_by=identity.username,_authorize=authorize)
        if result is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','经营事实或项目不存在')
        if 'authorization_context' not in captured:raise RuntimeError('finance primitive missed final authorization')
        result={**result,**captured};db.commit();return result
    except IntegrityError as exc:
        db.rollback();raise authz_error(409,'DOMAIN_CONFLICT','经营事实重复或冲突') from exc
    except operations.MaintenanceOperationConflict as exc:
        db.rollback();raise authz_error(409,'DOMAIN_CONFLICT',str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback();raise authz_error(400,'DOMAIN_INVALID',str(exc)) from exc
    except Exception:
        db.rollback();raise
