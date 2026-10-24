"""Site-issue grants shared by transport, reads and complete domain commands.

Commands keep the existing domain lock/receipt pipeline. A stable pre-write
context closes input OCC; all domain effects remain uncommitted until the final
live actor SHARE and PDP check. No actor lock precedes downstream obligation
projection, which can acquire additional domain locks.
"""
import json
from sqlalchemy import func,select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.authz import service
from app.authz.context import create_context_batch,relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project_operations import MaintenanceSiteIssue as Issue,MaintenanceSiteIssueLine as Line
from app.models.maintenance_project_operations import MaintenanceSiteIssueDeliverySource as Delivery
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as Assignment
from app.services import maintenance_authorization as authorization
from app.services.authorization_artifacts import stable_operation_key
from app.services import maintenance_project_operations as business
from app.services import maintenance_manual_site_issue as manual
from app.services import maintenance_site_issue_line_correction as correction
from app.services import maintenance_return_receipts as receipts

RESOURCE='maintenance_issue'
PREFIX='/api/maintenance/site-issues'
SEARCH='POST '+PREFIX+'/search'
CANDIDATES='POST '+PREFIX+'/projects/{project_id}/candidates/search'
DEMANDS='GET '+PREFIX+'/projects/{project_id}/demand-candidates'
PREVIEW='POST '+PREFIX+'/{issue_id}/preview'
MANUAL_PREVIEW='POST '+PREFIX+'/manual/preview'
CREATE='POST /api/maintenance/projects/stable/{project_id}/site-issues'
DRAFT='POST '+PREFIX+'/projects/{project_id}'
CONFIRM='POST '+PREFIX+'/{issue_id}/confirm'
UPDATE='PATCH '+PREFIX+'/{issue_id}'
VOID='POST '+PREFIX+'/{issue_id}/void'
STATUS='PATCH /api/maintenance/projects/stable/site-issues/{issue_id}/status'
MANUAL_CREATE='POST '+PREFIX+'/projects/{project_id}/manual'
MANUAL_UPDATE='PATCH '+PREFIX+'/manual/{issue_id}'
HEADER='PATCH '+PREFIX+'/{issue_id}/header'
LINE='PATCH '+PREFIX+'/{issue_id}/lines/{issue_line_id}'
READS={SEARCH:'read_search',CANDIDATES:'read_candidates',DEMANDS:'read_demand_candidates',
    PREVIEW:'preview',MANUAL_PREVIEW:'preview_manual'}
WRITES={CREATE:('create',business.create_site_issue),DRAFT:('create',business.create_site_issue_draft),
    CONFIRM:('confirm',business.confirm_site_issue),UPDATE:('update',business.patch_site_issue),
    VOID:('void',business.void_site_issue),STATUS:('update_status',business.update_site_issue_status),
    MANUAL_CREATE:('create_manual',manual.create_manual_site_issue),MANUAL_UPDATE:('update_manual',manual.patch_manual_site_issue),
    HEADER:('correct_header',correction.correct_site_issue_header),LINE:('correct_line',correction.correct_site_issue_line)}


