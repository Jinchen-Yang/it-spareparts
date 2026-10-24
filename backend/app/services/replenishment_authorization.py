"""Replenishment contracts bind stable owners, real projects and immutable versions."""
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.authz import service
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.engine import QueryBindings
from app.authz.errors import authz_error
from app.config import get_settings
from app.models.dimensions import DimPart
from app.models.maintenance_project import MaintenanceProject
from app.models.replenishment import (ReplenishmentApplication as Application,
    ReplenishmentApplicationVersion as Version, ReplenishmentApplicationLine as Line,
    ReplenishmentCartDraft as Cart, ReplenishmentCartDraftLine as CartLine,
    ReplenishmentAuditEvent as Event)
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import replenishment as business
from app.services import replenishment_cart as cart_business
from app.services.maintenance_workflow_authorization import require_complete

BASE = '/api/replenishment-beta'
CAPABILITIES = 'GET ' + BASE + '/capabilities'
CATALOG = 'GET ' + BASE + '/catalog'
PROJECTS = 'GET ' + BASE + '/projects'
LIST = 'GET ' + BASE + '/applications'
CREATE = 'POST ' + BASE + '/applications'
READ = 'GET ' + BASE + '/applications/{application_id}'
EXPORT = READ + '/exports/system-screening.xlsx'
REVISE = 'POST ' + BASE + '/applications/{application_id}/revisions'
CART_READ = 'GET ' + BASE + '/cart-drafts/{project_id}'
CART_UPDATE = 'PUT ' + BASE + '/cart-drafts/{project_id}'
CART_DELETE = 'DELETE ' + BASE + '/cart-drafts/{project_id}'
CART_SUBMIT = 'POST ' + BASE + '/cart-drafts/{project_id}/submit'
ENTRIES = (CAPABILITIES, CATALOG, PROJECTS, LIST, CREATE, READ, EXPORT, REVISE,
           CART_READ, CART_UPDATE, CART_DELETE, CART_SUBMIT)


def _application(db, application_id, *, metadata=False):
    statement = select(Application).where(Application.application_id == application_id)
    if metadata:
        statement = statement.options(load_only(Application.application_id, Application.owner_user_id,
            Application.project_id, Application.version, raiseload=True))
    row = db.scalar(statement.execution_options(populate_existing=True))
    if row is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '补库申请不存在')
    if row.owner_user_id is None:
        raise artifacts.unavailable()
    return row


