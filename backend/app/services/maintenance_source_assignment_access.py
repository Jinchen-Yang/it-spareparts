"""Source-order access over the actual assignment SQL relation."""
from sqlalchemy.orm import Session

from app.authz import service
from app.services import maintenance_authorization as foundation
from app.services import maintenance_source_assignments as business
from app.services.maintenance_workflow_authorization import require_complete

DIRECTORY = 'GET /api/maintenance/project-assignments/orders'


def read_directory(db, identity, *, params, page, page_size, authorization_view=False, precondition=None):
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=DIRECTORY, params=params) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, ref)
        access = accesses.require(ref, 'maintenance_source_assignment', 'read')
        result = business.list_source_orders(snapshot, q_text=params['q'],
            source_order_ids=params['source_order_id'], assignment_status=params['assignment_status'],
            project_id=params['project_id'], include_candidates=params['include_candidates'],
            xsdd_project_id=params['xsdd_project_id'], page=page, page_size=page_size,
            user_ctx=None, authorization_access=access)
        return {**result, 'authorization_context': accesses.wire(ref)}


from sqlalchemy import select
from app.authz.context import PolicySubject, create_context_batch, relation_revision, read_snapshot
from app.authz.errors import authz_error
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment

ASSIGN = 'POST /api/maintenance/project-assignments/orders/assign'
UNASSIGN = 'POST /api/maintenance/project-assignments/orders/unassign'


def _command_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None,
                     locked_user=None, _principal=None):
    if entry_id not in (ASSIGN, UNASSIGN) or branch not in (None, 'default'):
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '未登记来源关系命令')
    if entry_id == ASSIGN:
        ids = [item['source_order_id'] for item in params['items']]
        relationships = list(db.scalars(select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True)).execution_options(populate_existing=True)))
        parents = {params['project_id'], *(row.project_id for row in relationships)}
    else:
        assignment_ids = [item['assignment_id'] for item in params['items']]
        relationships = list(db.scalars(select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.assignment_id.in_(assignment_ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True)).execution_options(populate_existing=True)))
        if len(relationships) != len(assignment_ids):
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '来源关系已变化')
        ids = [row.source_order_id for row in relationships]
        parents = {row.project_id for row in relationships}
    if not ids or len(ids) != len(set(ids)) or not parents:
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '来源命令目标集合无效')
    from app.services.query_filters import active_beta_maintenance_orders
    source_ids = set(db.scalars(active_beta_maintenance_orders(select(FMaintenanceOrder.raw_order_id).where(
        FMaintenanceOrder.raw_order_id.in_(ids)), FMaintenanceOrder)))
    if source_ids != set(ids):
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '来源单不存在')
    if _principal is None:
        principal, projects = foundation.resolve_project_subjects(db, identity, sorted(parents),
            read_snapshot=read_snapshot, locked_user=locked_user)
    else:
        principal = _principal
        projects = foundation.project_evidence(db, principal, sorted(parents))
    sources = tuple(foundation.slot_ref(entry_id, 'context_0', value) for value in sorted(ids))
    parent_refs = tuple(foundation.slot_ref(entry_id, 'context_1', value) for value in sorted(parents))
    # This write resource has only the explicit all scope. Still retain every
    # actual source and old/new parent as separate conjunctive policy subjects.
    subjects = tuple(PolicySubject(value, {}) for value in sorted(set(ids) | parents))
    context = foundation.resolved_context(db, entry_id=entry_id, branch='default', carrier=sources[0],
        slots={'context_0': sources, 'context_1': parent_refs},
        subjects={'maintenance_source_assignment_write': subjects},
        revision=relation_revision({'command': params, 'actor': [principal.id, principal.authorization_epoch],
            'projects': {pid: evidence.authorization_revision for pid, evidence in projects.items()},
            'relationships': sorted([row.source_order_id, row.assignment_id, row.version, row.project_id]
                                    for row in relationships)}))
    return create_context_batch(db, [context])


for entry in (ASSIGN, UNASSIGN):
    foundation.register_entry_resolver(entry, _command_context)


