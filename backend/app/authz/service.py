"""Live resource access and batch capability snapshots.

Source profiles/templates never authorize reads. The only allow source is a
validated, live Grant passed through the PDP. Domain adapters own relationships
and ORM bindings; this module owns identity, coherence and failure semantics.
"""
from dataclasses import dataclass, replace
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from types import MappingProxyType

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.authz import engine, runtime
from app.authz.catalog import CATALOG_VERSION, SALES_FIELD_PERMISSIONS, SALES_RESOURCE, get_resource_definition, resource_descriptors
from app.authz.errors import authz_error
from app.authz.context import ContextBatch, ObjectRef, ReadSnapshotProof, ResolvedContext
from app.authz.principal import Principal, VerifiedIdentity, actor_capabilities, load_principal
from app.models.authorization import SysAuthorizationGrant, SysCustomerAssignment
from app.models.sales import FSalesOrder
from app.models.system import SysUser
from app.security import UserContext
from app import permissions


@dataclass(frozen=True)
class ResourceAccess:
    resource: str
    action: str
    principal: Principal
    authorization_epoch: int
    grant_version: int
    catalog_version: str
    scope: str
    field_permissions: frozenset[str]
    predicate: object
    object_id: str | int | None = None
    authorization_revision: str | int | None = None
    object_ref: ObjectRef | None = None
    entry_id: str | None = None
    context_branch: str | None = None

    def require_binding(self, resource: str, action: str) -> "ResourceAccess":
        if self.resource != resource or self.action != action:
            raise authz_error(403, "AUTHZ_FORBIDDEN", "授权上下文与业务操作不匹配")
        return self


_recorder: ContextVar[Callable[[ResourceAccess], None] | None] = ContextVar('authorization_access_recorder', default=None)


@contextmanager
def access_recorder(callback: Callable[[ResourceAccess], None]):
    """Install inside the worker that executes reads; always restore its context."""
    token = _recorder.set(callback)
    try:
        yield
    finally:
        _recorder.reset(token)


def _record(access: ResourceAccess) -> ResourceAccess:
    callback = _recorder.get()
    if callback is not None:
        callback(access)
    return access


def lock_actor(db: Session, identity: VerifiedIdentity) -> SysUser:
    """After domain locks, take SHARE without a later lock-strength upgrade."""
    if type(identity) is not VerifiedIdentity:
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '需要已验证的类型化身份')
    if identity.fallback:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '此操作需要有效实名账号')
    user = db.scalar(select(SysUser).where(SysUser.username == identity.username)
                     .with_for_update(read=True).execution_options(populate_existing=True))
    if user is None:
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '实名账号或登录状态已失效')
    load_principal(db, identity, locked_user=user)
    return user


def field_context(access: ResourceAccess, legacy_context: UserContext) -> UserContext:
    """A field adapter, never a role shim or a second permission authority."""
    field_policy = {key: key in access.field_permissions for key in permissions.DATA_GROUPS}
    field_policy['own_customers_only'] = False
    return replace(legacy_context, user_id=access.principal.username, role=access.principal.role,
                   salesperson_name=access.principal.salesperson_name, permissions=field_policy,
                   is_authenticated=True, authn='sys_user', is_fallback=False,
                   token_version=access.authorization_epoch, explicit_field_policy=True)


@dataclass(frozen=True)
class ResourceReadSet:
    requested: frozenset[tuple[str, str]]
    accesses: Mapping[tuple[str, str], ResourceAccess]

    def __post_init__(self):
        object.__setattr__(self, 'accesses', MappingProxyType(dict(self.accesses)))

    def get(self, resource: str, action: str) -> ResourceAccess | None:
        if (resource, action) not in self.requested:
            raise ValueError('查询未登记在本次授权批次')
        return self.accesses.get((resource, action))

    def require(self, resource: str, action: str) -> ResourceAccess:
        access = self.get(resource, action)
        if access is None:
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '未获得此资源动作授权')
        return access

    def decision(self, resource: str, action: str) -> dict:
        allowed = self.get(resource, action) is not None
        return {'effect': 'allow' if allowed else 'deny', 'reason': None if allowed else {
            'code': 'NOT_GRANTED', 'message': '未获得此资源动作授权'}}