def _cart(db, owner_id, project_id, *, metadata=False):
    statement = select(Cart).where(Cart.owner_user_id == owner_id, Cart.project_id == project_id)
    if metadata:
        statement = statement.options(load_only(Cart.draft_id, Cart.owner_user_id, Cart.project_id,
            Cart.version, Cart.client_request_id, raiseload=True))
    return db.scalar(statement.execution_options(populate_existing=True))


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    principal, _ = foundation.resolve_project_subjects(db, identity, [], read_snapshot=read_snapshot, locked_user=locked_user)
    contract = ENTRY_CONTRACTS[entry_id]
    slot = contract['slots']['context_0']
    requirements = contract['branches']['default']['requirements']
    project_id = params.get('project_id')
    revision = {'actor_id': principal.id}
    app = None
    if entry_id in (READ, EXPORT, REVISE):
        app = _application(db, params['application_id'], metadata=True)
        project_id = app.project_id
        carrier = foundation.slot_ref(entry_id, 'context_0', app.application_id)
        revision['application'] = [app.application_id, app.owner_user_id, app.project_id, app.version]
    elif slot['kind'] == 'collection':
        selection = {name: params[name] for name in slot['selector_schema']['properties'] if name in params}
        carrier = foundation.slot_ref(entry_id, 'context_0', slot['collection_ref'], selection=selection)
        revision['collection'] = foundation.project_collection_revision(db, principal, carrier) if entry_id == PROJECTS else carrier.to_dict()
    elif slot['kind'] == 'project':
        carrier = foundation.slot_ref(entry_id, 'context_0', project_id)
    else:
        # The cart is an owner/project address even before its first save. No
        # historical username or caller-supplied draft id can change its owner.
        row = _cart(db, principal.id, project_id, metadata=True)
        address = relation_revision({'owner_id': principal.id, 'project_id': project_id})
        carrier = foundation.slot_ref(entry_id, 'context_0', address, selection={'project_id': project_id})
        revision['cart'] = [row.draft_id, row.version, row.client_request_id] if row else None
    slots = {'context_0': (carrier,)}
    project = None
    if entry_id in (CREATE, REVISE, CART_READ, CART_UPDATE, CART_DELETE, CART_SUBMIT):
        if not project_id:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '补库申请缺少真实项目')
        _, evidence = foundation.resolve_project_subjects(db, identity, [project_id], read_snapshot=read_snapshot, locked_user=locked_user)
        project = evidence[project_id]
        if not project.subject.attributes['is_active']:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '维保项目已停用')
        revision['project'] = project.authorization_revision
        if 'context_1' in contract['slots']:
            slots['context_1'] = (foundation.slot_ref(entry_id, 'context_1', project_id),)
    subjects = {}
    for resource in requirements:
        if resource == 'replenishment_project_commands':
            subjects[resource] = (project.subject,)
        elif resource == 'replenishment_request':
            subjects[resource] = () if entry_id == LIST else (PolicySubject(app.application_id, {'is_owner': app.owner_user_id == principal.id}),)
        elif resource == 'replenishment_project_directory':
            subjects[resource] = ()
        else:
            subjects[resource] = (PolicySubject(carrier.id, {}),)
    return create_context_batch(db, [foundation.resolved_context(db, entry_id=entry_id, branch='default',
        carrier=carrier, slots=slots, subjects=subjects, revision=relation_revision(revision))])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    if entry_id in (READ, EXPORT, REVISE):
        params = {'application_id': ref.id}
    elif entry_id == CREATE:
        params = {'project_id': ref.id}
    else:
        raise artifacts.unavailable()
    return _resolve(db, identity, entry_id=entry_id, params=params, branch=branch,
                    read_snapshot=read_snapshot, locked_user=locked_user)


for entry in ENTRIES:
    foundation.register_entry_resolver(entry, _resolve, reference_resolver=_reference)


def _application_query(db, principal, context, resource):
    if context.ref.kind != 'collection' or context.ref.id != 'replenishment_owned_applications':
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '申请查询集合不匹配')
    table = select(Application.application_id.label('application_id'),
        (Application.owner_user_id == principal.id).label('is_owner')).subquery('replenishment_authorization_applications')
    return QueryBindings(resource, table, {'request.resource.attr.is_owner': table.c.is_owner})


def application_condition(access):
    from sqlalchemy.sql import visitors
    tables = {column.table for column in visitors.iterate(access.predicate)
        if getattr(getattr(column, 'table', None), 'name', None) == 'replenishment_authorization_applications'}
    if len(tables) > 1:
        raise authz_error(503, 'AUTHZ_ENGINE_UNAVAILABLE', '申请查询范围无法确认')
    if not tables:
        return access.predicate
    table = tables.pop()
    return Application.application_id.in_(select(table.c.application_id).where(access.predicate))


foundation.register_query_bindings('replenishment_request', _application_query)


def _version_state(db, target):
    version = db.get(Version, target.id, populate_existing=True)
    if version is None or version.owner_user_id is None or version.status != 'submitted' or target.generation != 1:
        raise artifacts.unavailable()
    app = _application(db, version.application_id)
    # Version contents and their system review are immutable business facts.
    # Mutable project metadata and the reader's account never enter this hash.
    content = {'application_id': app.application_id, 'project_id': app.project_id,
        'owner_user_id': app.owner_user_id, 'version_owner_user_id': version.owner_user_id,
        'version': business._version_payload(db, version),
        'events': [[event.event_id, event.action, event.before_json, event.after_json, event.reason, event.operated_by]
            for event in db.scalars(select(Event).where(Event.version_id == version.version_id).order_by(Event.event_id))]}
    return artifacts.ArtifactDomainState(target, ObjectRef('replenishment_application', app.application_id),
        business._digest(content), version.owner_user_id)


