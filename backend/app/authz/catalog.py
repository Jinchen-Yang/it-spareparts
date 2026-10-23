"""Closed vocabulary for the first migrated resource.

Adding an existing legacy data key does not automatically register it here.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType

SALES_RESOURCE = "sales_analysis"
SALES_ACTIONS = frozenset({"read", "export"})
SALES_SCOPES = frozenset({"all", "assigned_customers"})
SALES_FIELD_PERMISSIONS = frozenset({
    "data_supplier", "data_customer", "data_purchase_cost", "data_profit",
    "data_pool_price_governance", "data_reconciliation",
})


@dataclass(frozen=True)
class ResourceDefinition:
    name: str
    actions: frozenset[str]
    scopes: frozenset[str]
    field_permissions: frozenset[str]
    query_attributes: frozenset[str]
    action_dependencies: tuple[tuple[str, str], ...] = ()
    field_dependencies: tuple[tuple[str, str], ...] = ()


# Closed registry. A legacy page key or an arbitrary DB resource string does
# not become a migrated resource merely by being passed to the engine.
RESOURCE_DEFINITIONS = MappingProxyType({
    SALES_RESOURCE: ResourceDefinition(
        SALES_RESOURCE, SALES_ACTIONS, SALES_SCOPES, SALES_FIELD_PERMISSIONS,
        frozenset({"request.resource.attr.customer_id"}),
        action_dependencies=(("export", "read"),),
        field_dependencies=(("data_profit", "data_purchase_cost"),
                            ("data_reconciliation", "data_purchase_cost")),
    ),
})


def get_resource_definition(resource: str) -> ResourceDefinition:
    if not isinstance(resource, str) or resource not in RESOURCE_DEFINITIONS:
        raise ValueError("授权资源未注册")
    return RESOURCE_DEFINITIONS[resource]


def _registered_values(values: Sequence[str], allowed: frozenset[str], label: str) -> set[str]:
    if not isinstance(values, (list, tuple)) or any(type(item) is not str for item in values):
        raise ValueError(f"{label}必须是字符串列表")
    if len(set(values)) != len(values) or not set(values) <= allowed:
        raise ValueError(f"{label}包含重复或未注册项")
    return set(values)


def validate_grant(resource: str, actions: Sequence[str], scope: str,
                   field_permissions: Sequence[str]) -> None:
    definition = get_resource_definition(resource)
    action_set = _registered_values(actions, definition.actions, "动作")
    fields = _registered_values(field_permissions, definition.field_permissions, "字段权限")
    if not isinstance(scope, str) or scope not in definition.scopes:
        raise ValueError("数据范围未注册")
    for action, required in definition.action_dependencies:
        if action in action_set and required not in action_set:
            raise ValueError(f"{action}必须同时授予{required}权限")
    for permission, required in definition.field_dependencies:
        if permission in fields and required not in fields:
            raise ValueError(f"{permission}必须同时具有{required}权限")


def validate_sales_grant(actions: Sequence[str], scope: str, field_permissions: Sequence[str]) -> None:
    validate_grant(SALES_RESOURCE, actions, scope, field_permissions)
