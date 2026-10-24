"""Maintenance domain evidence, SQL scopes and complete-context execution.

The core owns identity, grants and PDP decisions. This module owns actual
project relationships and domain resolver registration. Legacy permission maps
never become canonical identity, scope or object evidence.
"""
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import json
import math
import re
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import exists, false, func, literal, select
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.catalog import get_resource_definition
from app.authz.context import (
    ContextBatch, ObjectRef, PolicySubject, ReadSnapshotProof, ResolvedContext,
    canonical_json, create_context_batch, read_snapshot, relation_revision,
)
from app.authz.context_registry import CLOSED_KINDS, COLLECTION_REFS, ENTRY_CONTRACTS
from app.authz.engine import QueryBindings
from app.authz.errors import authz_error
from app.authz.principal import Principal, VerifiedIdentity, load_principal
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectUserAssignment
from app.models.system import SysUser


@dataclass(frozen=True)
class ProjectEvidence:
    project_id: str
    subject: PolicySubject
    authorization_revision: str
    content_revision: str


@dataclass(frozen=True)
class ProjectReadPlan:
    access: service.ResourceAccess
    project_ids_query: object

    def condition(self, project_id_column):
        return project_id_column.in_(self.project_ids_query)


@dataclass(frozen=True)
class DocumentAccess:
    ref: ObjectRef
    authorization_revision: str
    accesses: tuple[service.ResourceAccess, ...]
    content_manifest: tuple[ObjectRef, ...]
    content_revision: str

    def __post_init__(self):
        object.__setattr__(self, 'accesses', tuple(self.accesses))
        object.__setattr__(self, 'content_manifest', tuple(self.content_manifest))
        if (type(self.ref) is not ObjectRef or not self.authorization_revision
                or type(self.content_revision) is not str or not self.content_revision
                or not self.accesses or not self.content_manifest
                or self.ref not in self.content_manifest
                or any(type(ref) is not ObjectRef for ref in self.content_manifest)):
            raise ValueError('完整文档许可必须保留真实载体和全部内容依赖')


@dataclass(frozen=True)
class LockedContextBatch:
    batch: ContextBatch
    accesses: service.ContextAccessSet
    actor: SysUser
    domain_state: object


_ENTRY_RESOLVERS: dict[str, Callable] = {}
_REFERENCE_RESOLVERS: dict[str, Callable] = {}
_DOCUMENT_RESOLVERS: dict[str, Callable] = {}
_QUERY_BINDINGS: dict[str, Callable] = {}


def _named_principal(db, identity, *, read_snapshot=None, locked_user=None) -> Principal:
    if type(identity) is not VerifiedIdentity:
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '需要已验证的类型化身份')
    if read_snapshot is not None:
        if type(read_snapshot) is not ReadSnapshotProof:
            raise ValueError('领域读取需要核心一致读证明')
        read_snapshot.validate(db)
    elif locked_user is None:
        raise ValueError('领域证据必须在一致读事务或完整写锁集内解析')
    principal = load_principal(db, identity, locked_user=locked_user)
    if principal is None:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '此操作需要有效实名账号')
    return principal