def read_accesses(db: Session, identity: VerifiedIdentity, requirements: Mapping[str, Sequence[str]], *,
                  locked_user: SysUser | None = None) -> ResourceReadSet:
    """One live actor/Grant read and one PDP batch for independent all-scope blocks.

    Only ordinary missing Grants/actions are partial denials. Invalid metadata,
    configuration, runtime, identity, database and PDP results abort the request.
    Scoped/object resources require their domain-bound resource_access instead.
    """
    if not isinstance(requirements, Mapping) or not requirements:
        raise ValueError('批量授权必须具有明确的资源动作')
    requested = {}
    for resource, actions in requirements.items():
        definition = get_resource_definition(resource)
        if (definition.query_attributes or definition.context_scopes
                or not isinstance(actions, (list, tuple)) or not actions
                or any(type(a) is not str for a in actions)
                or len(set(actions)) != len(actions) or not set(actions) <= definition.actions):
            raise ValueError('批量授权只接受已登记、无需对象上下文的动作')
        requested[resource] = tuple(actions)
    states = runtime.runtime_states(db)
    if any(states[resource].runtime_state != 'enabled' for resource in requested):
        raise authz_error(503, 'AUTHZ_RESOURCE_UNAVAILABLE', '授权模块未启用或已暂停')
    principal = _read_principal(db, identity, locked_user=locked_user)
    if principal is None:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '此操作需要有效实名账号')
    rows = db.scalars(select(SysAuthorizationGrant).where(
        SysAuthorizationGrant.user_id == principal.id, SysAuthorizationGrant.resource.in_(requested))
        .execution_options(populate_existing=True))
    try:
        grants = {row.resource: _grant(row) for row in rows}
    except (ValueError, TypeError):
        raise authz_error(403, 'AUTHZ_INVALID_CONFIGURATION', '资源授权配置无效') from None
    try:
        decisions = engine.check_resources(principal.id, grants, {r: {} for r in grants},
                                           {r: requested[r] for r in grants}) if grants else {}
    except (engine.AuthorizationDenied, engine.AuthorizationUnavailable):
        raise authz_error(503, 'AUTHZ_ENGINE_UNAVAILABLE', '授权服务暂不可用，请稍后重试') from None
    accesses = {}
    for resource, decision in decisions.items():
        grant = grants[resource]
        for action in decision.allowed_actions:
            access = ResourceAccess(resource, action, principal, principal.authorization_epoch, grant.version,
                                    CATALOG_VERSION, grant.scope, decision.field_permissions, true())
            accesses[(resource, action)] = _record(access)
    return ResourceReadSet(frozenset((r, a) for r, actions in requested.items() for a in actions), accesses)


@dataclass(frozen=True)
class SalesAccess:
    predicate: object
    context: UserContext
    scope: str
    customer_count: int
    access: ResourceAccess


def envelope() -> dict:
    return {"schema_version": 1, "authorization_mode": runtime.authorization_mode(), "catalog_version": CATALOG_VERSION}


def enabled(db: Session) -> bool:
    return runtime.is_authoritative(db, SALES_RESOURCE)


def _read_principal(db: Session, identity: VerifiedIdentity, *, locked_user=None,
                    read_snapshot: ReadSnapshotProof | None = None) -> Principal | None:
    if type(identity) is not VerifiedIdentity:
        raise authz_error(401, "AUTHN_SESSION_INVALID", "需要已验证的类型化身份")
    if identity.fallback:
        return load_principal(db, identity)
    if read_snapshot is not None:
        if type(read_snapshot) is not ReadSnapshotProof or locked_user is not None:
            raise ValueError('一致读取与写入锁证明不能混用')
        read_snapshot.validate(db)
        return load_principal(db, identity)
    if locked_user is not None:
        return load_principal(db, identity, locked_user=locked_user)
    user = db.scalar(select(SysUser).where(SysUser.username == identity.username)
                     .with_for_update(read=True).execution_options(populate_existing=True))
    if user is None:
        raise authz_error(401, "AUTHN_SESSION_INVALID", "实名账号或登录状态已失效")
    return load_principal(db, identity, locked_user=user)