artifacts.register_artifact_domain('replenishment_version', artifacts.ArtifactDomainAdapter(_version_state, None, (), private=False))


def verify_versions(db, app):
    versions = list(db.scalars(select(Version).where(Version.application_id == app.application_id)))
    if not versions:
        raise artifacts.unavailable()
    for version in versions:
        artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget('replenishment_version', version.version_id, 1))


@dataclass(frozen=True)
class CommandAuthorization:
    """An internal, transaction-bound adapter for already-authorized business work."""
    db: Session
    transaction: object
    accesses: service.ContextAccessSet
    entry_id: str
    project_id: str
    application_id: str | None
    seal: object

    def check(self, db, *, project_id=None, application_id=None, actions=()):
        if (self.seal is not _SEAL or self.db is not db or db.get_transaction() is not self.transaction
                or not self.transaction.is_active or self.entry_id not in ENTRIES
                or (project_id is not None and project_id != self.project_id)
                or (application_id is not None and application_id != self.application_id)):
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '补库业务授权与当前事务或目标不一致')
        require_complete(self.accesses)
        held = {a.action for a in self.accesses.accesses.values() if a.resource == 'replenishment_project_commands'}
        if actions and not held.intersection(actions):
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '补库动作授权不匹配')
        return self.accesses.principal


_SEAL = object()


def read(db, identity, *, entry_id, params, authorization_view=False, precondition=None):
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity, entry_id=entry_id, params=params) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, ref)
        resource, actions = next(iter(batch.contexts[0].requirements.items()))
        access = accesses.require(ref, resource, actions[0])
        if entry_id == CAPABILITIES:
            settings = get_settings()
            result = {'enabled': settings.replenishment_beta_enabled, 'beta': True,
                'can_view_price': False, 'can_create': False, 'can_review': False,
                'workflow_mode': 'system_auto_review' if settings.replenishment_auto_review_enabled else 'system_screening',
                'stage': 'screening_complete', 'stable_path': '/inventory',
                'data_contract': '仅记录维保项目补库申请与提交时冻结的三查事实；不修改库存、不自动定价。'}
        elif entry_id == CATALOG:
            result = business.catalog_search(snapshot, params.get('q'), page=params['page'], page_size=params['page_size'])
        elif entry_id == PROJECTS:
            result = {'items': business.available_projects(snapshot, visible_project_ids=None, authorization_condition=foundation.project_condition(access))}
        elif entry_id == LIST:
            result = business.list_applications(snapshot, username=identity.username, role=access.principal.role,
                page=params['page'], page_size=params['page_size'], authorization_access=access)
        elif entry_id == CART_READ:
            result = {'draft': cart_business._payload(snapshot, _cart(snapshot, access.principal.id, params['project_id']))}
        else:
            app = _application(snapshot, params['application_id'])
            verify_versions(snapshot, app)
            if entry_id == EXPORT:
                return business.system_screening_workbook(snapshot, app.application_id,
                    username=identity.username, role=access.principal.role, authorization_access=access)
            result = business.get_application(snapshot, app.application_id,
                username=identity.username, role=access.principal.role, authorization_access=access)
        return {**result, 'authorization_context': accesses.wire(ref)}