def project_evidence(db: Session, principal: Principal, project_ids: Sequence[str]) -> Mapping[str, ProjectEvidence]:
    """Batch actual M/V/S/active and opaque revision, with no descriptive facts.

    The caller must be in the same read snapshot or complete locked write
    transaction used for grants and facts. Assignments of all accounts enter the
    revision so a topology change cannot disappear behind the current actor.
    """
    if type(principal) is not Principal:
        raise ValueError('项目关系只接受实时主体')
    if any(type(value) is not str or not value for value in project_ids):
        raise ValueError('项目标识无效')
    ids = sorted(set(project_ids))
    if not ids:
        return MappingProxyType({})
    rows = list(db.execute(select(
        MaintenanceProject.project_id, MaintenanceProject.version,
        MaintenanceProject.is_active, MaintenanceProject.lifecycle_status,
        MaintenanceProject.salesperson, MaintenanceProject.salesperson_override_active,
    ).where(MaintenanceProject.project_id.in_(ids))))
    if {row.project_id for row in rows} != set(ids):
        raise authz_error(404, 'MAINTENANCE_PROJECT_NOT_FOUND', '维保项目不存在')
    assignments = {project_id: [] for project_id in ids}
    for row in db.execute(select(
        MaintenanceProjectUserAssignment.project_id,
        MaintenanceProjectUserAssignment.assignment_id,
        MaintenanceProjectUserAssignment.user_id,
        MaintenanceProjectUserAssignment.responsibility_type,
        MaintenanceProjectUserAssignment.version,
        MaintenanceProjectUserAssignment.archived_at,
    ).where(MaintenanceProjectUserAssignment.project_id.in_(ids))
      .order_by(MaintenanceProjectUserAssignment.assignment_id)):
        assignments[row.project_id].append(row)
    result = {}
    for project in rows:
        links = assignments[project.project_id]
        active = {row.responsibility_type for row in links
                  if row.user_id == principal.id and row.archived_at is None}
        attrs = {
            'is_manager': 'primary_manager' in active,
            'is_viewer': 'viewer' in active,
            'is_sales': bool(principal.salesperson_name and project.salesperson == principal.salesperson_name),
            'is_active': project.is_active is True,
        }
        content_revision = relation_revision({
            'project': list(project),
            'assignments': [[r.assignment_id, r.user_id, r.responsibility_type, r.version,
                             r.archived_at.isoformat() if r.archived_at else None] for r in links],
        })
        revision = relation_revision({
            'content_revision': content_revision,
            'actor': [principal.id, principal.authorization_epoch, principal.salesperson_name],
        })
        result[project.project_id] = ProjectEvidence(project.project_id,
            PolicySubject(project.project_id, attrs), revision, content_revision)
    return MappingProxyType(result)


def resolve_project_subjects(db, identity, project_ids, *, read_snapshot=None, locked_user=None):
    """Shared workflow helper: live principal and per-project evidence, no roles."""
    principal = _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    return principal, project_evidence(db, principal, project_ids)


def project_relation_table(principal: Principal):
    """Derived Boolean columns satisfy the official adapter's .table contract."""
    if type(principal) is not Principal:
        raise ValueError('项目范围需要实时主体')
    project = MaintenanceProject
    assignment = MaintenanceProjectUserAssignment
    def related(kind):
        return exists(select(assignment.assignment_id).where(
            assignment.project_id == project.project_id,
            assignment.user_id == principal.id,
            assignment.responsibility_type == kind,
            assignment.archived_at.is_(None),
        )).correlate(project)
    sales = (project.salesperson == principal.salesperson_name) if principal.salesperson_name else false()
    return select(project.project_id.label('project_id'),
        related('primary_manager').label('is_manager'), related('viewer').label('is_viewer'),
        func.coalesce(sales, false()).label('is_sales'), project.is_active.label('is_active'),
    ).subquery('maintenance_authorization_projects')


def project_query_bindings(principal, resource):
    table = project_relation_table(principal)
    attributes = {f'request.resource.attr.{name}': table.c[name]
                  for name in ('is_manager', 'is_viewer', 'is_sales', 'is_active')}
    return QueryBindings(resource, table, attributes), table.c.project_id


def register_query_bindings(resource, factory):
    if get_resource_definition(resource) is None or not callable(factory):
        raise ValueError('未登记的查询族')
    if resource in _QUERY_BINDINGS and _QUERY_BINDINGS[resource] is not factory:
        raise ValueError('查询族已有唯一绑定')
    _QUERY_BINDINGS[resource] = factory


def query_bindings(db, principal, context, resource):
    if resource in _QUERY_BINDINGS:
        return _QUERY_BINDINGS[resource](db, principal, context, resource)
    attributes = get_resource_definition(resource).query_attributes
    if not attributes:
        return QueryBindings(resource, select(literal(True).label('present')).subquery(), {})
    expected = {f'request.resource.attr.{name}' for name in ('is_manager', 'is_viewer', 'is_sales', 'is_active')}
    if set(attributes) != expected:
        raise authz_error(503, 'AUTHZ_DOMAIN_RESOLVER_UNAVAILABLE', '领域查询族尚未注册')
    return project_query_bindings(principal, resource)[0]