def _grant(row: SysAuthorizationGrant) -> engine.Grant:
    return engine.Grant(row.actions, row.scope, row.field_permissions, row.version, resource=row.resource)


def _customer_ids(db: Session, user_id: int) -> tuple[int, ...]:
    return tuple(db.scalars(select(SysCustomerAssignment.customer_id).where(
        SysCustomerAssignment.user_id == user_id).order_by(SysCustomerAssignment.customer_id)))


def resource_access(db: Session, identity: VerifiedIdentity, resource: str, action: str, *,
                    bindings: engine.QueryBindings | None = None, attributes=None,
                    locked_user: SysUser | None = None, object_id=None, authorization_revision=None,
                    read_snapshot: ReadSnapshotProof | None = None) -> ResourceAccess:
    """Readers hold SHARE; domain writers supply their ordered, already locked actor.

    This function does not commit, acquire domain locks, or upgrade user locks.
    ORM bindings and attributes are trusted server-domain inputs, never JSON.
    """
    if type(identity) is not VerifiedIdentity:
        raise authz_error(401, "AUTHN_SESSION_INVALID", "需要已验证的类型化身份")
    try:
        definition = get_resource_definition(resource)
    except ValueError:
        raise authz_error(403, "AUTHZ_FORBIDDEN", "未知或尚未实施的授权资源") from None
    if action not in definition.actions:
        raise authz_error(403, "AUTHZ_FORBIDDEN", "未注册的资源动作")
    state = runtime.runtime_states(db)[resource]
    if state.runtime_state != "enabled":
        raise authz_error(503, "AUTHZ_RESOURCE_UNAVAILABLE", "授权模块未启用或已暂停")
    principal = _read_principal(db, identity, locked_user=locked_user, read_snapshot=read_snapshot)
    if principal is None:
        raise authz_error(403, "AUTHZ_FORBIDDEN", "此操作需要有效实名账号")
    row = db.scalar(select(SysAuthorizationGrant).where(
        SysAuthorizationGrant.user_id == principal.id, SysAuthorizationGrant.resource == resource,
    ).execution_options(populate_existing=True))
    if row is None:
        raise authz_error(403, "AUTHZ_FORBIDDEN", "未获得此资源授权")
    try:
        grant = _grant(row)
    except (ValueError, TypeError):
        raise authz_error(403, "AUTHZ_INVALID_CONFIGURATION", "资源授权配置无效") from None
    if grant.scope in definition.context_scopes:
        # A module probe is never an object decision. A domain must provide its
        # current relationship revision and an enforceable query binding first.
        if (type(object_id) not in (str, int) or not object_id
                or type(authorization_revision) not in (str, int)
                or authorization_revision == "" or attributes is None
                or bindings is None or not definition.query_attributes):
            raise authz_error(403, "AUTHZ_CONTEXT_REQUIRED", "需要当前对象及关系版本的授权检查")
    if resource == SALES_RESOURCE:
        # Callers cannot inject customer ownership, including privileged API code.
        if attributes is not None:
            raise authz_error(403, "AUTHZ_FORBIDDEN", "客户范围必须从实时归属关系加载")
        attributes = engine._sales_attributes(_customer_ids(db, principal.id))
        if bindings is None:
            bindings = engine.sales_bindings(FSalesOrder, FSalesOrder.customer_id)
    attributes = {} if attributes is None else attributes
    try:
        if bindings is None:
            if definition.query_attributes:
                raise engine.AuthorizationDenied("资源缺少查询范围绑定")
            decision = engine.check_access(principal.id, grant, attributes, (action,))
            if action not in decision.allowed_actions:
                raise engine.AuthorizationDenied("未获资源操作授权")
            predicate, fields = true(), decision.field_permissions
        else:
            plan = engine.plan_access(principal.id, grant, attributes, action, bindings)
            predicate, fields = plan.predicate, plan.field_permissions
    except engine.AuthorizationDenied as exc:
        raise authz_error(403, "AUTHZ_FORBIDDEN", str(exc)) from None
    except engine.AuthorizationUnavailable:
        raise authz_error(503, "AUTHZ_ENGINE_UNAVAILABLE", "授权服务暂不可用，请稍后重试") from None
    return _record(ResourceAccess(resource, action, principal, principal.authorization_epoch, grant.version,
                          CATALOG_VERSION, grant.scope, fields, predicate, object_id, authorization_revision))


