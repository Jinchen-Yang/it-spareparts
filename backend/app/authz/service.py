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


def _read_principal(db: Session, identity: VerifiedIdentity, *, locked_user=None) -> Principal | None:
    if type(identity) is not VerifiedIdentity:
        raise authz_error(401, "AUTHN_SESSION_INVALID", "需要已验证的类型化身份")
    if identity.fallback:
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
                    locked_user: SysUser | None = None, object_id=None, authorization_revision=None) -> ResourceAccess:
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
    principal = _read_principal(db, identity, locked_user=locked_user)
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
            needs_context = bool(allowed and grant.scope in get_resource_definition(resource).context_scopes)
            action_reason = reason if not allowed else None
            if needs_context:
                action_reason = _reason("OBJECT_CONTEXT_REQUIRED", "需要检查当前业务对象")
            elif allowed and resource == SALES_RESOURCE and grant.scope == "assigned_customers" and customer_count == 0:
                action_reason = _reason("EMPTY_ASSIGNED_SCOPE", "尚未分配客户，当前数据范围为空")
            action_decisions[action] = {"effect": "requires_context" if needs_context else "allow" if allowed else "deny",
                                        "reason": action_reason}
        has_allow = any(item["effect"] == "allow" for item in action_decisions.values())
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