def project_condition(access):
    """Use the exact derived table present in the returned PlanResources SQL."""
    from sqlalchemy.sql import visitors
    tables = {column.table for column in visitors.iterate(access.predicate)
              if getattr(column, 'table', None) is not None
              and getattr(column.table, 'name', None) == 'maintenance_authorization_projects'}
    if len(tables) > 1:
        raise authz_error(503, 'AUTHZ_INVALID_SCOPE_PLAN', '项目查询计划包含不一致的关系表')
    if not tables:
        # An all/deny plan can compile to a constant. It still needs the same
        # relation projection, with the current principal and no role shortcut.
        table = project_relation_table(access.principal)
    else:
        table = tables.pop()
    return MaintenanceProject.project_id.in_(select(table.c.project_id).where(access.predicate))


def project_read_plan(db, identity, batch, ref, resource, action, *, read_snapshot=None, locked_user=None):
    principal = _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    bindings, project_id = project_query_bindings(principal, resource)
    access = service.context_query_access(db, identity, batch, ref, resource, action, bindings,
                                         read_snapshot=read_snapshot, locked_user=locked_user)
    return ProjectReadPlan(access, select(project_id).where(access.predicate))


def project_collection_revision(db, principal, ref):
    """Bind query output to current project and actual fact-parent generations."""
    from app.services.maintenance_authorization_relations import collection_relation_fingerprints
    return relation_revision({'carrier': ref.to_dict(),
        'relationships': collection_relation_fingerprints(db, ref.id),
        'actor': [principal.id, principal.authorization_epoch, principal.salesperson_name]})


def _selection_value(value, schema):
    """Validate the closed JSON-schema subset emitted by our domain registry."""
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:
                return _selection_value(value, option)
            except (ValueError, TypeError):
                pass
        raise ValueError('选择参数类型不匹配')
    kind = schema.get('type')
    if kind == 'null':
        if value is not None: raise ValueError('需要空值')
    elif kind == 'object':
        if not isinstance(value, Mapping): raise ValueError('需要对象')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is not False:
            raise ValueError('选择器必须是封闭对象')
        if set(value) - set(properties) or set(schema.get('required', ())) - set(value):
            raise ValueError('选择参数含未知字段或缺少必填字段')
        value = {key: _selection_value(item, properties[key]) for key, item in value.items()}
        for key, child in properties.items():
            if key not in value and 'default' in child:
                value[key] = _selection_value(child['default'], child)
    elif kind == 'array':
        if not isinstance(value, (list, tuple)): raise ValueError('需要数组')
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', math.inf):
            raise ValueError('数组长度无效')
        value = [_selection_value(item, schema['items']) for item in value]
        if schema.get('uniqueItems') and len({canonical_json(v) for v in value}) != len(value):
            raise ValueError('数组含重复项')
    elif kind == 'string':
        if type(value) in (date, datetime, UUID): value = str(value)
        if type(value) is not str: raise ValueError('需要字符串')
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', math.inf):
            raise ValueError('字符串长度无效')
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            raise ValueError('字符串格式无效')
        if schema.get('format') == 'date': date.fromisoformat(value)
        elif schema.get('format') == 'date-time': datetime.fromisoformat(value.replace('Z', '+00:00'))
        elif schema.get('format') == 'uuid': UUID(value)
    elif kind in ('integer', 'number'):
        if type(value) not in ((int,) if kind == 'integer' else (int, float)):
            raise ValueError('需要数值')
        if not math.isfinite(value) or (type(value) is int and abs(value) >= 2**53):
            raise ValueError('数值超出可精确表示范围')
        if value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise ValueError('数值范围无效')
        if value <= schema.get('exclusiveMinimum', -math.inf) or value >= schema.get('exclusiveMaximum', math.inf):
            raise ValueError('数值范围无效')
    elif kind == 'boolean':
        if type(value) is not bool: raise ValueError('需要布尔值')
    else:
        raise ValueError('选择器类型未登记')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('选择值未登记')
    return value


def register_entry_resolver(entry_id, resolver, *, reference_resolver=None):
    """Server-only registration; callbacks resolve stored topology, never JSON proof."""
    if entry_id not in ENTRY_CONTRACTS or not callable(resolver):
        raise ValueError('未登记的领域入口')
    if entry_id in _ENTRY_RESOLVERS and _ENTRY_RESOLVERS[entry_id] is not resolver:
        raise ValueError('领域入口已有唯一解析器')
    _ENTRY_RESOLVERS[entry_id] = resolver
    if reference_resolver is not None:
        if not callable(reference_resolver):
            raise ValueError('历史来源解析器无效')
        _REFERENCE_RESOLVERS[entry_id] = reference_resolver