def sales_access(db: Session, identity: VerifiedIdentity, legacy_context: UserContext,
                 action: str = 'read_orders') -> SalesAccess:
    access = resource_access(db, identity, SALES_RESOURCE, action)
    context = field_context(access, legacy_context)
    count = db.scalar(select(func.count()).select_from(SysCustomerAssignment).where(
        SysCustomerAssignment.user_id == access.principal.id))
    return SalesAccess(access.predicate, context, access.scope, count, access)


def _reason(code, message):
    return {"code": code, "message": message}


def my_snapshot(db: Session, identity: VerifiedIdentity) -> dict:
    states = runtime.runtime_states(db)
    principal = _read_principal(db, identity)
    rows = {} if principal is None else {row.resource: row for row in db.scalars(
        select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == principal.id)
        .execution_options(populate_existing=True))}
    checked, invalid = {}, set()
    for resource, row in rows.items():
        if resource not in states or states[resource].runtime_state != "enabled":
            continue
        try:
            checked[resource] = _grant(row)
        except (ValueError, TypeError):
            invalid.add(resource)
    # Module probes do not enumerate domain objects or generate query plans.
    attributes = {resource: ({"customer_ids": []} if resource == SALES_RESOURCE else {}) for resource in checked}
    try:
        decisions = engine.check_resources(principal.id, checked, attributes, {
            resource: tuple(sorted(get_resource_definition(resource).actions)) for resource in checked
        }) if principal and checked else {}
    except (engine.AuthorizationUnavailable, engine.AuthorizationDenied):
        raise authz_error(503, "AUTHZ_ENGINE_UNAVAILABLE", "授权服务暂不可用，请稍后重试") from None
    customer_count = 0
    if principal and SALES_RESOURCE in checked:
        customer_count = db.scalar(select(func.count()).select_from(SysCustomerAssignment).where(
            SysCustomerAssignment.user_id == principal.id))
    resources = {}
    for descriptor in resource_descriptors():
        resource = descriptor["id"]
        state = states[resource]
        row, grant = rows.get(resource), checked.get(resource)
        reason = _reason("NOT_GRANTED", "未获得此资源动作授权")
        if state.migration_state == "planned":
            reason = _reason("MODULE_NOT_MIGRATED", "此模块仍使用原有授权")
        elif state.migration_state == "retired":
            reason = _reason("RESOURCE_RETIRED", "功能已退役")
        elif state.runtime_state == "legacy":
            reason = _reason("MODULE_NOT_ENABLED", "模块尚未切换新授权")
        elif state.runtime_state == "suspended":
            reason = _reason("MODULE_SUSPENDED", "模块已暂停")
        elif principal is None:
            reason = _reason("NAMED_ACCOUNT_REQUIRED", "需要有效实名账号")
        elif resource in invalid:
            reason = _reason("INVALID_GRANT", "授权配置无效")
        elif row is None:
            reason = _reason("NO_GRANT", "未获得资源授权")
        decision = decisions.get(resource)
        action_decisions = {}
        for action_descriptor in descriptor["actions"]:
            action = action_descriptor["id"]
            allowed = decision is not None and action in decision.allowed_actions
            definition = get_resource_definition(resource) if grant is not None else None
            needs_context = bool(allowed and grant.scope in definition.context_scopes
                                 and action not in definition.display_actions)
            action_reason = reason if not allowed else None
            if needs_context:
                action_reason = _reason("OBJECT_CONTEXT_REQUIRED", "需要检查当前业务对象")
            elif allowed and resource == SALES_RESOURCE and grant.scope == "assigned_customers" and customer_count == 0:
                action_reason = _reason("EMPTY_ASSIGNED_SCOPE", "尚未分配客户，当前数据范围为空")
            action_decisions[action] = {"effect": "requires_context" if needs_context else "allow" if allowed else "deny",
                                        "reason": action_reason}
        has_allow = any(item["effect"] in ("allow", "requires_context") for item in action_decisions.values())
        resources[resource] = {
            "migration_state": state.migration_state, "runtime_state": state.runtime_state,
            "configurable": state.configurable, "grant_version": row.version if row else 0,
            "decisions": action_decisions, "scope_kind": grant.scope if has_allow else "none",
            "field_permissions": sorted(decision.field_permissions) if has_allow else [],
        }
    sales = resources[SALES_RESOURCE]
    read = any(sales['decisions'][action]['effect'] == 'allow' for action in (
        'read_summary', 'read_groups', 'read_trend', 'read_orders', 'read_order_detail'))
    sales_reason = (next((d['reason'] for d in sales['decisions'].values()
                          if d['effect'] == 'allow' and d['reason']), None)
                    if read else sales['decisions']['read_orders']['reason'])
    version = hashlib.sha256(json.dumps({
        "catalog": CATALOG_VERSION, "mode": runtime.authorization_mode(),
        "user": principal.id if principal else None, "epoch": principal.authorization_epoch if principal else None,
        "resources": resources,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**envelope(), "version": version, "username": principal.username if principal else None,
            "principal": {"id": principal.id, "username": principal.username, "display_name": principal.display_name} if principal else None,
            "authorization_epoch": principal.authorization_epoch if principal else None,
            "actor_capabilities": actor_capabilities(principal), "can_manage": bool(principal and principal.role == "admin"),
            "resources": resources, "sales_analysis": {
                "enabled": sales["runtime_state"] != "legacy", "read": read,
                "export": sales["decisions"]["export"]["effect"] == "allow",
                "scope": sales["scope_kind"], "customer_count": customer_count if read else 0,
                "field_permissions": sales["field_permissions"],
                "reason": sales_reason["message"] if sales_reason else "已获得显式授权",
            }}


def catalog_snapshot(db: Session) -> dict:
    states = runtime.runtime_states(db)
    descriptors = resource_descriptors()
    for descriptor in descriptors:
        state = states[descriptor["id"]]
        descriptor.update(runtime_state=state.runtime_state, configurable=state.configurable)
    return {**envelope(), "modules": descriptors}


def _validate_domain_context(context: ResolvedContext) -> None:
    """Closed entry/branch obligations, rather than a union of permitted kinds."""
    from app.authz.context_registry import ENTRY_CONTRACTS
    contract = ENTRY_CONTRACTS.get(context.entry_id)
    branch = contract and contract['branches'].get(context.branch)
    if (branch is None or {r: set(a) for r, a in branch['requirements'].items()} !=
            {r: set(a) for r, a in context.requirements.items()}
            or set(branch['slots']) != set(context.slots)):
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '业务入口与完整领域证据不匹配')
    if context.empty_target_set and not contract.get('empty_target_set_allowed'):
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '此操作不接受空目标')
    carrier_slots = branch.get('carrier_slots', branch['slots'])
    if not any(context.ref in context.slots[key] for key in carrier_slots):
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '业务载体不在已验证证据中')
    for key, refs in context.slots.items():
        slot = contract['slots'][key]
        expected_kind = slot.get('object_type') if slot['kind'] == 'object' else slot['kind']
        for ref in refs:
            if (ref.kind != expected_kind
                    or (slot.get('collection_ref') is not None and ref.id != slot['collection_ref'])
                    or (slot.get('purpose') is not None and ref.selector.get('purpose') != slot['purpose'])):
                raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '业务证据类型或用途不匹配')
    for resource, actions in context.requirements.items():
        definition = get_resource_definition(resource)
        if not definition.context_scopes or not set(actions) <= definition.actions:
            raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '资源动作不接受此对象上下文')
        expected = {a.removeprefix('request.resource.attr.') for a in definition.query_attributes}
        if any(set(subject.attributes) != expected for subject in context.policy_subjects[resource]):
            raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '业务关系属性与资源不匹配')


