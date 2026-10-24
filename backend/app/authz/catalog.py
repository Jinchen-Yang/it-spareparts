"""Closed executable vocabulary; descriptive metadata never registers a resource.

Adding an existing legacy data key does not automatically register it here.
"""
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from app.authz.business_definitions import BUSINESS_SPECS
from app.authz.maintenance_definitions import MAINTENANCE_SPECS

SALES_RESOURCE = "sales_analysis"
SALES_ACTIONS = frozenset(BUSINESS_SPECS[SALES_RESOURCE]["actions"])
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
    action_field_dependencies: tuple[tuple[str, str], ...] = ()
    denial_scope: str = "all"
    context_scopes: frozenset[str] = frozenset()
    action_any_dependencies: tuple[tuple[str, tuple[str, ...]], ...] = ()
    display_actions: frozenset[str] = frozenset()


# Closed registry. A legacy page key or an arbitrary DB resource string does
# not become a migrated resource merely by being passed to the engine.
RESOURCE_DEFINITIONS = MappingProxyType({
    name: ResourceDefinition(
        name, frozenset(spec["actions"]), frozenset(spec["scopes"]), frozenset(spec["fields"]),
        frozenset({"request.resource.attr.customer_id"}) if name == SALES_RESOURCE else frozenset(spec.get('query_attributes', ())),
        action_dependencies=spec["action_dependencies"], field_dependencies=spec["field_dependencies"],
        action_field_dependencies=spec["action_field_dependencies"],
        action_any_dependencies=spec["action_any_dependencies"],
        denial_scope=spec.get('denial_scope', "assigned_customers" if name == SALES_RESOURCE else "all"),
        context_scopes=frozenset(spec.get('context_scopes', ())),
        display_actions=frozenset(spec.get('display_actions', ())),
    ) for name, spec in (BUSINESS_SPECS | MAINTENANCE_SPECS).items()
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
    for action, alternatives in definition.action_any_dependencies:
        if action in action_set and not action_set.intersection(alternatives):
            raise ValueError(f"{action}必须至少授予一个依赖动作：{'、'.join(alternatives)}")
    for permission, required in definition.field_dependencies:
        if permission in fields and required not in fields:
            raise ValueError(f"{permission}必须同时具有{required}权限")
    for action, required in definition.action_field_dependencies:
        if action in action_set and required not in fields:
            raise ValueError(f"{action}必须同时具有{required}字段权限")


def validate_sales_grant(actions: Sequence[str], scope: str, field_permissions: Sequence[str]) -> None:
    validate_grant(SALES_RESOURCE, actions, scope, field_permissions)


# Planned metadata is deliberately separate from executable definitions. It
# cannot authorize a request or make a database grant valid.
_DESCRIPTORS = json.loads(Path(__file__).with_name("catalog_manifest.json").read_text())
CATALOG_VERSION = "authz-s3-" + hashlib.sha256(json.dumps(
    {"policy_protocol_version": 3, "descriptors": _DESCRIPTORS, "executable": {key: {
        "actions": sorted(value.actions), "scopes": sorted(value.scopes),
        "fields": sorted(value.field_permissions), "action_dependencies": value.action_dependencies,
        "action_any_dependencies": value.action_any_dependencies,
        "field_dependencies": value.field_dependencies, "action_fields": value.action_field_dependencies,
        "denial_scope": value.denial_scope,
        "query_attributes": sorted(value.query_attributes),
        "context_scopes": sorted(value.context_scopes),
        "display_actions": sorted(value.display_actions),
    } for key, value in RESOURCE_DEFINITIONS.items()}},
    sort_keys=True, separators=(",", ":"), ensure_ascii=False,
).encode()).hexdigest()[:24]


def resource_descriptors() -> list[dict]:
    return deepcopy(_DESCRIPTORS)


def validate_catalog() -> None:
    """Reject metadata/execution drift before activation or a legacy decision.

    Descriptions alone cannot register a resource. The two closed sets must
    agree, including every dependency used by the editor and runtime validator.
    """
    def ids(items):
        if type(items) is not list or any(type(x) is not dict or type(x.get("id")) is not str
                                          or not x["id"] for x in items):
            raise ValueError("目录条目必须有具名 ID")
        values = {x["id"] for x in items}
        if len(values) != len(items):
            raise ValueError("目录 ID 不能重复")
        return values

    def acyclic(graph):
        visited, active = set(), set()
        def visit(node):
            if node in active:
                raise ValueError('目录依赖存在环')
            if node in visited:
                return
            active.add(node)
            for dependency in graph.get(node, ()):
                visit(dependency)
            active.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)

    ids(_DESCRIPTORS)
    by_id = {item["id"]: item for item in _DESCRIPTORS}
    implemented = {item["id"] for item in _DESCRIPTORS if item["migration_state"] == "implemented"}
    if implemented != set(RESOURCE_DEFINITIONS):
        raise ValueError("已实施目录必须与执行资源闭集相等")
    for item in _DESCRIPTORS:
        if item["migration_state"] not in ("planned", "implemented", "retired"):
            raise ValueError("目录迁移状态无效")
        actions = ids(item["actions"])
        scopes = ids(item["scopes"])
        fields = ids(item["field_permissions"])
        for action in item["actions"]:
            if action["kind"] not in ("data", "display", "mutation"):
                raise ValueError("目录动作类型无效")
            _registered_values(action["requires_actions"], actions, "目录动作依赖")
            _registered_values(action["requires_any_actions"], actions, "目录动作可选依赖")
            _registered_values(action["requires_fields"], fields, "目录动作字段依赖")
        for field in item["field_permissions"]:
            _registered_values(field["requires_fields"], fields, "目录字段依赖")
        acyclic({a['id']: a['requires_actions'] + a['requires_any_actions'] for a in item['actions']})
        acyclic({f['id']: f['requires_fields'] for f in item['field_permissions']})
        for scope in item["scopes"]:
            if type(scope["requires_context"]) is not bool:
                raise ValueError("目录对象上下文标记无效")
        ids(item["surfaces"])
        for surface in item["surfaces"]:
            _registered_values(surface["display_actions"], actions, "目录展示动作")
            if type(surface["data_requirements"]) is not list:
                raise ValueError("目录数据要求必须是数组")
            for requirement in surface["data_requirements"]:
                other = by_id.get(requirement["resource_id"])
                if other is None:
                    raise ValueError("目录数据要求引用未知资源")
                _registered_values(requirement["actions"], ids(other["actions"]), "目录数据动作")
            if type(surface["route_entry"]) is not bool or type(surface["page_routes"]) is not list:
                raise ValueError("目录页面入口标记无效")
            if any(type(route) is not str or not route.startswith('/') for route in surface["page_routes"]):
                raise ValueError("目录路由无效")
            rule = surface["data_rule"]
            if (type(rule) is not dict or set(rule) != {"anyOf"} or type(rule["anyOf"]) is not list
                    or (item["id"] in implemented and not rule["anyOf"])):
                raise ValueError("目录数据规则必须是非空 OR 数组")
            for alternative in rule["anyOf"]:
                if type(alternative) is not list or not alternative:
                    raise ValueError("目录数据规则不能包含空允许条件")
                for need in alternative:
                    if type(need) is not dict or set(need) != {"resource_id", "actions", "field_permissions"}:
                        raise ValueError("目录数据规则格式无效")
                    other = by_id.get(need["resource_id"])
                    if other is None:
                        raise ValueError("目录数据规则引用未知资源")
                    _registered_values(need["actions"], ids(other["actions"]), "目录数据动作")
                    _registered_values(need["field_permissions"], ids(other["field_permissions"]), "目录数据字段")
        if item["id"] not in implemented:
            continue
        definition = RESOURCE_DEFINITIONS[item["id"]]
        action_dependencies = {(a["id"], dep) for a in item["actions"] for dep in a["requires_actions"]}
        action_fields = {(a["id"], dep) for a in item["actions"] for dep in a["requires_fields"]}
        action_any = {(a["id"], tuple(a["requires_any_actions"])) for a in item["actions"] if a["requires_any_actions"]}
        field_dependencies = {(f["id"], dep) for f in item["field_permissions"] for dep in f["requires_fields"]}
        context_scopes = {s["id"] for s in item["scopes"] if s["requires_context"]}
        display_actions = {a['id'] for a in item['actions'] if a['kind'] == 'display'}
        if (definition.name != item["id"] or actions != definition.actions or scopes != definition.scopes
                or fields != definition.field_permissions or context_scopes != definition.context_scopes
                or (item['id'] in MAINTENANCE_SPECS and display_actions != definition.display_actions)
                or item["denial_scope"] != definition.denial_scope or definition.denial_scope not in scopes
                or action_dependencies != set(definition.action_dependencies)
                or action_any != set(definition.action_any_dependencies)
                or action_fields != set(definition.action_field_dependencies)
                or field_dependencies != set(definition.field_dependencies)):
            raise ValueError("目录动作、范围、字段或依赖与执行定义不一致")