def register_document_resolver(format_purpose, resolver):
    if type(format_purpose) is not str or not format_purpose or not callable(resolver):
        raise ValueError('文档格式解析器无效')
    if format_purpose in _DOCUMENT_RESOLVERS and _DOCUMENT_RESOLVERS[format_purpose] is not resolver:
        raise ValueError('文档格式已有唯一解析器')
    _DOCUMENT_RESOLVERS[format_purpose] = resolver


def slot_ref(entry_id, slot_name, object_id, *, selection=None):
    slot = ENTRY_CONTRACTS[entry_id]['slots'][slot_name]
    try:
        selection = _selection_value(dict(selection or {}), slot['selector_schema'])
    except (ValueError, TypeError):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '领域上下文选择参数无效') from None
    selector = {}
    if slot.get('purpose') is not None:
        selector['purpose'] = slot['purpose']
    if selection:
        selector['selection'] = dict(selection)
    kind = slot.get('object_type') if slot['kind'] == 'object' else slot['kind']
    if kind == 'collection' and object_id != slot['collection_ref']:
        raise ValueError('集合用途与入口不匹配')
    return ObjectRef(kind, object_id, selector)


def resolved_context(db, *, entry_id, branch, carrier, slots, subjects, revision, empty_target_set=False):
    """Construct the shared type from a fully resolved domain plan.

    No caller-supplied attributes or stored authorization_revision may enter
    this constructor without actual domain recomputation.
    """
    contract = ENTRY_CONTRACTS.get(entry_id, {}).get('branches', {}).get(branch)
    if contract is None:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '未登记的领域授权分支')
    return ResolvedContext(ref=carrier, authorization_revision=revision,
        policy_subjects=subjects, requirements={r: tuple(aa) for r, aa in contract['requirements'].items()},
        entry_id=entry_id, branch=branch, slots=slots, empty_target_set=empty_target_set)


def resolve_entry_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    principal = _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    if entry_id in _ENTRY_RESOLVERS:
        batch = _ENTRY_RESOLVERS[entry_id](db, identity, entry_id=entry_id, params=params,
            branch=branch, read_snapshot=read_snapshot, locked_user=locked_user)
        if type(batch) is not ContextBatch:
            raise ValueError('领域解析器必须返回核心 ContextBatch')
        return batch
    contract = ENTRY_CONTRACTS.get(entry_id)
    selected = branch or ('default' if contract and 'default' in contract['branches'] else None)
    rule = contract and contract['branches'].get(selected)
    if rule is None:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '此入口需要真实输入选择授权分支')
    if rule['slots'] and all(contract['slots'][key]['kind'] == 'collection'
                              and contract['slots'][key]['authorizing'] for key in rule['slots']):
        slots = {}
        for key in rule['slots']:
            slot = contract['slots'][key]
            selection = {name: params[name] for name in slot['selector_schema']['properties'] if name in params}
            slots[key] = (slot_ref(entry_id, key, slot['collection_ref'], selection=selection),)
        carrier = slots[rule['carrier_slots'][0]][0]
        subjects = {resource: (() if get_resource_definition(resource).query_attributes else
                              (PolicySubject(carrier.id, {}),)) for resource in rule['requirements']}
        context = resolved_context(db, entry_id=entry_id, branch=selected, carrier=carrier, slots=slots,
            subjects=subjects, revision=project_collection_revision(db, principal, carrier))
        return create_context_batch(db, [context])
    if not rule['slots'] or any(contract['slots'][key]['kind'] != 'project' for key in rule['slots']):
        raise authz_error(503, 'AUTHZ_DOMAIN_RESOLVER_UNAVAILABLE', '领域对象解析器尚未注册')
    project_id = params.get('project_id')
    evidence = project_evidence(db, principal, [project_id])[project_id]
    slots = {key: (slot_ref(entry_id, key, project_id, selection={name: params[name]
        for name in contract['slots'][key]['selector_schema']['properties'] if name in params}),)
        for key in rule['slots']}
    carrier = slots[rule['carrier_slots'][0]][0]
    subjects = {}
    for resource in rule['requirements']:
        attributes = get_resource_definition(resource).query_attributes
        if attributes:
            subjects[resource] = (evidence.subject,)
        else:
            subjects[resource] = (PolicySubject(project_id, {}),)
    context = resolved_context(db, entry_id=entry_id, branch=selected, carrier=carrier,
                               slots=slots, subjects=subjects, revision=relation_revision({
                                   'project': evidence.authorization_revision, 'carrier': carrier.to_dict()}))
    return create_context_batch(db, [context])