def _context_grants(db, identity, batch, *, read_snapshot=None, locked_user=None):
    if type(batch) is not ContextBatch:
        raise ValueError('必须使用受控领域批次')
    batch.validate_observation(db)
    if read_snapshot is None and locked_user is None:
        raise ValueError('领域读取需要一致快照，写入需要完整锁集中的主体')
    for context in batch.contexts:
        _validate_domain_context(context)
    requested = {r for c in batch.contexts for r in c.requirements}
    states = runtime.runtime_states(db)
    if any(states[r].runtime_state != 'enabled' for r in requested):
        raise authz_error(503, 'AUTHZ_RESOURCE_UNAVAILABLE', '授权模块未启用或已暂停')
    principal = _read_principal(db, identity, locked_user=locked_user, read_snapshot=read_snapshot)
    if principal is None:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '此操作需要有效实名账号')
    rows = db.scalars(select(SysAuthorizationGrant).where(
        SysAuthorizationGrant.user_id == principal.id, SysAuthorizationGrant.resource.in_(requested))
        .execution_options(populate_existing=True))
    try:
        grants = {row.resource: _grant(row) for row in rows}
    except (ValueError, TypeError):
        raise authz_error(403, 'AUTHZ_INVALID_CONFIGURATION', '资源授权配置无效') from None
    return principal, grants