def _context(db,principal,entry_id,params):
    project_id=params.get('project_id');child=None;line=None
    if issue_id:=params.get('issue_id'):
        row=db.execute(select(Issue.issue_id,Issue.project_id,Issue.version,Issue.normalized_status,Issue.source)
            .where(Issue.issue_id==issue_id)).one_or_none()
        if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','领用单不存在')
        if project_id is not None and project_id!=row.project_id:
            raise authz_error(403,'AUTHZ_FORBIDDEN','领用单不属于指定项目')
        project_id=row.project_id;child=dict(row._mapping)
    if line_id:=params.get('issue_line_id'):
        row=db.execute(select(Line.issue_line_id,Line.issue_id,Line.version,Line.is_active,Line.delivery_line_id)
            .where(Line.issue_line_id==line_id,Line.issue_id==params['issue_id'])).one_or_none()
        if row is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','领用明细不属于当前单据')
        line=dict(row._mapping)
    evidence=authorization.project_evidence(db,principal,[project_id])[project_id]
    schema=ENTRY_CONTRACTS[entry_id]['slots']['context_0']['selector_schema']
    selection=json.loads(json.dumps({key:params[key] for key in schema['properties'] if key in params},default=str))
    ref=authorization.slot_ref(entry_id,'context_0',child['issue_id'] if child else project_id,selection=selection)
    # Exact project-local metadata, never amounts or row descriptions. Source
    # reattachment and line identity changes invalidate view/formal pairing.
    relations={
        'sources':[tuple(row) for row in db.execute(select(Assignment.assignment_id,Assignment.source_order_id,
            Assignment.project_id,Assignment.version,Assignment.is_active).where(Assignment.project_id==project_id)
            .order_by(Assignment.assignment_id))],
        'deliveries':[tuple(row) for row in db.execute(select(Delivery.delivery_line_id,Delivery.source_order_id,
            Delivery.source_line_id,Delivery.project_id,Delivery.is_active,Delivery.mapping_state,Delivery.mapping_version)
            .where(Delivery.project_id==project_id).order_by(Delivery.delivery_line_id))],
        'lines':[tuple(row) for row in db.execute(select(Line.issue_line_id,Line.issue_id,Line.version,Line.is_active)
            .join(Issue,Issue.issue_id==Line.issue_id).where(Issue.project_id==project_id).order_by(Line.issue_line_id))]}
    context=authorization.resolved_context(db,entry_id=entry_id,branch='default',carrier=ref,
        slots={'context_0':(ref,)},subjects={RESOURCE:(evidence.subject,)},
        revision=relation_revision({'project':evidence.authorization_revision,'child':child,'line':line,
            'relations':relations,'carrier':ref.to_dict()}))
    return create_context_batch(db,[context])


def resolve(db,identity,*,entry_id,params,branch=None,read_snapshot=None,locked_user=None):
    principal=authorization._named_principal(db,identity,read_snapshot=read_snapshot,locked_user=locked_user)
    return _context(db,principal,entry_id,params)


def reference(db,identity,ref,*,entry_id,branch,actions_by_resource=None,read_snapshot=None,locked_user=None):
    params=dict(ref.selector.get('selection',{}))
    params['project_id' if ref.kind=='project' else 'issue_id']=ref.id
    return resolve(db,identity,entry_id=entry_id,params=params,read_snapshot=read_snapshot,locked_user=locked_user)


for _entry in (*READS,*WRITES):authorization.register_entry_resolver(_entry,resolve,reference_resolver=reference)


def _view(view):
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知授权视图')


def read(db,identity,*,entry_id,params,view=None,precondition=None):
    _view(view)
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry_id,params=params) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        accesses.require(ref,RESOURCE,READS[entry_id])
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        kwargs=dict(params)
        if entry_id==SEARCH:
            kwargs['q_text']=kwargs.pop('q',None);data=business.search_site_issues(snapshot,**kwargs)
        elif entry_id==CANDIDATES:
            kwargs['q_text']=kwargs.pop('q',None);data=business.search_site_issue_candidates(snapshot,**kwargs)
        elif entry_id==DEMANDS:
            data=receipts.demand_candidates(snapshot,**kwargs)
            orders={r.raw_order_id:r for r in snapshot.execute(select(FMaintenanceOrder.raw_order_id,
                FMaintenanceOrder.receiver_address,FMaintenanceOrder.receiver,FMaintenanceOrder.receiver_phone)
                .where(FMaintenanceOrder.raw_order_id.in_([row['source_order_id'] for row in data['rows']])))}
            data={**data,'rows':[{**row,'contact_info_state':'visible' if row['source_order_id'] in orders else 'restricted',
                **{key:getattr(orders.get(row['source_order_id']),key,None) for key in ('receiver_address','receiver','receiver_phone')}}
                for row in data['rows']]}
        elif entry_id==PREVIEW:data=business.preview_site_issue(snapshot,**kwargs)
        else:data=manual.preview_manual_site_issue(snapshot,**kwargs)
        if data is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','领用单或项目不存在')
        return {**data,'authorization_context':accesses.wire(ref)}


