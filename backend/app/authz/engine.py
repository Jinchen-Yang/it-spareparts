"""Cerbos boundary: trusted grants in, decisions and SQL predicates out.

No DB access, token interpretation, implicit roles, or network-error fallback.
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import logging
from types import MappingProxyType

from cerbos.sdk.client import CerbosClient
from cerbos.sdk.model import Effect, Principal, Resource, ResourceDesc, ResourceList
from cerbos_sqlalchemy import get_query
from sqlalchemy import false, true

from app.authz.catalog import SALES_RESOURCE, get_resource_definition, validate_grant
from app.config import get_settings

_log = logging.getLogger(__name__)


class AuthorizationDenied(Exception):
    """A valid policy decision did not authorize the requested action."""


class AuthorizationUnavailable(Exception):
    """No usable policy decision/plan was obtained; callers must fail closed."""


@dataclass(frozen=True)
class Grant:
    actions: tuple[str, ...]
    scope: str
    field_permissions: tuple[str, ...]
    version: int
    resource: str = field(kw_only=True)

    def __post_init__(self) -> None:
        validate_grant(self.resource, self.actions, self.scope, self.field_permissions)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("授权版本无效")
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "field_permissions", tuple(self.field_permissions))


@dataclass(frozen=True)
class SalesGrant(Grant):
    resource: str = field(default=SALES_RESOURCE, init=False)


@dataclass(frozen=True)
class Decision:
    allowed_actions: frozenset[str]
    field_permissions: frozenset[str]


@dataclass(frozen=True)
class Plan:
    predicate: object
    field_permissions: frozenset[str]


@dataclass(frozen=True)
class QueryBindings:
    """Trusted ORM bindings supplied by a registered module, never request JSON."""
    resource: str
    table: object
    attributes: Mapping
    operator_overrides: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        definition = get_resource_definition(self.resource)
        if set(self.attributes) != definition.query_attributes:
            raise ValueError("查询属性与资源目录不匹配")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        object.__setattr__(self, "operator_overrides", MappingProxyType(dict(self.operator_overrides)))


# Preserve the small module-facing interface during extraction of the shared
# protocol; services do not know the transport/adapter implementation.
SalesDecision = Decision
SalesPlan = Plan


def _make_principal(user_id: int, grant: Grant, attributes: Mapping) -> Principal:
    return _make_batch_principal(user_id, {grant.resource: grant}, {grant.resource: attributes})


def _make_batch_principal(user_id: int, grants: Mapping[str, Grant], attributes: Mapping[str, Mapping]) -> Principal:
    if type(user_id) is not int or user_id < 1:
        raise ValueError("实名用户 ID 无效")
    if set(attributes) != set(grants):
        raise ValueError("资源属性必须与授权逐项配对")
    for resource, grant in grants.items():
        if resource != grant.resource or any(key in attributes[resource] for key in ("grant", "grants", "contexts")):
            raise ValueError("资源属性不能覆盖显式授权")
    return Principal(
        id=str(user_id), roles={"registered_account"},
        attr={"grants": {resource: {
            "actions": list(grant.actions), "scope": grant.scope,
            "field_permissions": list(grant.field_permissions), "version": grant.version,
        } for resource, grant in grants.items()},
              "contexts": {resource: dict(values) for resource, values in attributes.items()}},
    )


def _client() -> CerbosClient:
    settings = get_settings()
    return CerbosClient(
        settings.cerbos_url, timeout_secs=settings.cerbos_timeout_seconds,
        raise_on_error=True, request_retries=0, connection_retries=0,
    )


def _check(client: CerbosClient, principal: Principal, grant: Grant,
           actions: Sequence[str]) -> Decision:
    return _check_batch(client, principal, {grant.resource: grant}, {grant.resource: actions})[grant.resource]


def _module_id(resource: str) -> str:
    return f"{resource.replace('_', '-')}-module"


def _check_batch(client, principal, grants, requested) -> dict[str, Decision]:
    if set(grants) != set(requested):
        raise AuthorizationDenied("授权与请求资源不匹配")
    resources = ResourceList()
    for resource, grant in grants.items():
        actions = requested[resource]
        if (not actions or len(actions) != len(set(actions))
                or not set(actions) <= get_resource_definition(resource).actions):
            raise AuthorizationDenied("未注册的资源动作")
        resources.add(Resource(id=_module_id(resource), kind=resource, attr={"module_probe": True}), set(actions))
    response = client.check_resources(principal, resources)
    response.raise_if_failed()
    results = response.results
    expected = {_module_id(resource): resource for resource in grants}
    if (not isinstance(results, list) or len(results) != len(expected)
            or len({result.resource.id for result in results}) != len(expected)
            or {result.resource.id for result in results} != set(expected)):
        raise AuthorizationUnavailable("授权批量响应缺失、重复或包含额外资源")
    decisions = {}
    for result in results:
        resource = expected[result.resource.id]
        decisions[resource] = _decision(result, grants[resource], requested[resource])
    return decisions


def _decision(result, grant: Grant, actions: Sequence[str]) -> Decision:
    if (result.resource.kind != grant.resource or result.validation_errors
            or set(result.actions) != set(actions)
            or any(effect not in (Effect.ALLOW, Effect.DENY) for effect in result.actions.values())):
        raise AuthorizationUnavailable("授权响应无效")
    allowed = frozenset(action for action in actions if result.is_allowed(action))
    if not allowed <= set(grant.actions):
        raise AuthorizationUnavailable("授权响应超出显式动作")
    if any(entry.action not in allowed for entry in result.outputs or []):
        raise AuthorizationUnavailable("授权输出包含未允许的动作")
    fields: frozenset[str] | None = None
    for action in allowed:
        outputs = [entry for entry in result.outputs or [] if entry.action == action]
        if len(outputs) != 1 or outputs[0].error:
            raise AuthorizationUnavailable("授权字段响应缺失")
        output = outputs[0].val
        if (not isinstance(output, dict) or output.get("resource") != grant.resource
                or output.get("scope") != grant.scope or isinstance(output.get("version"), bool)
                or output.get("version") != grant.version):
            raise AuthorizationUnavailable("授权范围或版本响应不匹配")
        returned = output.get("field_permissions")
        validate_grant(grant.resource, grant.actions, grant.scope, returned)
        current = frozenset(returned)
        if not current <= set(grant.field_permissions) or (fields is not None and fields != current):
            raise AuthorizationUnavailable("授权字段响应不匹配")
        fields = current
    return Decision(allowed, fields or frozenset())


def check_resources(user_id: int, grants: Mapping[str, Grant],
                    principal_attributes: Mapping[str, Mapping],
                    actions: Mapping[str, Sequence[str]]) -> dict[str, Decision]:
    """One connection and one CheckResources call for an entire snapshot."""
    if not grants:
        if principal_attributes or actions:
            raise AuthorizationDenied("空授权请求不能携带额外资源")
        return {}
    try:
        principal = _make_batch_principal(user_id, grants, principal_attributes)
        with _client() as client:
            return _check_batch(client, principal, grants, actions)
    except (AuthorizationDenied, AuthorizationUnavailable):
        raise
    except Exception as exc:
        raise _unavailable(exc) from exc


def _unavailable(exc: Exception) -> AuthorizationUnavailable:
    # Never log subject/grant payloads, HTTP response bodies, or SQL parameters.
    _log.warning("authorization engine unavailable (%s)", type(exc).__name__)
    return AuthorizationUnavailable("授权服务暂不可用")


def check_access(user_id: int, grant: Grant, principal_attributes: Mapping,
                 actions: Sequence[str]) -> Decision:
    try:
        principal = _make_principal(user_id, grant, principal_attributes)
        with _client() as client:
            return _check(client, principal, grant, actions)
    except (AuthorizationDenied, AuthorizationUnavailable):
        raise
    except Exception as exc:
        raise _unavailable(exc) from exc


def plan_access(user_id: int, grant: Grant, principal_attributes: Mapping,
                action: str, bindings: QueryBindings) -> Plan:
    if action not in get_resource_definition(grant.resource).actions:
        raise AuthorizationDenied("未注册的资源动作")
    if bindings.resource != grant.resource:
        raise AuthorizationDenied("查询绑定与授权资源不匹配")
    try:
        principal = _make_principal(user_id, grant, principal_attributes)
        with _client() as client:
            decision = _check(client, principal, grant, (action,))
            if action not in decision.allowed_actions:
                raise AuthorizationDenied("未获资源操作授权")
            plan = client.plan_resources(
                action, principal, ResourceDesc(kind=grant.resource, attr={"module_probe": False}),
            )
            plan.raise_if_failed()
            planned_actions = [plan.action] if isinstance(plan.action, str) else plan.action
            if (plan.validation_errors or plan.resource_kind != grant.resource
                    or planned_actions != [action]):
                raise AuthorizationUnavailable("授权查询计划无效")
        query = get_query(
            plan, bindings.table, bindings.attributes,
            operator_override_fns=bindings.operator_overrides,
        )
        return Plan(
            predicate=query.whereclause if query.whereclause is not None else true(),
            field_permissions=decision.field_permissions,
        )
    except (AuthorizationDenied, AuthorizationUnavailable):
        raise
    except Exception as exc:
        raise _unavailable(exc) from exc


def _sales_attributes(customer_ids: Sequence[int]) -> dict:
    if any(type(value) is not int or value < 1 for value in customer_ids):
        raise ValueError("客户归属 ID 无效")
    # Numeric transport uses doubles; keep DB identifiers lossless strings.
    return {"customer_ids": [str(value) for value in sorted(set(customer_ids))]}


def _principal(user_id: int, grant: SalesGrant, customer_ids: Sequence[int]) -> Principal:
    return _make_principal(user_id, grant, _sales_attributes(customer_ids))


def _customer_id(value):
    if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            or int(value) < 1):
        raise ValueError("授权客户范围计划无效")
    return int(value)


def _customer_in(column, values):
    if not isinstance(values, list):
        raise ValueError("授权客户范围计划无效")
    return column.in_([_customer_id(value) for value in values]) if values else false()


def _customer_eq(column, value):
    # The planner reduces singleton membership to equality. Convert only the
    # constant, preserving the indexed integer customer_id column unchanged.
    return column == _customer_id(value)


def sales_bindings(table, customer_column) -> QueryBindings:
    return QueryBindings(
        SALES_RESOURCE, table, {"request.resource.attr.customer_id": customer_column},
        {"in": _customer_in, "eq": _customer_eq},
    )


def check_sales(user_id: int, grant: SalesGrant, customer_ids: Sequence[int],
                actions: Sequence[str] = ("read_orders", "export")) -> Decision:
    return check_access(user_id, grant, _sales_attributes(customer_ids), actions)


def plan_sales(user_id: int, grant: SalesGrant, customer_ids: Sequence[int],
               action: str, table, customer_column) -> Plan:
    return plan_access(user_id, grant, _sales_attributes(customer_ids), action,
                       sales_bindings(table, customer_column))