@dataclass(frozen=True)
class ContextAccessSet:
    principal: Principal
    batch: ContextBatch
    accesses: Mapping[tuple[str, str, str], ResourceAccess]
    grant_versions: Mapping[str, int]

    def __post_init__(self):
        object.__setattr__(self, 'accesses', MappingProxyType(dict(self.accesses)))
        object.__setattr__(self, 'grant_versions', MappingProxyType(dict(self.grant_versions)))

    def _context(self, ref):
        if type(ref) is not ObjectRef:
            raise ValueError('必须使用完整的业务对象引用')
        for context in self.batch.contexts:
            if context.ref == ref:
                return context
        raise ValueError('对象未登记在本次授权批次')

    def get(self, ref: ObjectRef, resource: str, action: str) -> ResourceAccess | None:
        context = self._context(ref)
        if action not in context.requirements.get(resource, ()):
            raise ValueError('动作未登记在本次对象授权批次')
        return self.accesses.get((ref.key, resource, action))

    def require(self, ref: ObjectRef, resource: str, action: str) -> ResourceAccess:
        access = self.get(ref, resource, action)
        if access is None:
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '未获得当前业务对象的操作权限')
        return access

    def wire(self, ref: ObjectRef) -> dict:
        context = self._context(ref)
        resources = {}
        for resource, actions in context.requirements.items():
            decisions, fields = {}, None
            for action in actions:
                access = self.get(ref, resource, action)
                decisions[action] = {'effect': 'allow' if access else 'deny', 'reason': None if access else {
                    'code': 'NOT_GRANTED', 'message': '未获得当前业务对象的操作权限'}}
                if access is not None:
                    fields = access.field_permissions if fields is None else fields & access.field_permissions
            resources[resource] = {'grant_version': self.grant_versions.get(resource), 'decisions': decisions,
                                   'field_permissions': sorted(fields or ())}
        return {'schema_version': 1, 'catalog_version': CATALOG_VERSION,
                'authorization_epoch': self.principal.authorization_epoch,
                'context': {**ref.wire(), 'authorization_revision': context.authorization_revision},
                'resources': resources}