class SourceWriteAuthorization:
    """Internal callbacks bound to one parsed command and current transaction."""
    def __init__(self, identity, entry_id, params, principal, precondition):
        self.identity, self.entry_id, self.params = identity, entry_id, params
        self.principal, self.precondition = principal, precondition
        self.batch = None
        self.transaction = None

    def capture(self, db):
        self.transaction = db.get_transaction()
        self.batch = _command_context(db, self.identity, entry_id=self.entry_id,
            params=self.params, _principal=self.principal)

    def finish(self, db):
        if self.batch is None or self.transaction is not db.get_transaction():
            raise ValueError('来源关系最终授权缺少同事务锁内领域上下文')
        actor = service.lock_actor(db, self.identity)
        accesses = service.context_accesses(db, self.identity, self.batch, locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(self.precondition, accesses, self.batch.contexts[0].ref)
        return accesses


def write_command(db, identity, *, entry_id, params, authorization_view=False, precondition=None):
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
        batch = _command_context(snapshot, identity, entry_id=entry_id, params=params, read_snapshot=proof)
        accesses = service.context_accesses(snapshot, identity, batch, read_snapshot=proof)
        if authorization_view:
            return service.context_resolution(accesses, batch.contexts[0].ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
        principal = accesses.principal
    authorization = SourceWriteAuthorization(identity, entry_id, params, principal, precondition)
    try:
        call = business.assign_source_orders if entry_id == ASSIGN else business.unassign_source_orders
        result = call(db, **params, operated_by=principal.username, user_ctx=None,
            _source_authorization=authorization)
        # Raw primitives and all projection repairs have completed all domain
        # locks. No new domain lock is permitted after this actor SHARE lock.
        authorization.finish(db)
        db.commit()
        return {'assignments': result}
    except BaseException:
        db.rollback()
        raise


AUTO = 'POST /api/maintenance/project-assignments/orders/auto-assign'


def _auto_context(db, identity, *, entry_id=AUTO, params=None, branch=None, read_snapshot=None,
                  locked_user=None, _principal=None):
    from sqlalchemy import and_
    from sqlalchemy.orm import load_only
    from app.models.system import SysUser
    from app.services.query_filters import active_beta_maintenance_orders
    if entry_id != AUTO or params not in (None, {}):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '自动归属不接受客户端目标列表')
    orders = list(db.scalars(active_beta_maintenance_orders(select(FMaintenanceOrder)
        .options(load_only(FMaintenanceOrder.id, FMaintenanceOrder.raw_order_id, FMaintenanceOrder.project_std,
            FMaintenanceOrder.project_raw, FMaintenanceOrder.linked_sales_order_no, FMaintenanceOrder.salesperson))
        .outerjoin(MaintenanceSourceOrderAssignment, and_(
            MaintenanceSourceOrderAssignment.source_order_id == FMaintenanceOrder.raw_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True)))
        .where(MaintenanceSourceOrderAssignment.assignment_id.is_(None))
        .order_by(FMaintenanceOrder.raw_order_id).execution_options(populate_existing=True), FMaintenanceOrder)))
    (grouped, group_names, ungrouped, existing, creates, ambiguous, target_ids, owner_ids,
        _, _, _) = business.plan_auto_assignment(db, orders)
    parent_ids = sorted(target_ids | owner_ids)
    source_ids = [row.raw_order_id for row in orders]
    if _principal is None:
        principal, projects = foundation.resolve_project_subjects(db, identity, parent_ids,
            read_snapshot=read_snapshot, locked_user=locked_user)
    else:
        principal = _principal
        projects = foundation.project_evidence(db, principal, parent_ids)
    # User mappings are inputs to owner backfill, not a username ownership proof.
    names = {value for value in business.salesperson_modes_by_project(db, parent_ids).values() if value}
    names.update(row.salesperson for row in orders if row.salesperson)
    users = list(db.execute(select(SysUser.id, SysUser.salesperson_name, SysUser.is_active,
        SysUser.deleted_at, SysUser.token_version).where(SysUser.salesperson_name.in_(names)).order_by(SysUser.id))) if names else []
    digest = relation_revision({'sources': [[row.raw_order_id, row.project_std, row.project_raw,
        row.linked_sales_order_no, row.salesperson] for row in orders],
        'existing': sorted([list(key), pid] for key, pid in existing.items()),
        'creates': sorted(map(list, creates)), 'ambiguous': sorted(map(list, ambiguous)),
        'owner_projects': sorted(owner_ids), 'users': [[*row[:3], row.deleted_at.isoformat() if row.deleted_at else None, row.token_version] for row in users]})
    actual_branch = ('default' if source_ids and parent_ids else 'create_only' if source_ids
                     else 'owner_only' if parent_ids else 'empty')
    if branch not in (None, actual_branch):
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '自动归属实际目标集合已变化')
    operation_id = relation_revision({'entry': AUTO, 'actor': principal.id,
        'epoch': principal.authorization_epoch, 'plan': digest})
    carrier = foundation.slot_ref(AUTO, 'context_3', operation_id, selection={'command_plan_sha256': digest})
    slots = {'context_0': (foundation.slot_ref(AUTO, 'context_0', 'source_assignments_auto_candidates'),),
             'context_3': (carrier,)}
    if source_ids:
        slots['context_1'] = tuple(foundation.slot_ref(AUTO, 'context_1', sid) for sid in source_ids)
    if parent_ids:
        slots['context_2'] = tuple(foundation.slot_ref(AUTO, 'context_2', pid) for pid in parent_ids)
    context = foundation.resolved_context(db, entry_id=AUTO, branch=actual_branch, carrier=carrier, slots=slots,
        subjects={'maintenance_source_assignment_write': tuple(PolicySubject(value, {})
            for value in [operation_id, *source_ids, *parent_ids])},
        revision=relation_revision({'plan': digest, 'actor': [principal.id, principal.authorization_epoch],
            'projects': {pid: item.authorization_revision for pid, item in projects.items()}}),
        empty_target_set=not source_ids and not parent_ids)
    return create_context_batch(db, [context])