def execute(db,identity,*,entry_id,command,project_id=None,issue_id=None,issue_line_id=None,view=None,precondition=None):
    _view(view);action,operation=WRITES[entry_id]
    params={**command,**({ 'project_id':project_id} if project_id else {}),
        **({'issue_id':issue_id} if issue_id else {}),**({'issue_line_id':issue_line_id} if issue_line_id else {})}
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,entry_id=entry_id,params=params) as (_,_,batch,accesses):
        original=batch.contexts[0];ref=original.ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        initial=accesses.require(ref,RESOURCE,action)
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
    try:
        # Same outer order as assignment/workbook repairs. This is never an
        # authorization decision; it only stabilizes actual ownership inputs.
        db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
        scoped_key=stable_operation_key(initial.principal.id,'site_issue',command['idempotency_key']) if command.get('idempotency_key') else None
        if scoped_key:business._lock_idempotency_key(db,scoped_key)
        pid=project_id or command.get('project_id')
        if issue_id:
            actual=db.scalar(select(Issue.project_id).where(Issue.issue_id==issue_id))
            if actual is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','领用单不存在')
            if pid is not None and pid!=actual:raise authz_error(403,'AUTHZ_FORBIDDEN','领用单不属于指定项目')
            pid=actual
        if business._lock_project_for_fact_write(db,pid) is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','项目不存在')
        if issue_id:
            db.scalar(select(Issue).where(Issue.issue_id==issue_id).with_for_update().execution_options(populate_existing=True))
            list(db.scalars(select(Line).where(Line.issue_id==issue_id).order_by(Line.issue_line_id)
                .with_for_update().execution_options(populate_existing=True)))
        locked_context=_context(db,initial.principal,entry_id,params).contexts[0]
        if locked_context.authorization_revision!=original.authorization_revision:
            raise authz_error(409,'AUTHZ_CONTEXT_CHANGED','实际项目或领用关系已变化，请重新确认')
        kwargs={**command,**({'project_id':project_id} if project_id else {}),**({'issue_id':issue_id} if issue_id else {})}
        if issue_line_id:kwargs['issue_line_id']=issue_line_id
        if scoped_key:kwargs['idempotency_key']=scoped_key
        if entry_id in {HEADER,LINE}:
            # Existing primitive callback is a domain identity check only here.
            # Its full late authorization below runs after obligation locks.
            def operator_for(actual):
                if actual!=pid:raise authz_error(409,'AUTHZ_CONTEXT_CHANGED','领用归属已变化')
                return initial.principal.username
            kwargs['authorize']=operator_for
        else:kwargs['operated_by']=initial.principal.username
        if entry_id==CREATE:kwargs.update(source='direct_api',import_batch_id=None)
        data=operation(db,**kwargs)
        if data is None:raise authz_error(404,'AUTHZ_OBJECT_NOT_FOUND','领用单或项目不存在')
        db.flush()  # All original domain/FK locks and pending effects precede actor.
        actor,_=authorization.lock_final_users(db,identity)
        current=resolve(db,identity,entry_id=entry_id,params=params,locked_user=actor)
        final=service.context_accesses(db,identity,current,locked_user=actor)
        final.require(current.contexts[0].ref,RESOURCE,action)
        result={**data,'authorization_context':final.wire(current.contexts[0].ref)}
        db.commit();return result
    except IntegrityError as exc:
        db.rollback();raise authz_error(409,'DOMAIN_CONFLICT','领用事实或幂等命令冲突') from exc
    except Exception:db.rollback();raise