def context_resolution(accesses: ContextAccessSet, ref: ObjectRef) -> dict:
    """Closed authorization-view envelope shared with the browser validator."""
    if type(accesses) is not ContextAccessSet or type(ref) is not ObjectRef:
        raise ValueError('上下文响应需要真实授权批次和载体')
    locator = ({'project_id': ref.id} if ref.kind == 'project' else
               {'collection_ref': ref.id} if ref.kind == 'collection' else
               {'object_type': ref.kind, 'object_id': ref.id})
    return {'locator': locator, 'authorization_context': accesses.wire(ref)}


def context_accesses(db: Session, identity: VerifiedIdentity, batch: ContextBatch, *,
                     read_snapshot: ReadSnapshotProof | None = None, locked_user=None) -> ContextAccessSet:
    principal, grants = _context_grants(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    checks, keys = [], {}
    empty = {}
    for index, context in enumerate(batch.contexts):
        for resource, actions in context.requirements.items():
            if resource not in grants:
                continue
            subjects = context.policy_subjects[resource]
            if not subjects:
                if not context.empty_target_set:
                    raise ValueError('集合只能通过已绑定 SQL 查询计划取得许可')
                # Only the exact registered, actually parsed empty operation can
                # use this admission. Empty ALL must not bypass a live Grant.
                empty.setdefault(resource, set()).update(actions)
            for subject in subjects:
                key = f'{index}:{resource}:{subject.id}'
                checks.append(engine.ObjectCheck(key, resource, actions, subject.attributes))
                keys[(context.ref.key, resource, subject.id)] = key
    try:
        decisions = engine.check_objects(principal.id, {r: grants[r] for r in {c.resource for c in checks}}, checks)
        admissions = engine.check_resources(principal.id, {r: grants[r] for r in empty},
            {r: {} for r in empty}, {r: tuple(sorted(actions)) for r, actions in empty.items()}) if empty else {}
    except (engine.AuthorizationDenied, engine.AuthorizationUnavailable):
        raise authz_error(503, 'AUTHZ_ENGINE_UNAVAILABLE', '授权服务暂不可用，请稍后重试') from None
    # No record, DTO or partial allow is observable until every PDP batch passed.
    accesses = {}
    for context in batch.contexts:
        for resource, actions in context.requirements.items():
            grant = grants.get(resource)
            if grant is None:
                continue
            evaluated = [decisions[keys[(context.ref.key, resource, subject.id)]]
                         for subject in context.policy_subjects[resource]]
            if not evaluated:
                evaluated = [admissions[resource]]
            for action in actions:
                if not all(action in d.allowed_actions for d in evaluated):
                    continue
                fields = frozenset.intersection(*(d.field_permissions for d in evaluated))
                access = ResourceAccess(resource, action, principal, principal.authorization_epoch, grant.version,
                    CATALOG_VERSION, grant.scope, fields, true(), context.ref.id, context.authorization_revision,
                    context.ref, context.entry_id, context.branch)
                accesses[(context.ref.key, resource, action)] = access
    for access in accesses.values():
        _record(access)
    return ContextAccessSet(principal, batch, accesses, {r: g.version for r, g in grants.items()})


def context_query_access(db: Session, identity: VerifiedIdentity, batch: ContextBatch,
                         ref: ObjectRef, resource: str, action: str, bindings: engine.QueryBindings, *,
                         read_snapshot: ReadSnapshotProof | None = None, locked_user=None) -> ResourceAccess:
    """A registered collection plan; its existence never allows a child command."""
    principal, grants = _context_grants(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    context = next((c for c in batch.contexts if c.ref == ref), None)
    if (context is None or ref.kind != 'collection' or action not in context.requirements.get(resource, ())):
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '集合查询必须绑定已登记入口及选择范围')
    grant = grants.get(resource)
    if grant is None:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '未获得此集合查询权限')
    try:
        plan = engine.plan_access(principal.id, grant, {}, action, bindings)
    except engine.AuthorizationDenied:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '未获得此集合查询权限') from None
    except engine.AuthorizationUnavailable:
        raise authz_error(503, 'AUTHZ_ENGINE_UNAVAILABLE', '授权服务暂不可用，请稍后重试') from None
    return _record(ResourceAccess(resource, action, principal, principal.authorization_epoch, grant.version,
        CATALOG_VERSION, grant.scope, plan.field_permissions, plan.predicate, ref.id,
        context.authorization_revision, ref, context.entry_id, context.branch))