def _lock_domain(db, identity, *, entry_id, params, principal_id):
    project_id = params.get('project_id')
    application_id = params.get('application_id')
    key = params.get('client_request_id')
    if entry_id == REVISE:
        # Preserve the existing revision advisory -> project ordering.
        db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(
            f'replenishment-revision:{application_id}:{key}', 0))))
        project_id = _application(db, application_id).project_id
    project = db.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id == project_id)
        .with_for_update().execution_options(populate_existing=True))
    if project is None or not project.is_active:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '维保项目不存在或已停用')
    draft = None
    part_ids = {int(item['part_id']) for item in params.get('lines') or []}
    part_ids.update(int(item['part_id']) for item in params.get('resolutions') or [] if item.get('part_id'))
    if entry_id in (CART_UPDATE, CART_DELETE, CART_SUBMIT):
        draft = db.scalar(select(Cart).where(Cart.owner_user_id == principal_id, Cart.project_id == project_id)
            .with_for_update().execution_options(populate_existing=True))
        if draft is not None:
            rows = list(db.scalars(select(CartLine).where(CartLine.draft_id == draft.draft_id)
                .order_by(CartLine.draft_line_id).with_for_update().execution_options(populate_existing=True)))
            part_ids.update(row.part_id for row in rows)
    if entry_id in (CREATE, CART_SUBMIT):
        db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(
            f'replenishment-submit:{principal_id}:{key}', 0))))
        existing = db.scalar(select(Application).where(Application.owner_user_id == principal_id,
            Application.client_request_id == key).with_for_update().execution_options(populate_existing=True))
        if existing is not None:
            if existing.project_id != project_id:
                raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '请求键已用于不同项目')
            application_id = existing.application_id
    if application_id is not None:
        app = db.scalar(select(Application).where(Application.application_id == application_id)
            .with_for_update().execution_options(populate_existing=True))
        if app is None or app.project_id != project_id:
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '申请项目已变化')
        versions = list(db.scalars(select(Version).where(Version.application_id == application_id)
            .order_by(Version.version_id).with_for_update().execution_options(populate_existing=True)))
        version_ids = [version.version_id for version in versions]
        rows = list(db.scalars(select(Line).where(Line.version_id.in_(version_ids))
            .order_by(Line.line_id).with_for_update().execution_options(populate_existing=True)))
        part_ids.update(row.part_id for row in rows)
        verify_versions(db, app)
    if part_ids:
        list(db.scalars(select(DimPart).where(DimPart.id.in_(sorted(part_ids))).order_by(DimPart.id)
            .with_for_update().execution_options(populate_existing=True)))
    return project_id, application_id


def command(db, identity, *, entry_id, params, legacy_context, precondition=None, authorization_view=False):
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity, entry_id=entry_id, params=params) as (_, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, ref)
        principal_id = accesses.principal.id
    artifacts.begin_artifact_protocol(db.get_bind())
    try:
        project_id, application_id = _lock_domain(db, identity, entry_id=entry_id, params=params, principal_id=principal_id)
        actor = service.lock_actor(db, identity)
        batch = foundation.resolve_entry_context(db, identity, entry_id=entry_id, params=params, locked_user=actor)
        accesses = service.context_accesses(db, identity, batch, locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
        authorization = CommandAuthorization(db, db.get_transaction(), accesses, entry_id, project_id,
            application_id if entry_id == REVISE else None, _SEAL)
        before = set(db.scalars(select(Version.version_id).where(Version.application_id == application_id))) if application_id else set()
        common = dict(username=identity.username, user_ctx=legacy_context, authorization=authorization, commit=False)
        if entry_id == CREATE:
            result = business.submit_application_atomic(db, **common, **params)
        elif entry_id == REVISE:
            result = business.apply_revision_atomic(db, **common, **params)
        elif entry_id == CART_UPDATE:
            result = {'draft': cart_business.replace_cart_draft(db, **common, **params)}
        elif entry_id == CART_DELETE:
            result = {'deleted': cart_business.delete_cart_draft(db, **common, **params)}
        else:
            result = cart_business.submit_cart_draft_atomic(db, **common, **params)
        if entry_id in (CREATE, REVISE, CART_SUBMIT):
            db.flush()
            app = _application(db, result['application_id'])
            for version in db.scalars(select(Version).where(Version.application_id == app.application_id)):
                target = artifacts.ArtifactTarget('replenishment_version', version.version_id, 1)
                if version.version_id not in before:
                    state = _version_state(db, target)
                    artifacts.register_artifact_binding(db, identity, target=target,
                        input_digest=state.input_digest, locked_user=actor)
                else:
                    artifacts.load_artifact_binding(db, target=target)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