foundation.register_entry_resolver(AUTO, _auto_context)


class SourceAutoAuthorization:
    def __init__(self, identity, principal, precondition):
        self.identity, self.principal, self.precondition = identity, principal, precondition
        self.batch = None
        self.transaction = None
        self.pending_users = []

    def capture(self, db):
        if self.batch is not None:
            return
        self.transaction = db.get_transaction()
        self.batch = _auto_context(db, self.identity, _principal=self.principal)

    def defer_user(self, db, *, project, user, salesperson, state, skip_bump, reason, operator):
        self.pending_users.append({'project_id': project.project_id, 'user_id': user.id,
            'salesperson': salesperson, 'state': state, 'skip_bump': skip_bump, 'reason': reason, 'operator': operator})

    def finish(self, db):
        from app.models.system import SysUser
        from app.models.maintenance_project import MaintenanceProjectUserAssignment
        from app.services import maintenance_project_assignments
        if self.batch is None or self.transaction is not db.get_transaction():
            raise ValueError('自动归属缺少锁内完整命令上下文')
        project_ids = sorted({item['project_id'] for item in self.pending_users})
        # Every project is already locked by the domain primitive. Lock its
        # existing primary rows before taking the final complete user lock set.
        if project_ids:
            list(db.scalars(select(MaintenanceProjectUserAssignment).where(
                MaintenanceProjectUserAssignment.project_id.in_(project_ids),
                MaintenanceProjectUserAssignment.responsibility_type == 'primary_manager',
                MaintenanceProjectUserAssignment.archived_at.is_(None))
                .order_by(MaintenanceProjectUserAssignment.project_id, MaintenanceProjectUserAssignment.assignment_id)
                .with_for_update().execution_options(populate_existing=True)))
        actor, users = foundation.lock_final_users(db, self.identity,
            share_user_ids=sorted({item['user_id'] for item in self.pending_users}))
        for item in self.pending_users:
            user = users[item['user_id']]
            current_id = db.scalar(select(SysUser.id).where(SysUser.salesperson_name == item['salesperson'],
                SysUser.is_active.is_(True), SysUser.deleted_at.is_(None)).order_by(SysUser.id))
            if current_id != user.id or not user.is_active or user.deleted_at is not None:
                raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '自动归属账号映射已变化，请重新预览')
        accesses = service.context_accesses(db, self.identity, self.batch, locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(self.precondition, accesses, self.batch.contexts[0].ref)
        # The immutable input proof is never persisted. Resolve the actual newly
        # created parents too before publishing the resulting business response.
        source_ids = [ref.id for ref in self.batch.contexts[0].slots.get('context_1', ())]
        actual_parent_ids = set(db.scalars(select(MaintenanceSourceOrderAssignment.project_id).where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(source_ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True))))
        actual_parent_ids.update(ref.id for ref in self.batch.contexts[0].slots.get('context_2', ()))
        if actual_parent_ids:
            refs = tuple(foundation.slot_ref(AUTO, 'context_2', pid) for pid in sorted(actual_parent_ids))
            from dataclasses import replace
            original = self.batch.contexts[0]
            slots = {**original.slots, 'context_2': refs}
            branch = 'default' if source_ids else 'owner_only'
            complete = replace(original, branch=branch, slots=slots, empty_target_set=False,
                policy_subjects={'maintenance_source_assignment_write': tuple(PolicySubject(value, {})
                    for value in [original.ref.id, *source_ids, *sorted(actual_parent_ids)])})
            require_complete(service.context_accesses(db, self.identity, create_context_batch(db, [complete]), locked_user=actor))
        for item in self.pending_users:
            maintenance_project_assignments.assign_primary_manager(db, project_id=item['project_id'],
                user_id=item['user_id'], expected_assignment_id=None, expected_assignment_version=None,
                reason=item['reason'], operated_by=item['operator'], _prelocked_state=item['state'],
                _skip_workbook_bump=item['skip_bump'],
                _authorize=lambda project, current, user_id: users[user_id])


def auto_command(db, identity, *, authorization_view=False, precondition=None):
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
        batch = _auto_context(snapshot, identity, read_snapshot=proof)
        accesses = service.context_accesses(snapshot, identity, batch, read_snapshot=proof)
        if authorization_view:
            return service.context_resolution(accesses, batch.contexts[0].ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
        principal = accesses.principal
    authorization = SourceAutoAuthorization(identity, principal, precondition)
    try:
        result = business.auto_assign_unassigned(db, operated_by=principal.username, user_ctx=None,
            _authorization=authorization)
        authorization.finish(db)
        db.commit()
        return {'result': result}
    except BaseException:
        db.rollback()
        raise