def _check_precondition_wire(value, expected) -> None:
    if value is None:
        return
    if (type(value) is not dict or set(value) != {'catalog_version', 'authorization_epoch', 'context'}
            or type(value.get('authorization_epoch')) is not int
            or value != {key: expected[key] for key in ('catalog_version', 'authorization_epoch', 'context')}):
        raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '权限或业务关系已变化，请重新加载后操作')


def check_context_precondition(value, access_set: ContextAccessSet, ref: ObjectRef) -> None:
    """Optional old-client OCC input; always call only after current authorization."""
    _check_precondition_wire(value, access_set.wire(ref))


def check_resource_precondition(value, access: ResourceAccess, *, object_ref=None,
                                authorization_revision=None) -> None:
    """Document adapter supplies its current carrier after complete document checks.

    This is only optimistic concurrency control. It cannot grant access and must
    follow live authorization in the same snapshot that materializes the file.
    """
    if type(access) is not ResourceAccess:
        raise ValueError('文档前提检查必须来自实际领域许可')
    ref = object_ref if object_ref is not None else access.object_ref
    revision = authorization_revision if authorization_revision is not None else access.authorization_revision
    if type(ref) is not ObjectRef or type(revision) is not str or not revision:
        raise ValueError('文档前提检查缺少实际载体和版本')
    _check_precondition_wire(value, {'catalog_version': access.catalog_version,
        'authorization_epoch': access.authorization_epoch,
        'context': {**ref.wire(), 'authorization_revision': revision}})


def context_query_accesses(db: Session, identity: VerifiedIdentity, batch: ContextBatch,
                           bindings_by_resource: Mapping[tuple[str, str], engine.QueryBindings], *,
                           read_snapshot: ReadSnapshotProof | None = None, locked_user=None) -> ContextAccessSet:
    principal, grants = _context_grants(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    requests, request_keys = {}, {}
    expected_bindings = set()
    for index, context in enumerate(batch.contexts):
        if context.ref.kind != 'collection':
            raise ValueError('集合批量计划不能代替业务对象授权')
        for resource, actions in context.requirements.items():
            binding_key = (context.ref.key, resource)
            expected_bindings.add(binding_key)
            binding = bindings_by_resource.get(binding_key)
            if type(binding) is not engine.QueryBindings or binding.resource != resource:
                raise ValueError('集合批次缺少完整查询绑定')
            if resource not in grants:
                continue
            for action in actions:
                key = f'{index}:{resource}:{action}'
                requests[key] = (resource, action, binding)
                request_keys[key] = (context, resource, action)
    if set(bindings_by_resource) != expected_bindings:
        raise ValueError('集合批次包含额外查询绑定')
    try:
        plans = engine.plan_accesses(principal.id, {r: grants[r] for r, _, _ in requests.values()}, requests)
    except engine.AuthorizationDenied:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '集合查询授权无效') from None
    except engine.AuthorizationUnavailable:
        raise authz_error(503, 'AUTHZ_ENGINE_UNAVAILABLE', '授权服务暂不可用，请稍后重试') from None
    accesses = {}
    for key, plan in plans.items():
        context, resource, action = request_keys[key]
        grant = grants[resource]
        accesses[(context.ref.key, resource, action)] = ResourceAccess(resource, action, principal,
            principal.authorization_epoch, grant.version, CATALOG_VERSION, grant.scope, plan.field_permissions,
            plan.predicate, context.ref.id, context.authorization_revision, context.ref, context.entry_id, context.branch)
    for access in accesses.values():
        _record(access)
    return ContextAccessSet(principal, batch, accesses, {r: grant.version for r, grant in grants.items()})