@contextmanager
def read_context_snapshot(session_factory, identity, *, entry_id, params, branch=None):
    with read_snapshot(session_factory) as (db, proof):
        batch = resolve_entry_context(db, identity, entry_id=entry_id, params=params,
                                      branch=branch, read_snapshot=proof)
        if all(context.ref.kind == 'collection' for context in batch.contexts):
            principal = _named_principal(db, identity, read_snapshot=proof)
            bindings = {(context.ref.key, resource): query_bindings(db, principal, context, resource)
                for context in batch.contexts for resource in context.requirements
                }
            accesses = service.context_query_accesses(db, identity, batch, bindings, read_snapshot=proof)
        else:
            accesses = service.context_accesses(db, identity, batch, read_snapshot=proof)
        yield db, proof, batch, accesses


def lock_context_batch(db, identity, *, entry_id, params, lock_domain, branch=None, lock_users=None):
    """Final domain topology -> final user locks -> authorization, no commit."""
    domain_state = lock_domain(db, params)
    actor = lock_users(db, identity, domain_state) if lock_users else service.lock_actor(db, identity)
    if type(actor) is not SysUser:
        raise ValueError('用户锁计划必须返回已锁定的真实主体')
    batch = resolve_entry_context(db, identity, entry_id=entry_id, params=params,
                                  branch=branch, locked_user=actor)
    accesses = service.context_accesses(db, identity, batch, locked_user=actor)
    return LockedContextBatch(batch, accesses, actor, domain_state)


def lock_final_users(db, identity, *, update_user_ids=(), share_user_ids=()):
    """After all domain locks, acquire the final user set/strength once by ID.

    Relationship writers which mutate a target user must declare UPDATE here;
    they cannot first SHARE the actor and later upgrade it as a target.
    """
    if type(identity) is not VerifiedIdentity:
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '需要已验证的类型化身份')
    actor_id = db.scalar(select(SysUser.id).where(SysUser.username == identity.username))
    if actor_id is None:
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '实名账号已失效')
    updates, shares = set(update_user_ids), set(share_user_ids) | {actor_id}
    if any(type(value) is not int or value <= 0 for value in updates | shares):
        raise ValueError('最终用户锁集合需要稳定账号 ID')
    users = {}
    for user_id in sorted(updates | shares):
        user = db.scalar(select(SysUser).where(SysUser.id == user_id)
            .with_for_update(read=user_id not in updates).execution_options(populate_existing=True))
        if user is None:
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '目标账号已变化，请重新加载')
        users[user_id] = user
    actor = users[actor_id]
    load_principal(db, identity, locked_user=actor)
    return actor, MappingProxyType(users)


def document_access(db, identity, document_ref, *, context_batch, entry_id, format_purpose,
                    read_snapshot=None, locked_user=None):
    _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    resolver = _DOCUMENT_RESOLVERS.get(format_purpose)
    if resolver is None:
        raise authz_error(503, 'AUTHZ_DOMAIN_RESOLVER_UNAVAILABLE', '完整文档合同尚未注册')
    result = resolver(db, identity, document_ref, context_batch=context_batch, entry_id=entry_id,
                      format_purpose=format_purpose, read_snapshot=read_snapshot, locked_user=locked_user)
    if type(result) is not DocumentAccess or result.ref != document_ref:
        raise ValueError('文档解析器未保留实际载体')
    return result


