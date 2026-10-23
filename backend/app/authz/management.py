"""Canonical authorization commands: one transaction for source, grants and epoch.

HTTP adapters do not write authorization rows. No policy decisions or cutover
activation happen here; catalog/principal/runtime remain the shared authority.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import re
import secrets

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.authz import catalog, principal, runtime
from app.authz.errors import authz_error
from app.models.authorization import SysAuthorizationGrant, SysCustomerAssignment
from app.models.dimensions import DimCustomer
from app.models.system import SysAuditLog, SysRoleTemplate, SysUser

MAX_INT = 2**53 - 1
ROLES = frozenset({'admin', 'boss', 'sales', 'purchaser', 'readonly', 'maintenance_manager'})
LEGACY_KEYS = frozenset({'permissions', 'overrides', 'keys', 'template_code', 'template_version', 'base_role'})


def envelope():
    return {'schema_version': 1, 'authorization_mode': runtime.authorization_mode(), 'catalog_version': catalog.CATALOG_VERSION}


def invalid(path, message, code='AUTHZ_INVALID_CONFIGURATION'):
    raise authz_error(422, code, message, field_errors=[{'path': path, 'code': code, 'message': message}])


def obj(value, allowed, required=(), path=''):
    if type(value) is not dict:
        invalid(path, '必须为对象')
    extra = value.keys() - set(allowed)
    if extra:
        key = sorted(extra)[0]
        code = ('LEGACY_AUTHORIZATION_PAYLOAD_UNSUPPORTED' if key in LEGACY_KEYS
                else 'ACCOUNT_SECURITY_FIELDS_FORBIDDEN' if key in {'role', 'salesperson_name'} else 'AUTHZ_INVALID_CONFIGURATION')
        invalid(f'{path}/{key}', '该命令不接受此字段', code)
    missing = set(required) - value.keys()
    if missing:
        invalid(f'{path}/{sorted(missing)[0]}', '缺少必填字段')
    return value


def integer(value, path, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INT:
        invalid(path, '必须为无损安全整数')
    return value


def string(value, path, *, nullable=False, maximum=256, minimum=0):
    if value is None and nullable:
        return None
    if type(value) is not str or not minimum <= len(value) <= maximum:
        invalid(path, '字符串类型或长度不合法')
    return value


def seq(value, path, *, nonempty=False):
    if type(value) is not list or (nonempty and not value):
        invalid(path, '必须为非空数组' if nonempty else '必须为数组')
    return value


def unique(values, path):
    if len(values) != len(set(values)):
        invalid(path, '条目不能重复')


def base(body, allowed, required=()):
    obj(body, {'schema_version', 'catalog_version', *allowed}, {'schema_version', 'catalog_version', *required})
    if integer(body['schema_version'], '/schema_version', 1) != 1:
        invalid('/schema_version', '不支持的协议版本')
    string(body['catalog_version'], '/catalog_version', minimum=1, maximum=256)
    if body['catalog_version'] != catalog.CATALOG_VERSION:
        raise authz_error(409, 'AUTHZ_CATALOG_CONFLICT', '授权目录已更新，请重新载入', conflicts=[{
            'kind': 'catalog', 'expected': body['catalog_version'], 'actual': catalog.CATALOG_VERSION}])
    if runtime.authorization_mode() != 'unified_modules':
        raise authz_error(409, 'AUTHZ_MODE_NOT_UNIFIED', '请在统一模块授权模式下操作')


def configuration(db, resource, value, *, states, path):
    string(resource, path + '/resource_id', minimum=1, maximum=64)
    runtime.require_configurable(db, resource, states=states)
    obj(value, {'actions', 'scope', 'field_permissions'}, {'actions', 'scope', 'field_permissions'}, path)
    try:
        catalog.validate_grant(resource, value['actions'], value['scope'], value['field_permissions'])
    except (ValueError, TypeError) as exc:
        invalid(path, str(exc))
    return {'actions': sorted(value['actions']), 'scope': value['scope'], 'field_permissions': sorted(value['field_permissions'])}


def denial(resource):
    return {'actions': [], 'scope': catalog.get_resource_definition(resource).denial_scope, 'field_permissions': []}


def empty_profile():
    return {'schema_version': 1, 'template_snapshot': None, 'resource_overrides': {}}


def profile(user):
    value = deepcopy(user.authorization_profile)
    if (type(value) is not dict or value.keys() != {'schema_version', 'template_snapshot', 'resource_overrides'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['resource_overrides']) is not dict):
        raise authz_error(409, 'AUTHZ_SOURCE_INVALID', '账号授权来源无效，不能继续写入')
    snap = value['template_snapshot']
    if snap is not None and (type(snap) is not dict or snap.keys() != {'code', 'version', 'modules'}
                             or type(snap['modules']) is not dict):
        raise authz_error(409, 'AUTHZ_SOURCE_INVALID', '账号模板快照无效')
    if snap is not None and (type(snap['code']) is not str or not snap['code']
            or type(snap['version']) is not int or not 1 <= snap['version'] <= MAX_INT):
        raise authz_error(409, 'AUTHZ_SOURCE_INVALID', '账号模板快照身份或版本无效')
    for resource, config in value['resource_overrides'].items():
        stored_configuration(resource, config)
    for resource, config in (snap['modules'].items() if snap else ()):
        stored_configuration(resource, config)
    return value


def stored_configuration(resource, value):
    """Validate persisted data before sorting/projecting; never diagnose with secrets."""
    try:
        if type(value) is not dict or value.keys() != {'actions', 'scope', 'field_permissions'}:
            raise ValueError('invalid stored shape')
        catalog.validate_grant(resource, value['actions'], value['scope'], value['field_permissions'])
    except (TypeError, ValueError, KeyError):
        raise authz_error(409, 'AUTHZ_SOURCE_INVALID', '保存的授权配置无效，需要诊断修复') from None
    return {'actions': sorted(value['actions']), 'scope': value['scope'], 'field_permissions': sorted(value['field_permissions'])}


def stored_bundle(value):
    if type(value) is not dict:
        raise authz_error(409, 'AUTHZ_SOURCE_INVALID', '保存的模板模块包无效')
    return {resource: stored_configuration(resource, config) for resource, config in value.items()}


def grants(db, users):
    return {(g.user_id, g.resource): g for g in db.scalars(select(SysAuthorizationGrant).where(
        SysAuthorizationGrant.user_id.in_(users)).execution_options(populate_existing=True))}


def grant_config(grant):
    if grant is None:
        return None
    if type(grant.version) is not int or not 1 <= grant.version <= MAX_INT:
        raise authz_error(409, 'AUTHZ_SOURCE_INVALID', '保存的授权版本无效')
    return stored_configuration(grant.resource, {'actions': grant.actions, 'scope': grant.scope, 'field_permissions': grant.field_permissions})


def effective(source, resource):
    if resource in source['resource_overrides']:
        return deepcopy(source['resource_overrides'][resource])
    snap = source['template_snapshot']
    return deepcopy(snap['modules'].get(resource)) if snap else None


def subject(user):
    return {k: getattr(user, k) for k in ('id', 'username', 'display_name', 'role', 'salesperson_name', 'is_active')}


def actor(db, identity, *, admin=True, locked=None, capability=None):
    who = principal.load_principal(db, identity, locked_user=locked)
    if admin:
        return principal.require_named_admin(who)
    if who is None or principal.actor_capabilities(who)[capability]['effect'] != 'allow':
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '当前账号没有此资料操作权限')
    return who


def target(db, username):
    user = db.scalar(select(SysUser).where(SysUser.username == username, SysUser.deleted_at.is_(None)))
    if user is None:
        raise authz_error(404, 'ACCOUNT_NOT_FOUND', '账号不存在')
    return user


def lock_users(db, identity, before_actor, targets, *, write=True, admin=True, capability=None):
    ids = {before_actor.id, *(u.id for u in targets)}
    stmt = select(SysUser).where(SysUser.id.in_(ids)).order_by(SysUser.id).with_for_update(read=not write)
    locked = {u.id: u for u in db.scalars(stmt.execution_options(populate_existing=True))}
    if before_actor.id not in locked:
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '操作者已失效')
    who = actor(db, identity, admin=admin, locked=locked[before_actor.id], capability=capability)
    for old in targets:
        if old.id not in locked or locked[old.id].deleted_at is not None:
            raise authz_error(404, 'ACCOUNT_NOT_FOUND', '账号不存在')
    return who, locked


def epoch(user, expected):
    integer(expected, '/expected_epoch')
    if user.token_version != expected:
        raise authz_error(409, 'AUTHZ_EPOCH_CONFLICT', '账号授权已变化，请重新载入', conflicts=[{
            'kind': 'epoch', 'user_id': user.id, 'expected': expected, 'actual': user.token_version}])


def version(resource, grant, expected, user_id):
    integer(expected, '/expected_version')
    actual = grant.version if grant else 0
    if expected != actual:
        raise authz_error(409, 'AUTHZ_RESOURCE_VERSION_CONFLICT', '模块授权已变化', conflicts=[{
            'kind': 'resource', 'user_id': user_id, 'resource_id': resource, 'expected': expected, 'actual': actual}])


def bump(user):
    if user.token_version >= MAX_INT:
        invalid('/expected_epoch', '账号版本已超过可用范围')
    user.token_version += 1


def audit(db, who, entity, entity_id, action, before, after):
    db.add(SysAuditLog(entity_type=entity, entity_id=entity_id, action=action,
                       before_json=deepcopy(before), after_json=deepcopy(after), operated_by=who.username,
                       reason='规范模块授权事务'))


def transaction(fn):
    @wraps(fn)
    def wrapped(db, *args, **kwargs):
        try:
            return fn(db, *args, **kwargs)
        except IntegrityError:
            db.rollback()
            raise authz_error(409, 'AUTHZ_WRITE_CONFLICT', '数据已变化或名称已被占用，请重新载入') from None
        except Exception:
            db.rollback()
            raise
    return wrapped


def _read_account(db, user, *, loaded_grants=None, templates=None):
    source = profile(user)
    loaded_grants = grants(db, [user.id]) if loaded_grants is None else loaded_grants
    snap = source['template_snapshot']
    resources_in_source = set(source['resource_overrides']) | (set(snap['modules']) if snap else set())
    resources_in_grants = {resource for uid, resource in loaded_grants if uid == user.id}
    for resource in resources_in_source | resources_in_grants:
        saved = effective(source, resource)
        actual = grant_config(loaded_grants.get((user.id, resource)))
        expected = stored_configuration(resource, saved) if saved is not None else denial(resource)
        if actual != expected:
            raise authz_error(409, 'AUTHZ_SOURCE_INCONSISTENT', '账号授权来源与物化授权不一致，需要诊断修复')
    tpl = (db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == snap['code'])) if templates is None else templates.get(snap['code'])) if snap else None
    rows = db.execute(select(DimCustomer.id, DimCustomer.name_raw, DimCustomer.name_normalized).join(
        SysCustomerAssignment, SysCustomerAssignment.customer_id == DimCustomer.id).where(
        SysCustomerAssignment.user_id == user.id).order_by(DimCustomer.id)).all()
    resources = {}
    for descriptor in catalog.resource_descriptors():
        resource = descriptor['id']; over = source['resource_overrides']; present = resource in over
        g = loaded_grants.get((user.id, resource))
        resources[resource] = {'version': g.version if g else 0, 'configuration': grant_config(g),
            'source': 'personal_override' if present else 'template_snapshot' if snap and resource in snap['modules'] else 'none',
            'template_snapshot': None if snap is None else {'code': snap['code'], 'version': snap['version'], 'configuration': deepcopy(snap['modules'].get(resource))},
            'personal_override': {'present': present, 'configuration': deepcopy(over[resource]) if present else None}}
    return {**envelope(), 'subject': subject(user), 'authorization_epoch': user.token_version,
        'template': None if snap is None else {'code': snap['code'], 'version': snap['version'], 'current_version': tpl.version if tpl else None, 'stale': bool(tpl and tpl.version != snap['version'])},
        'customer_assignments': {'ids': [r.id for r in rows], 'customers': [{'id': r.id, 'name': r.name_normalized or r.name_raw, 'raw_name': r.name_raw} for r in rows]}, 'resources': resources}


def read_account(db, identity, username):
    who = actor(db, identity); user = target(db, username)
    _, locked = lock_users(db, identity, who, [user], write=False)
    return _read_account(db, locked[user.id])


def customer_ids(db, body):
    obj(body, {'ids'}, {'ids'}, '/customer_assignments')
    ids = [integer(v, '/customer_assignments/ids', 1) for v in seq(body['ids'], '/customer_assignments/ids')]
    unique(ids, '/customer_assignments/ids')
    if ids:
        found = set(db.scalars(select(DimCustomer.id).where(DimCustomer.id.in_(ids)).order_by(DimCustomer.id).with_for_update(read=True, key_share=True)))
        if found != set(ids):
            raise authz_error(404, 'CUSTOMER_NOT_FOUND', '所选客户不存在')
    return sorted(ids)


def set_customers(db, user, ids):
    db.execute(delete(SysCustomerAssignment).where(SysCustomerAssignment.user_id == user.id))
    db.add_all(SysCustomerAssignment(user_id=user.id, customer_id=value) for value in ids)


def put_grant(db, user, resource, config, grant):
    if grant is None:
        grant = SysAuthorizationGrant(user_id=user.id, resource=resource, version=1)
        db.add(grant)
    else:
        if grant.version >= MAX_INT:
            invalid('/expected_version', '模块版本已超过可用范围')
        grant.version += 1
    grant.actions = config['actions']; grant.scope = config['scope']; grant.field_permissions = config['field_permissions']
    return grant


@transaction
def replace_modules(db, identity, username, body):
    who = actor(db, identity)
    base(body, {'expected_epoch', 'updates', 'customer_assignments'}, {'expected_epoch', 'updates'})
    updates = seq(body['updates'], '/updates', nonempty=True)
    user = target(db, username); who, locked = lock_users(db, identity, who, [user]); user = locked[user.id]
    epoch(user, body['expected_epoch']); states = runtime.runtime_states(db); source = profile(user); gs = grants(db, [user.id])
    before = _read_account(db, user, loaded_grants=gs); plans = []; seen = []
    for i, update in enumerate(updates):
        path = f'/updates/{i}'
        obj(update, {'resource_id', 'expected_version', 'operation', 'configuration'}, {'resource_id', 'expected_version', 'operation'}, path)
        resource = string(update['resource_id'], path+'/resource_id', minimum=1, maximum=64); seen.append(resource)
        runtime.require_configurable(db, resource, states=states)
        grant = gs.get((user.id, resource)); version(resource, grant, update['expected_version'], user.id)
        op = string(update['operation'], path+'/operation', minimum=1)
        if op == 'clear_override':
            if 'configuration' in update:invalid(path+'/configuration', '清除覆盖不能携带配置')
            source['resource_overrides'].pop(resource, None)
            config = effective(source, resource) or denial(resource)
            config = configuration(db, resource, config, states=states, path=path)
        elif op in {'replace_resource', 'revoke_resource'}:
            config = configuration(db, resource, update.get('configuration'), states=states, path=path+'/configuration')
            if op == 'revoke_resource' and config['actions']:invalid(path+'/configuration/actions', '撤销必须为空动作')
            source['resource_overrides'][resource] = config
        else:invalid(path+'/operation', '未知模块操作')
        plans.append((resource, config, grant))
    unique(seen, '/updates')
    ids = None
    if 'customer_assignments' in body:
        if catalog.SALES_RESOURCE not in seen:invalid('/updates', '客户归属必须与完整销售模块配置同时保存')
        ids = customer_ids(db, body['customer_assignments'])
    for resource, config, grant in plans:put_grant(db, user, resource, config, grant)
    user.authorization_profile = source
    if ids is not None:set_customers(db, user, ids)
    bump(user); db.flush(); result = _read_account(db, user)
    audit(db, who, 'sys_user', user.id, 'authorization_modules_replace', before, result)
    db.commit()
    return {**envelope(), 'committed': True, 'actor_session_revoked': who.id == user.id, 'result': result}


def template_row(db, code, *, lock=False, usable=False):
    string(code, '/template_code', minimum=1, maximum=64)
    stmt = select(SysRoleTemplate).where(SysRoleTemplate.code == code)
    if lock:stmt = stmt.with_for_update()
    tpl = db.scalar(stmt.execution_options(populate_existing=True))
    if tpl is None:raise authz_error(404, 'TEMPLATE_NOT_FOUND', '模板不存在')
    if usable and (tpl.code == 'admin' or not tpl.is_active):
        raise authz_error(400, 'ACCOUNT_PROTECTION_VIOLATION', '该模板已锁定或停用，不能使用')
    return tpl


def template_version(tpl, expected):
    integer(expected, '/expected_version', 1)
    if tpl.version != expected:
        raise authz_error(409, 'TEMPLATE_VERSION_CONFLICT', '模板已变化', conflicts=[{
            'kind': 'template', 'expected': expected, 'actual': tpl.version}])


def template_view(tpl):
    return {**envelope(), 'template': {**{k: getattr(tpl,k) for k in ('code','name','description','base_role','is_system','is_active','version')}, 'locked': tpl.code == 'admin'}, 'modules': stored_bundle(tpl.authorization_bundle)}


def read_template(db, identity, code):
    actor(db, identity)
    return template_view(template_row(db, code))


def module_bundle(db, values, states, path='/authorization/modules'):
    obj(values, values.keys() if type(values) is dict else (), path=path)
    return {r: configuration(db,r,v,states=states,path=path+'/'+r) for r,v in values.items()}


def template_metadata(value, *, create=False):
    allowed={'name','description'} | ({'code'} if create else set())
    obj(value, allowed, {'name'} if create else (), '/metadata')
    result={}
    for key,v in value.items():
        result[key]=string(v,'/metadata/'+key,nullable=key=='description',minimum=1 if key in {'name','code'} else 0,maximum=4000 if key=='description' else 64)
    if 'code' in result and not re.fullmatch(r'[a-z0-9_-]{2,64}',result['code']):invalid('/metadata/code','模板代码格式非法')
    return result


@transaction
def save_template(db, identity, body, *, code=None, state_action=None):
    who=actor(db,identity)
    if code is None:
        base(body,{'metadata','authorization'},{'metadata','authorization'})
        metadata=template_metadata(body['metadata'],create=True); spec=obj(body['authorization'],{'kind','modules','code','expected_version'},{'kind'},'/authorization')
        src=None
        if spec['kind']=='copy_template':
            obj(spec,{'kind','code','expected_version'},{'kind','code','expected_version'},'/authorization')
            src=template_row(db,spec['code'],lock=True,usable=True);template_version(src,spec['expected_version'])
        elif spec['kind']=='explicit':obj(spec,{'kind','modules'},{'kind','modules'},'/authorization')
        else:invalid('/authorization/kind','未知模板创建方式')
        who,_=lock_users(db,identity,who,[])
        states=runtime.runtime_states(db)
        bundle=module_bundle(db,src.authorization_bundle if src else spec['modules'],states)
        newcode=metadata.pop('code',None) or 'tpl-'+secrets.token_hex(4)
        if newcode=='admin':raise authz_error(400,'ACCOUNT_PROTECTION_VIOLATION','admin 模板保留')
        tpl=SysRoleTemplate(code=newcode,base_role='readonly',permissions={},authorization_bundle=bundle,authorization_legacy_seed_eligible=False,version=1,is_active=True,is_system=False,created_by=who.username,**metadata)
        db.add(tpl);db.flush();before=None
    else:
        base(body,{'expected_version'} if state_action else {'expected_version','metadata','updates'},{'expected_version'})
        tpl=template_row(db,code,lock=True);template_version(tpl,body['expected_version'])
        if tpl.code=='admin' or (state_action=='archive' and tpl.is_system):raise authz_error(400,'ACCOUNT_PROTECTION_VIOLATION','系统模板不可执行该操作')
        who,_=lock_users(db,identity,who,[]);before=template_view(tpl)
        if state_action:
            desired=state_action=='restore'
            if tpl.is_active==desired:
                db.rollback();return {**envelope(),'committed':True,'actor_session_revoked':False,'changed':False,'result':before}
            tpl.is_active=desired
        else:
            metadata=template_metadata(body['metadata']) if 'metadata' in body else {}
            updates=seq(body['updates'],'/updates',nonempty=True) if 'updates' in body else []
            if not metadata and not updates:invalid('/','模板保存必须包含元数据或模块更新')
            bundle=deepcopy(tpl.authorization_bundle);states=runtime.runtime_states(db);seen=[]
            for i,u in enumerate(updates):
                path=f'/updates/{i}';obj(u,{'resource_id','operation','configuration'},{'resource_id','operation'},path)
                r=string(u['resource_id'],path+'/resource_id',minimum=1,maximum=64);seen.append(r);runtime.require_configurable(db,r,states=states)
                if u['operation']=='remove_resource':
                    if 'configuration' in u:invalid(path+'/configuration','移除资源不能携带配置')
                    bundle.pop(r,None)
                elif u['operation']=='replace_resource':bundle[r]=configuration(db,r,u.get('configuration'),states=states,path=path+'/configuration')
                else:invalid(path+'/operation','未知模板模块操作')
            unique(seen,'/updates')
            for k,v in metadata.items():setattr(tpl,k,v)
            tpl.authorization_bundle=bundle
        integer(tpl.version+1,'/expected_version',1);tpl.version+=1
    tpl.updated_by=who.username;tpl.updated_at=datetime.now(timezone.utc);db.flush();result=template_view(tpl)
    audit(db,who,'sys_role_template',tpl.id,'authorization_template_'+(state_action or 'save'),before,result);db.commit()
    return {**envelope(),'committed':True,'actor_session_revoked':False,**({'changed':True} if state_action else {}),'result':result}


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def preview_mode(body):
    if type(body.get('dry_run')) is not bool:invalid('/dry_run','必须明确预览或执行')
    if body['dry_run'] and 'fingerprint' in body:invalid('/fingerprint','预览不接受旧指纹')
    if not body['dry_run']:string(body.get('fingerprint'),'/fingerprint',minimum=64,maximum=64)
    return body['dry_run']


def check_fingerprint(actual, supplied):
    if not secrets.compare_digest(actual,supplied):
        raise authz_error(409,'AUTHZ_PREVIEW_CONFLICT','预览后数据或操作已变化，请重新预览')


def template_ref(source):
    snap=source['template_snapshot']
    return {'code':snap['code'],'version':snap['version']} if snap else None


def override(source,r):
    over=source['resource_overrides']
    return {'present':r in over,'configuration':deepcopy(over.get(r))}


def resource_diff(r,g,before,after,old_source,new_source):
    old=before or {'actions':[],'scope':'none','field_permissions':[]}
    was=override(old_source,r);now=override(new_source,r)
    change=('revoke' if not after['actions'] and old['actions'] else
            'clear_override' if was['present'] and not now['present'] else
            'unchanged' if before==after and was==now else
            'inherit' if not now['present'] and effective(new_source,r) is not None else 'replace')
    return {'resource_id':r,'version_before':g.version if g else 0,'version_after':g.version+1 if g else 1,
        'configuration_before':before,'configuration_after':after,'override_before':was,'override_after':now,'change':change,
        'actions_added':sorted(set(after['actions'])-set(old['actions'])),'actions_removed':sorted(set(old['actions'])-set(after['actions'])),
        'scope_before':old['scope'],'scope_after':after['scope'],'fields_added':sorted(set(after['field_permissions'])-set(old['field_permissions'])),
        'fields_removed':sorted(set(old['field_permissions'])-set(after['field_permissions']))}


@transaction
def bulk_modules(db,identity,body,*,sync_code=None):
    who=actor(db,identity)
    base(body,{'targets','operation','dry_run','fingerprint'},{'targets','operation','dry_run'})
    dry=preview_mode(body);targets=seq(body['targets'],'/targets',nonempty=True)
    spec=obj(body['operation'],{'kind','template_code','expected_template_version','override_policy','updates','resource_ids'},{'kind'},'/operation')
    kind=string(spec['kind'],'/operation/kind',minimum=1);tpl=None;normalized=deepcopy(spec)
    if kind in {'apply_template','sync_template'}:
        obj(spec,{'kind','template_code','expected_template_version','override_policy'},{'kind','template_code','expected_template_version','override_policy'},'/operation')
        if type(spec['override_policy']) is not str or spec['override_policy'] not in {'preserve','clear'}:invalid('/operation/override_policy','覆盖策略无效')
        if (sync_code is not None and (kind!='sync_template' or spec['template_code']!=sync_code)) or (sync_code is None and kind=='sync_template'):
            invalid('/operation','同步命令与模板路径不匹配')
        tpl=template_row(db,spec['template_code'],lock=True,usable=True);template_version(tpl,spec['expected_template_version'])
    elif kind=='replace_resources':
        obj(spec,{'kind','updates'},{'kind','updates'},'/operation');seq(spec['updates'],'/operation/updates',nonempty=True)
    elif kind in {'revoke_resources','clear_overrides'}:
        obj(spec,{'kind','resource_ids'},{'kind','resource_ids'},'/operation')
        seq(spec['resource_ids'],'/operation/resource_ids',nonempty=True)
    else:invalid('/operation/kind','未知批量操作')
    if sync_code is not None and kind!='sync_template':invalid('/operation/kind','该路径仅支持模板同步')
    users=[];target_ids=[]
    for i,t in enumerate(targets):
        path=f'/targets/{i}';obj(t,{'user_id','username','expected_epoch','expected_resource_versions'},{'user_id','username','expected_epoch'},path)
        uid=integer(t['user_id'],path+'/user_id',1);name=string(t['username'],path+'/username',minimum=1,maximum=64);integer(t['expected_epoch'],path+'/expected_epoch')
        user=target(db,name)
        if user.id!=uid:raise authz_error(409,'AUTHZ_PREVIEW_CONFLICT','目标主键与用户名不匹配')
        target_ids.append(uid);users.append(user)
    unique(target_ids,'/targets');who,locked=lock_users(db,identity,who,users)
    users=[locked[uid] for uid in target_ids];states=runtime.runtime_states(db);gs=grants(db,target_ids)
    configs={};selected=[]
    if kind=='replace_resources':
        for i,u in enumerate(spec['updates']):
            path=f'/operation/updates/{i}';obj(u,{'resource_id','configuration'},{'resource_id','configuration'},path)
            r=string(u['resource_id'],path+'/resource_id',minimum=1,maximum=64);selected.append(r)
            configs[r]=configuration(db,r,u['configuration'],states=states,path=path+'/configuration')
        normalized['updates']=[{'resource_id':r,'configuration':configs[r]} for r in sorted(configs)]
    elif kind in {'revoke_resources','clear_overrides'}:
        selected=[string(r,'/operation/resource_ids',minimum=1,maximum=64) for r in spec['resource_ids']]
        for r in selected:runtime.require_configurable(db,r,states=states)
        normalized['resource_ids']=sorted(selected)
    unique(selected,'/operation')
    bundle=module_bundle(db,tpl.authorization_bundle,states) if tpl else None
    previews=[];plans=[];normalized_targets=[];source_states=[]
    for user,t in zip(users,targets):
        if user.role=='admin' or user.username=='admin':raise authz_error(400,'ACCOUNT_PROTECTION_VIOLATION','管理员账号不参与批量或模板应用')
        epoch(user,t['expected_epoch']);old=profile(user);new=deepcopy(old)
        if kind=='sync_template' and (old['template_snapshot'] is None or old['template_snapshot']['code']!=sync_code):
            raise authz_error(409,'AUTHZ_PREVIEW_CONFLICT','目标不再属于该规范模板')
        if tpl:
            oldmods=old['template_snapshot']['modules'] if old['template_snapshot'] else {}
            affected=set(oldmods)|set(bundle)|set(old['resource_overrides'])
            new['template_snapshot']={'code':tpl.code,'version':tpl.version,'modules':deepcopy(bundle)}
            if spec['override_policy']=='clear':new['resource_overrides']={}
        else:
            affected=set(selected)
            for r in selected:
                if kind=='clear_overrides':new['resource_overrides'].pop(r,None)
                else:new['resource_overrides'][r]=configs[r] if kind=='replace_resources' else denial(r)
        expected_versions={r:gs[(user.id,r)].version if (user.id,r) in gs else 0 for r in sorted(affected)}
        if not dry and 'expected_resource_versions' not in t:invalid('/targets/expected_resource_versions','执行必须回传完整预览资源版本')
        if 'expected_resource_versions' in t:
            supplied=t['expected_resource_versions'];obj(supplied,expected_versions,expected_versions,'/targets/expected_resource_versions')
            for r,v in supplied.items():version(r,gs.get((user.id,r)),v,user.id)
        changes=[];pending=[]
        for r in sorted(affected):
            runtime.require_configurable(db,r,states=states)
            config=configuration(db,r,effective(new,r) or denial(r),states=states,path='/operation/'+r)
            g=gs.get((user.id,r));before=grant_config(g);changes.append(resource_diff(r,g,before,config,old,new));pending.append((r,config,g))
        previews.append({'subject':subject(user),'epoch_before':user.token_version,'epoch_after':user.token_version+1,
            'template_before':template_ref(old),'template_after':template_ref(new),
            'effective_changed':any(c['configuration_before']!=c['configuration_after'] for c in changes),'source_changed':old!=new,
            'session_revoked':True,'resources':changes})
        normalized_targets.append({'user_id':user.id,'username':user.username,'expected_epoch':user.token_version,'expected_resource_versions':expected_versions})
        source_states.append({'user_id':user.id,'before':old,'after':new})
        plans.append((user,new,pending))
    command={'schema_version':1,'catalog_version':catalog.CATALOG_VERSION,'targets':normalized_targets,'operation':normalized}
    fp=fingerprint({'actor_id':who.id,'actor_epoch':who.authorization_epoch,'command':command,'preview':previews,'sources':source_states})
    if dry:
        db.rollback();return {**envelope(),'dry_run':True,'committed':False,'fingerprint':fp,'command':command,'affected':len(plans),'preview':previews}
    check_fingerprint(fp,body['fingerprint'])
    for user,source,pending in plans:
        before=_read_account(db,user,loaded_grants=gs)
        for r,c,g in pending:put_grant(db,user,r,c,g)
        user.authorization_profile=source;bump(user);db.flush()
        audit(db,who,'sys_user',user.id,'authorization_'+kind,before,_read_account(db,user))
    results=[_read_account(db,u) for u,_,_ in plans];db.commit()
    return {**envelope(),'dry_run':False,'committed':True,'actor_session_revoked':who.id in target_ids,'applied':len(results),'results':results}


def roster_lock(db):
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended('accounts:active-admin-roster',0))))


def protect_user(db,who,user,*,password=False,demote=False,disable=False,delete_user=False):
    if user.username=='admin' and not password:raise authz_error(400,'ACCOUNT_PROTECTION_VIOLATION','内置 admin 除重置密码外不可修改')
    if user.role=='admin' and who.role!='admin':raise authz_error(403,'AUTHZ_FORBIDDEN','仅管理员可操作管理员账号')
    if user.id==who.id and (demote or disable or delete_user):raise authz_error(400,'ACCOUNT_PROTECTION_VIOLATION','不能停用、删除或降级自己')
    if user.role=='admin' and user.is_active and (demote or disable or delete_user):
        count=db.scalar(select(func.count()).select_from(SysUser).where(SysUser.role=='admin',SysUser.is_active.is_(True),SysUser.deleted_at.is_(None)))
        if count<=1:raise authz_error(400,'ACCOUNT_PROTECTION_VIOLATION','不能移除最后一个有效管理员')


def security_spec(value):
    obj(value,{'role','salesperson_name'},path='/security_change')
    if not value:invalid('/security_change','安全属性变更不能为空')
    if 'role' in value and (type(value['role']) is not str or value['role'] not in ROLES):invalid('/security_change/role','角色非法')
    if 'salesperson_name' in value:string(value['salesperson_name'],'/security_change/salesperson_name',nullable=True,maximum=64)
    return deepcopy(value)


@transaction
def create_account(db,identity,body):
    who=actor(db,identity)
    base(body,{'profile','authorization','customer_assignments','security_change','dry_run','fingerprint'},{'profile','authorization'})
    secured='security_change' in body;dry=preview_mode(body) if secured else False
    if not secured and ('dry_run' in body or 'fingerprint' in body):invalid('/dry_run','普通建号直接原子提交')
    pr=obj(body['profile'],{'username','password','display_name'},{'username'} if dry else {'username','password'},'/profile')
    username=string(pr['username'],'/profile/username',minimum=1,maximum=64).strip()
    if not username:invalid('/profile/username','用户名不能为空')
    if dry and 'password' in pr:invalid('/profile/password','预览不能发送密码')
    if not dry:string(pr['password'],'/profile/password',minimum=6,maximum=4096)
    display=string(pr.get('display_name'),'/profile/display_name',nullable=True,maximum=64)
    security=security_spec(body['security_change']) if secured else {}
    if 'role' in security and security['role'] != 'admin':invalid('/security_change/role','新账号只能默认 readonly 或显式创建 admin')
    spec=obj(body['authorization'],{'kind','modules','template_code','expected_template_version'},{'kind'},'/authorization')
    kind=string(spec['kind'],'/authorization/kind',minimum=1);tpl=None
    if secured:roster_lock(db)
    if kind=='apply_template':
        obj(spec,{'kind','template_code','expected_template_version'},{'kind','template_code','expected_template_version'},'/authorization')
        tpl=template_row(db,spec['template_code'],lock=True,usable=True);template_version(tpl,spec['expected_template_version'])
    elif kind=='explicit':obj(spec,{'kind','modules'},{'kind','modules'},'/authorization')
    elif kind=='none':obj(spec,{'kind'},{'kind'},'/authorization')
    else:invalid('/authorization/kind','未知建号授权方式')
    who,_=lock_users(db,identity,who,[])
    if db.scalar(select(SysUser.id).where(SysUser.username==username)) is not None:raise authz_error(409,'ACCOUNT_ALREADY_EXISTS','用户名已存在')
    states=runtime.runtime_states(db)
    bundle=module_bundle(db,tpl.authorization_bundle if tpl else spec.get('modules',{}),states)
    source=empty_profile()
    if tpl:source['template_snapshot']={'code':tpl.code,'version':tpl.version,'modules':bundle}
    elif kind=='explicit':source['resource_overrides']=bundle
    ids=[]
    if 'customer_assignments' in body:
        if kind=='none' or catalog.SALES_RESOURCE not in bundle:invalid('/customer_assignments','客户归属需要同时提供销售模块授权')
        ids=customer_ids(db,body['customer_assignments'])
    epoch_after=int(kind!='none' or secured)
    command={k:deepcopy(v) for k,v in body.items() if k not in {'dry_run','fingerprint'}}
    command['profile'].pop('password',None)
    fp=fingerprint({'actor_id':who.id,'actor_epoch':who.authorization_epoch,'command':command,'source':source,'username_absent':True})
    if dry:
        preview={'username':username,'username_must_be_absent':True,'role_after':security.get('role','readonly'),'authorization_kind':kind,'authorization_epoch_after':epoch_after}
        db.rollback();return {**envelope(),'dry_run':True,'committed':False,'fingerprint':fp,'command':command,'preview':preview}
    if secured:check_fingerprint(fp,body['fingerprint'])
    user=SysUser(username=username,password_hash=hash_password(pr['password']),role=security.get('role','readonly'),display_name=display,
        salesperson_name=security.get('salesperson_name'),authorization_profile=source,authorization_legacy_seed_eligible=False,token_version=epoch_after,is_active=True)
    db.add(user);db.flush()
    for r,c in bundle.items():put_grant(db,user,r,c,None)
    set_customers(db,user,ids);db.flush();result=_read_account(db,user)
    audit(db,who,'sys_user',user.id,'authorization_account_create',None,result);db.commit()
    return {**envelope(),'committed':True,'actor_session_revoked':False,'account':result}


@transaction
def update_account(db,identity,username,body):
    secured='security_change' in body
    who=actor(db,identity,admin=secured,capability='edit_account_profiles')
    if secured:
        base(body,{'expected_epoch','security_change','dry_run','fingerprint'},{'expected_epoch','security_change','dry_run'})
        spec=security_spec(body['security_change']);dry=preview_mode(body);roster_lock(db)
    else:
        base(body,{'profile'},{'profile'});spec=obj(body['profile'],{'display_name'},{'display_name'},'/profile')
        string(spec['display_name'],'/profile/display_name',nullable=True,maximum=64)
    user=target(db,username);who,locked=lock_users(db,identity,who,[user],admin=secured,capability='edit_account_profiles');user=locked[user.id]
    protect_user(db,who,user,demote=secured and user.role=='admin' and spec.get('role',user.role)!='admin')
    before=subject(user)
    if not secured:
        user.display_name=spec['display_name'];db.flush();result=subject(user)
        audit(db,who,'sys_user',user.id,'account_profile_update',before,result);epoch_value=user.token_version;db.commit()
        return {**envelope(),'committed':True,'actor_session_revoked':False,'subject':result,'authorization_epoch':epoch_value}
    epoch(user,body['expected_epoch']);old={k:getattr(user,k) for k in ('role','salesperson_name')};new={**old,**spec}
    preview={'subject':before,'epoch_before':user.token_version,'epoch_after':user.token_version+1,'security_before':old,'security_after':new,
             'legacy_role_behavior_may_change':old['role']!=new['role'],'session_revoked':True,'canonical_configuration_unchanged':True}
    command={k:deepcopy(v) for k,v in body.items() if k not in {'dry_run','fingerprint'}}
    fp=fingerprint({'actor_id':who.id,'actor_epoch':who.authorization_epoch,'target_id':user.id,'command':command,'preview':preview})
    if dry:
        db.rollback();return {**envelope(),'dry_run':True,'committed':False,'fingerprint':fp,'command':command,'preview':preview}
    check_fingerprint(fp,body['fingerprint'])
    for k,v in spec.items():setattr(user,k,v)
    bump(user);db.flush();result=subject(user);new_epoch=user.token_version
    audit(db,who,'sys_user',user.id,'account_security_change',before,result);db.commit()
    return {**envelope(),'committed':True,'actor_session_revoked':who.id==user.id,'subject':result,'authorization_epoch':new_epoch}


@transaction
def account_lifecycle(db,identity,username,body,*,operation):
    who=actor(db,identity)
    extra={'password'} if operation=='password' else {'is_active'} if operation=='active' else set()
    base(body,{'expected_epoch',*extra},{'expected_epoch',*extra})
    if operation=='password':string(body['password'],'/password',minimum=6,maximum=4096)
    if operation=='active' and type(body['is_active']) is not bool:invalid('/is_active','必须为布尔值')
    if operation!='password':roster_lock(db)
    user=target(db,username);who,locked=lock_users(db,identity,who,[user]);user=locked[user.id];epoch(user,body['expected_epoch'])
    protect_user(db,who,user,password=operation=='password',disable=operation=='active' and not body['is_active'],delete_user=operation=='delete')
    before=subject(user);changed=operation!='active' or user.is_active!=body['is_active']
    if operation=='password':
        user.password_hash=hash_password(body['password']);user.failed_attempts=0;user.locked_until=None
    elif operation=='active':user.is_active=body['is_active']
    else:user.is_active=False;user.deleted_at=datetime.now(timezone.utc)
    if changed:bump(user)
    db.flush();after=subject(user);new_epoch=user.token_version
    if changed:audit(db,who,'sys_user',user.id,'account_'+operation,before,after)
    result={**envelope(),'committed':True,'actor_session_revoked':changed and who.id==user.id,'subject':{'id':user.id,'username':user.username},'authorization_epoch':new_epoch}
    if operation=='password':result['reset']=True
    elif operation=='active':result.update(is_active=user.is_active,changed=changed)
    else:result['deleted']=True
    db.commit();return result


def account_summaries(users,templates):
    result={}
    for user in users:
        source=profile(user);snap=source['template_snapshot'];tpl=templates.get(snap['code']) if snap else None
        result[user.id]={'template_code':snap['code'] if snap else None,'template_version':snap['version'] if snap else None,
            'template_current_version':tpl.version if tpl else None,'override_resources':sorted(source['resource_overrides'])}
    return result


def template_summaries(db,templates):
    counts={t.code:0 for t in templates}
    for value in db.scalars(select(SysUser.authorization_profile).where(SysUser.deleted_at.is_(None))):
        snap=value.get('template_snapshot') if isinstance(value,dict) else None
        if snap and snap['code'] in counts:counts[snap['code']]+=1
    return {t.code:{'configured_resources':sorted(stored_bundle(t.authorization_bundle)),'canonical_usage_count':counts[t.code]} for t in templates}


def template_accounts(db,identity,code):
    actor(db,identity);tpl=template_row(db,code)
    users=list(db.scalars(select(SysUser).where(SysUser.deleted_at.is_(None),SysUser.authorization_profile['template_snapshot']['code'].astext==code).order_by(SysUser.id)))
    rows=[]
    for user in users:
        source=profile(user);snap=source['template_snapshot']
        rows.append({'id':user.id,'username':user.username,'display_name':user.display_name,'is_active':user.is_active,
            'authorization_epoch':user.token_version,'template_version':snap['version'],'stale':snap['version']!=tpl.version,
            'override_resources':sorted(source['resource_overrides'])})
    return {**envelope(),'code':code,'template_version':tpl.version,'accounts':rows,'total':len(rows)}
