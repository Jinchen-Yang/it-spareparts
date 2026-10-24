"""Stable-project use cases: canonical scope and facts share one observation."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.business_time import business_today
from app.services import maintenance_authorization as authorization
from app.services import maintenance_project
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_manager import MaintenanceServicePeriod
from app.models.system import SysUser
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_catalog as catalog
from app.services import maintenance_project_operations as operations
from app.services import project_names

DIRECTORY = 'GET /api/maintenance/projects/stable'
SEARCH = 'POST /api/maintenance/projects/stable/search'
OVERVIEW = 'GET /api/maintenance/projects/stable/{project_id}'


def _view(value):
    if value not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知的授权视图')
    return value == 'authorization'


def read_directory(db, identity, *, legacy_context, q=None, page=1, page_size=50,
                   include_inactive=False, as_of=None, view=None, precondition=None):
    authorization_view = _view(view)
    effective_as_of = as_of or business_today()
    entry_id = SEARCH if q is not None else DIRECTORY
    params = {'q': q, 'include_inactive': include_inactive, 'as_of': effective_as_of}
    with authorization.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry_id, params=params) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        access = accesses.require(ref, 'maintenance_project_directory', 'read')
        service.check_context_precondition(authorization.decode_precondition(precondition), accesses, ref)
        result = maintenance_project.project_directory(snapshot, q_text=q, page=page, page_size=page_size,
            include_inactive=include_inactive, as_of=effective_as_of,
            project_condition=authorization.project_condition(access))
        return {**result, 'authorization_context': accesses.wire(ref)}


def read_overview(db, identity, project_id, *, legacy_context, as_of=None, view=None, precondition=None):
    authorization_view = _view(view)
    effective_as_of = as_of or business_today()
    with authorization.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=OVERVIEW, params={'project_id': project_id, 'as_of': effective_as_of},
            branch='authorization_view' if authorization_view else None) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        access = accesses.require(ref, 'maintenance_project', 'read')
        service.check_context_precondition(authorization.decode_precondition(precondition), accesses, ref)
        result = maintenance_project.project_overview(snapshot, project_id, as_of=effective_as_of,
            user_ctx=service.field_context(access, legacy_context))
        if result is None:
            raise authz_error(404, 'MAINTENANCE_PROJECT_NOT_FOUND', '维保项目不存在')
        # Compatibility flag is never a new workbook authority. Each workbook
        # obtains its own registered context before download/validate/apply.
        result['project']['can_edit_master_workbook'] = False
        return {**result, 'authorization_context': accesses.wire(ref)}


def _master_locks(db, params):
    project_id, command = params['project_id'], params['command']
    if 'display_name' in command:
        project_names.lock_display_name_identities(db, [command['display_name']])
    project, state = catalog._lock_project_for_master_write(db, project_id=project_id)
    if project is None:
        raise authz_error(404, 'MAINTENANCE_PROJECT_NOT_FOUND', '维保项目不存在')
    # Period writes take this row after the project, before any user lock.
    db.scalars(select(MaintenanceServicePeriod).where(MaintenanceServicePeriod.project_id == project_id)
        .with_for_update().execution_options(populate_existing=True)).all()
    links = list(db.scalars(select(MaintenanceProjectUserAssignment).where(
        MaintenanceProjectUserAssignment.project_id == project_id)
        .order_by(MaintenanceProjectUserAssignment.assignment_id).with_for_update()
        .execution_options(populate_existing=True)))
    wanted = command.get('visible_usernames')
    targets = set(row.user_id for row in links)
    if wanted is not None:
        cleaned = {name.strip() for name in wanted if name.strip()}
        selected = list(db.execute(select(SysUser.id, SysUser.username).where(SysUser.username.in_(cleaned))))
        if {row.username for row in selected} != cleaned:
            raise assignments.MaintenanceProjectAssignmentError('账号不存在或已停用')
        targets.update(row.id for row in selected)
    return project, state, targets


def _master_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    principal, projects = authorization.resolve_project_subjects(db, identity, [params['project_id']],
        read_snapshot=read_snapshot, locked_user=locked_user)
    evidence = projects[params['project_id']]
    slot = ENTRY_CONTRACTS[entry_id]['slots']['context_0']
    selection = {key:params[key] for key in slot['selector_schema']['properties'] if key in params}
    ref = authorization.slot_ref(entry_id,'context_0',params['project_id'],selection=selection)
    targets = []
    if params.get('visible_usernames') is not None:
        requested = {name.strip() for name in params['visible_usernames'] if name.strip()}
        targets = list(db.execute(select(SysUser.id, SysUser.username, SysUser.is_active, SysUser.deleted_at)
            .where(SysUser.username.in_(requested)).order_by(SysUser.id)))
        if ({row.username for row in targets} != requested
                or any(not row.is_active or row.deleted_at is not None for row in targets)):
            raise assignments.MaintenanceProjectAssignmentError('账号不存在或已停用')
    revision = relation_revision({'project':evidence.authorization_revision,'carrier':ref.to_dict(),
        'viewer_targets':[[row.id,row.username,row.is_active] for row in targets]})
    context = authorization.resolved_context(db, entry_id=entry_id, branch='default',carrier=ref,
        slots={'context_0':(ref,)},subjects={'maintenance_project':(evidence.subject,)},revision=revision)
    return create_context_batch(db,[context])


def change_project(db, identity, project_id, *, action, command, view=None, precondition=None):
    """One master transaction; relationship and user locks precede authorization."""
    suffix = '' if action == 'update' else '/' + action
    entry_id = ('PATCH' if action == 'update' else 'POST') + ' /api/maintenance/projects/stable/{project_id}' + suffix
    params = {'project_id': project_id, **command}
    if _view(view):
        with authorization.read_context_snapshot(lambda: Session(db.get_bind()), identity,
                entry_id=entry_id, params=params) as (_, _, batch, accesses):
            return service.context_resolution(accesses, batch.contexts[0].ref)
    try:
        locked = authorization.lock_context_batch(db, identity, entry_id=entry_id, params=params,
            lock_domain=lambda transaction, _: _master_locks(transaction, {'project_id':project_id,'command':command}),
            lock_users=lambda transaction, who, state: authorization.lock_final_users(
                transaction, who, share_user_ids=state[2])[0])
        ref = locked.batch.contexts[0].ref
        locked.accesses.require(ref, 'maintenance_project', action)
        service.check_context_precondition(authorization.decode_precondition(precondition), locked.accesses, ref)
        project, state, _ = locked.domain_state
        if project.version != command['version']:
            raise catalog.MaintenanceProjectCatalogConflict('项目主档已被他人修改，请刷新后重试')
        operator = locked.actor.username
        if action != 'update':
            result = catalog.set_project_active(db, project_id=project_id, version=command['version'],
                active=action=='restore', reason=command['reason'], operated_by=operator, _prelocked=(project,state))
        else:
            updates = {k:v for k,v in command.items() if k not in {'version','reason','visible_usernames'}}
            result = catalog.update_project(db,project_id=project_id,version=command['version'],updates=updates,
                reason=command['reason'],operated_by=operator,_prelocked=(project,state)) if updates else catalog.project_dict(project)
            master_changed = result['version'] != command['version']
            if command.get('visible_usernames') is not None:
                viewers, changed = assignments.sync_project_viewers(db,project_id=project_id,
                    usernames=command['visible_usernames'],operated_by=operator,reason=command['reason'])
                if changed:
                    if not master_changed: project.version += 1
                    operations.bump_locked_workbook_revision(db,state=state)
                    db.flush()
                result = {**catalog.project_dict(project),'visible_usernames':viewers}
        response = {**result, 'authorization_context':locked.accesses.wire(ref)}
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


for _entry in ('PATCH /api/maintenance/projects/stable/{project_id}',
               'POST /api/maintenance/projects/stable/{project_id}/archive',
               'POST /api/maintenance/projects/stable/{project_id}/restore'):
    authorization.register_entry_resolver(_entry, _master_context)


def read_operations_directory(db,identity,*,entry_id,command,legacy_context,view=None,precondition=None):
    """Plan the granted project predicate before all card aggregates and paging."""
    from app.models.maintenance_project import MaintenanceProject
    from app.security import is_field_hidden
    if entry_id not in ('GET /api/maintenance/projects/stable/operations',
                        'POST /api/maintenance/projects/stable/operations/search'):
        raise ValueError('unregistered operations directory')
    authorization_view=_view(view)
    command=dict(command);command['as_of']=command.get('as_of') or business_today()
    scope=command.get('owner_scope') or 'all'
    if scope not in ('me','all'):
        raise authz_error(422,'AUTHZ_INVALID_REQUEST','项目负责人范围无效')
    command['owner_scope']=scope
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=entry_id,params=command) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        access=accesses.require(ref,'maintenance_operation_directory','read')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        fields=service.field_context(access,legacy_context)
        owner=MaintenanceProject.project_id.in_(select(MaintenanceProjectUserAssignment.project_id).where(
            MaintenanceProjectUserAssignment.user_id==access.principal.id,
            MaintenanceProjectUserAssignment.responsibility_type.in_(('primary_manager','viewer')),
            MaintenanceProjectUserAssignment.archived_at.is_(None)))
        try:
            result=operations.project_operations(snapshot,as_of=command['as_of'],user_ctx=fields,
                q_text=command.get('q'),lifecycle=command.get('lifecycle','all'),reminder=command.get('reminder'),
                include_inactive=command.get('include_inactive',False),page=command.get('page',1),
                page_size=command.get('page_size',24),owner_scope=scope,task_type=command.get('task_type'),
                task_status=command.get('task_status'),due_from=command.get('due_from'),due_to=command.get('due_to'),
                project_condition=authorization.project_condition(access),owner_condition=owner)
        except operations.MaintenanceOperationPermissionError as exc:
            raise authz_error(403,'AUTHZ_FIELD_REQUIRED','当前授权不允许按该财务事实筛选') from exc
        except operations.MaintenanceOperationError as exc:
            raise authz_error(422,'DOMAIN_INVALID',str(exc)) from exc
        if is_field_hidden(fields,'contract_amount'):
            for row in result['rows']:
                tracking=row.get('manager_tracking') or {}
                for key in ('next_collection_milestone','outstanding_collection_milestones'):
                    tracking[key]=None
        return {**result,'authorization_context':accesses.wire(ref)}