def revalidate_dependency(db, identity, ref, *, entry_id, branch, actions_by_resource,
                          read_snapshot=None, locked_user=None):
    if type(ref) is not ObjectRef or ref.kind == 'operation_input':
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '临时输入不能恢复成历史访问许可')
    _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    resolver = _REFERENCE_RESOLVERS.get(entry_id)
    if resolver is not None:
        batch = resolver(db, identity, ref, entry_id=entry_id, branch=branch,
                         read_snapshot=read_snapshot, locked_user=locked_user)
    elif ref.kind == 'project':
        # Persisted selectors are parameters, never proof. Reconstruct the same
        # registered selection and resolve the real project again; dropping
        # dates/filters would falsely compare different content selections.
        selection = ref.selector.get('selection', {})
        if not isinstance(selection, Mapping) or 'project_id' in selection:
            raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史项目选择器无效')
        batch = resolve_entry_context(db, identity, entry_id=entry_id,
                                      params={**selection, 'project_id': ref.id}, branch=branch,
                                      read_snapshot=read_snapshot, locked_user=locked_user)
    elif ref.kind == 'collection' and entry_id not in _ENTRY_RESOLVERS:
        contract = ENTRY_CONTRACTS.get(entry_id, {})
        rule = contract.get('branches', {}).get(branch, {})
        if not rule.get('slots') or any(contract['slots'][key]['kind'] != 'collection' for key in rule['slots']):
            raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史集合不能替代对象或输入证据')
        selection = ref.selector.get('selection', {})
        if not isinstance(selection, Mapping):
            raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史集合选择器无效')
        batch = resolve_entry_context(db, identity, entry_id=entry_id, params=dict(selection), branch=branch,
                                      read_snapshot=read_snapshot, locked_user=locked_user)
    else:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史对象缺少完整的当前解析合同')
    context = next((c for c in batch.contexts if c.ref == ref), None)
    if context is None or any(not set(actions) <= set(context.requirements.get(resource, ()))
                              for resource, actions in actions_by_resource.items()):
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史访问来源与实际对象合同不匹配')
    if all(context.ref.kind == 'collection' for context in batch.contexts):
        principal = _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
        bindings = {(context.ref.key, resource): query_bindings(db, principal, context, resource)
                    for context in batch.contexts for resource in context.requirements}
        return service.context_query_accesses(db, identity, batch, bindings,
            read_snapshot=read_snapshot, locked_user=locked_user)
    return service.context_accesses(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)


def decode_precondition(value):
    """One JSON contract for body, multipart field, and GET query transport."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '授权前置条件格式无效') from None
    if (type(value) is not dict or set(value) != {'catalog_version', 'authorization_epoch', 'context'}
            or type(value['catalog_version']) is not str or not value['catalog_version']
            or type(value['authorization_epoch']) is not int or not 0 <= value['authorization_epoch'] < 2**53
            or type(value['context']) is not dict):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '授权前置条件格式无效')
    context = value['context']
    keys = {'kind', 'id', 'authorization_revision'} | ({'object_type'} if context.get('kind') == 'object' else set())
    if (set(context) != keys or context.get('kind') not in {'project', 'collection', 'object'}
            or any(type(context.get(key)) is not str or not context[key] for key in keys)):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '授权对象前置条件格式无效')
    if ((context['kind'] == 'object' and context['object_type'] not in set(CLOSED_KINDS) - {'project', 'collection'})
            or (context['kind'] == 'collection' and context['id'] not in COLLECTION_REFS)):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '授权对象类型或集合未登记')
    return value


def project_query(access, *, include_unassigned_when_all=False):
    """SQL set for project facts; only an unconditional PDP plan permits NULL.

    Existing all-project analysis includes unassigned source facts. A scope
    label alone never suppresses a conditional policy predicate.
    """
    from sqlalchemy import true
    if include_unassigned_when_all and access.scope == 'all_projects' and access.predicate.compare(true()):
        return None
    return select(MaintenanceProject.project_id).where(project_condition(access))


def project_read_capabilities(db, identity, project_ids, *, read_snapshot):
    """Batch narrow navigation checks without loading any project facts."""
    entry_id = 'GET /api/maintenance/projects/stable/{project_id}'
    principal, evidence = resolve_project_subjects(db, identity, sorted(set(project_ids)), read_snapshot=read_snapshot)
    contexts = []
    for project_id, item in evidence.items():
        ref = slot_ref(entry_id, 'context_0', project_id, selection={})
        contexts.append(resolved_context(db, entry_id=entry_id, branch='default', carrier=ref,
            slots={'context_0': (ref,)}, subjects={'maintenance_project': (item.subject,)},
            revision=relation_revision({'project': item.authorization_revision, 'carrier': ref.to_dict()})))
    if not contexts: return {}
    accesses = service.context_accesses(db, identity, create_context_batch(db, contexts), read_snapshot=read_snapshot)
    return {context.ref.id: accesses.get(context.ref, 'maintenance_project', 'read') is not None for context in contexts}
