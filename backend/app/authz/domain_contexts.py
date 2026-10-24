"""Typed cross-domain context dispatch; domain evidence is explicitly registered."""
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import json
import math
import re
from types import MappingProxyType
from uuid import UUID
from sqlalchemy import literal, select
from app.authz import service
from app.authz.catalog import get_resource_definition
from app.authz.context import (ContextBatch, ObjectRef, ReadSnapshotProof, ResolvedContext, canonical_json, read_snapshot)
from app.authz.context_registry import CLOSED_KINDS, COLLECTION_REFS, ENTRY_CONTRACTS
from app.authz.engine import QueryBindings
from app.authz.errors import authz_error
from app.authz.principal import Principal, VerifiedIdentity, load_principal
from app.models.system import SysUser

_ENTRY_RESOLVERS: dict[str, Callable] = {}
_REFERENCE_RESOLVERS: dict[str, Callable] = {}
_DOCUMENT_RESOLVERS: dict[str, Callable] = {}
_QUERY_BINDINGS: dict[str, Callable] = {}


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


def register_query_bindings(resource, factory):
    if get_resource_definition(resource) is None or not callable(factory):
        raise ValueError('未登记的查询族')
    if resource in _QUERY_BINDINGS and _QUERY_BINDINGS[resource] is not factory:
        raise ValueError('查询族已有唯一绑定')
    _QUERY_BINDINGS[resource] = factory


def query_bindings(db, principal, context, resource):
    if resource in _QUERY_BINDINGS:
        return _QUERY_BINDINGS[resource](db, principal, context, resource)
    definition = get_resource_definition(resource)
    if definition is not None and not definition.query_attributes:
        return QueryBindings(resource, select(literal(True).label('present')).subquery(), {})
    raise authz_error(503, 'AUTHZ_DOMAIN_RESOLVER_UNAVAILABLE', '领域查询族尚未注册')


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


def register_reference_resolver(entry_id, resolver):
    if entry_id not in ENTRY_CONTRACTS or not callable(resolver):
        raise ValueError('未登记的历史来源解析器')
    if entry_id in _REFERENCE_RESOLVERS and _REFERENCE_RESOLVERS[entry_id] is not resolver:
        raise ValueError('历史来源已有唯一解析器')
    _REFERENCE_RESOLVERS[entry_id] = resolver


def register_entry_resolver(entry_id, resolver, *, reference_resolver=None):
    """Validate the whole registration before mutating the single registry."""
    if entry_id not in ENTRY_CONTRACTS or not callable(resolver):
        raise ValueError('未登记的领域入口')
    if entry_id in _ENTRY_RESOLVERS and _ENTRY_RESOLVERS[entry_id] is not resolver:
        raise ValueError('领域入口已有唯一解析器')
    if reference_resolver is not None:
        if not callable(reference_resolver):
            raise ValueError('历史来源解析器无效')
        if entry_id in _REFERENCE_RESOLVERS and _REFERENCE_RESOLVERS[entry_id] is not reference_resolver:
            raise ValueError('历史来源已有唯一解析器')
    _ENTRY_RESOLVERS[entry_id] = resolver
    if reference_resolver is not None:
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
    _named_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    resolver = _ENTRY_RESOLVERS.get(entry_id)
    if resolver is None:
        raise authz_error(503, 'AUTHZ_DOMAIN_RESOLVER_UNAVAILABLE', '领域对象解析器尚未注册')
    batch = resolver(db, identity, entry_id=entry_id, params=params, branch=branch,
                     read_snapshot=read_snapshot, locked_user=locked_user)
    return _checked_dispatch(db, batch, entry_id=entry_id, branch=branch)


def _checked_dispatch(db, batch, *, entry_id, branch):
    """An adapter may choose a branch, but cannot substitute another contract."""
    if type(batch) is not ContextBatch:
        raise ValueError('领域解析器必须返回核心 ContextBatch')
    batch.validate_observation(db)
    if any(context.entry_id != entry_id or (branch is not None and context.branch != branch)
           for context in batch.contexts):
        raise ValueError('领域解析器返回了其他入口或分支的上下文')
    return batch


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
    else:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '历史对象缺少完整的当前解析合同')
    batch = _checked_dispatch(db, batch, entry_id=entry_id, branch=branch)
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
