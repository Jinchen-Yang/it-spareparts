"""Closed platform entry contracts, separate from employee grants.

These descriptors are inventory, never an authorization decision. Each adapter
must validate its actual credential or live administrator independently.
"""
from copy import deepcopy


def _boundary(identifier, label, principal_kind, authority, actions, scope, notice):
    return {'id': identifier, 'label': label, 'principal_kind': principal_kind,
            'authority': authority, 'actions': [
                {'id': key, 'label': title, 'kind': kind} for key, title, kind in actions],
            'scope': scope, 'notice': notice}


_BOUNDARIES = (
    _boundary('account_lifecycle', '账号安全与生命周期', 'named_admin', 'live_named_admin', (
        ('create', '创建账号', 'mutation'), ('security_change', '更改安全属性', 'mutation'),
        ('set_active', '启停账号', 'mutation'), ('reset_password', '重置口令', 'mutation'),
        ('delete', '删除账号', 'mutation')), 'protected_accounts', '实名管理员命令；保留自锁、内置账号与最后管理员保护'),
    _boundary('legacy_template', '规范授权模板管理', 'named_admin', 'live_named_admin', (
        ('read', '读取模板', 'data'), ('read_members', '读取成员', 'data'),
        ('create', '创建模板', 'mutation'), ('update', '修改模板', 'mutation'),
        ('archive', '归档模板', 'mutation'), ('restore', '恢复模板', 'mutation'),
        ('sync', '同步模板', 'mutation')), 'authorization_templates', '模板元数据不授予任何业务访问'),
    _boundary('authorization_admin', '授权管理', 'named_admin', 'live_named_admin', (
        ('read_catalog', '读取授权目录', 'data'), ('find_accounts', '定位授权目标', 'data'),
        ('find_customers', '定位客户归属', 'data'), ('read_user', '诊断账号授权', 'data'),
        ('read_modules', '读取模块授权', 'data'), ('replace_modules', '替换模块授权', 'mutation'),
        ('read_template_modules', '读取模板模块', 'data'), ('replace_template_modules', '替换模板模块', 'mutation'),
        ('sync_template', '应用模板快照', 'mutation'), ('bulk', '原子批量管理', 'mutation'),
        ('retired_user_replace', '退役销售专用写入口', 'retired'),
        ('legacy_metadata', '旧布尔元数据', 'retired')), 'authorization_configuration', '规范模式禁用旧布尔写入与旧矩阵；不依赖普通资料 Grant'),
    _boundary('authentication', '身份认证', 'authentication', 'credential_verification', (
        ('login', '登录', 'authentication'), ('change_password', '本人改密', 'mutation'),
        ('change_password_unauth', '凭旧口令改密', 'authentication'),
        ('feature_snapshot', '登录功能快照', 'data')), 'current_identity', '认证规则独立于员工业务 Grant'),
    _boundary('authorization_snapshot', '当前身份授权快照', 'verified_user', 'current_identity', (
        ('read', '读取本人快照', 'data'),), 'self', '只返回当前验签主体，不存在本人自赋权命令'),
    _boundary('health_probe', '有界健康探测', 'public', 'bounded_public', (
        ('read', '应用探活', 'data'), ('database', '数据库探活', 'data')), 'service_health', '不返回业务记录或内部异常详情'),
    _boundary('framework_documentation', '接口结构文档', 'public', 'framework_schema', (
        ('read', '读取接口结构', 'data'),), 'schema_only', '文档可见不代表接口可调用'),
    _boundary('resale_return_feed', '返件集成', 'service', 'resale_export_credential', (
        ('read', '读取返件', 'data'),), 'fixed_return_contract', '独立返件服务凭据与逐动作配置'),
    _boundary('resale_catalog', '型号目录集成', 'service', 'resale_catalog_credential', (
        ('resolve', '解析型号', 'data'), ('snapshots', '读取型号快照', 'data'),
        ('list', '列举型号', 'data'), ('search', '搜索型号', 'data'),
        ('price_evidence', '读取价格证据', 'data')), 'fixed_catalog_contract', '只读 POST；不继承员工权限或另一服务凭据'),
    _boundary('resale_price_reference', '价格参考集成', 'service', 'resale_catalog_credential', (
        ('read', '读取价格参考', 'data'),), 'fixed_price_contract', '可独立暂停，使用同一型号服务身份'),
)


def boundary_descriptors():
    return deepcopy(list(_BOUNDARIES))


# Method/path are literal server entries, never client-supplied resource names.
BOUNDARY_ENTRY_CONTRACTS = {}


def _entries(boundary, action, adapter, entries, *, branch='default'):
    for entry in entries:
        if entry in BOUNDARY_ENTRY_CONTRACTS:
            raise ValueError('平台入口重复登记')
        BOUNDARY_ENTRY_CONTRACTS[entry] = {
            'boundary_id': boundary, 'action': action, 'adapter': adapter, 'branch': branch}


for _action, _entries_list in {
    'create': ('POST /api/accounts',), 'security_change': ('PUT /api/accounts/{username}',),
    'set_active': ('PUT /api/accounts/{username}/active',),
    'reset_password': ('PUT /api/accounts/{username}/password',),
    'delete': ('DELETE /api/accounts/{username}',),
}.items():
    _entries('account_lifecycle', _action, 'app.authz.management.actor', _entries_list,
             branch='security_change' if _action == 'security_change' else 'default')
for _action, _entries_list in {
    'read': ('GET /api/role-templates',), 'read_members': ('GET /api/role-templates/{code}/accounts',),
    'create': ('POST /api/role-templates',), 'update': ('PUT /api/role-templates/{code}',),
    'archive': ('POST /api/role-templates/{code}/archive',),
    'restore': ('POST /api/role-templates/{code}/restore',),
    'sync': ('POST /api/role-templates/{code}/sync',),
}.items():
    _entries('legacy_template', _action, 'app.authz.management.actor', _entries_list)
for _action, _entries_list in {
    'read_catalog': ('GET /api/authz/catalog',), 'find_accounts': ('GET /api/authz/accounts',),
    'find_customers': ('GET /api/authz/customers',), 'read_user': ('GET /api/authz/users/{username}',),
    'read_modules': ('GET /api/authz/users/{username}/modules',),
    'replace_modules': ('PUT /api/authz/users/{username}/modules',),
    'read_template_modules': ('GET /api/authz/templates/{code}/modules',),
    'replace_template_modules': ('PUT /api/authz/templates/{code}/modules',),
    'sync_template': ('POST /api/authz/templates/{code}/sync',), 'bulk': ('POST /api/accounts/bulk',),
    'retired_user_replace': ('PUT /api/authz/users/{username}',),
    'legacy_metadata': ('GET /api/accounts/_meta',),
}.items():
    _entries('authorization_admin', _action, 'app.authz.management.actor', _entries_list)
for _action, _entries_list in {
    'login': ('POST /api/auth/login',), 'change_password': ('POST /api/auth/change-password',),
    'change_password_unauth': ('POST /api/auth/change-password-unauth',),
    'feature_snapshot': ('GET /api/auth/beta-features',),
}.items():
    _entries('authentication', _action, 'app.auth.' + ('beta_features' if _action == 'feature_snapshot' else _action), _entries_list)
_entries('authorization_snapshot', 'read', 'app.authz.service.my_snapshot', ('GET /api/authz/me',))
_entries('health_probe', 'read', 'app.main.health', ('GET /health',))
_entries('health_probe', 'database', 'app.main.health_db', ('GET /health/db',))
_entries('framework_documentation', 'read', 'fastapi.applications.FastAPI.setup', tuple(
    f'{method} {path}' for path in ('/docs', '/docs/oauth2-redirect', '/openapi.json', '/redoc')
    for method in ('GET', 'HEAD')))
_entries('resale_return_feed', 'read', 'app.authz.machine_access.require_service', ('GET /api/integrations/resale/bad-receipts',))
for _action, _path in {
    'resolve': 'resolve', 'snapshots': 'snapshots', 'list': 'list',
    'search': 'search', 'price_evidence': 'price-evidence',
}.items():
    _entries('resale_catalog', _action, 'app.authz.machine_access.require_service', (f'POST /api/integrations/resale/parts/{_path}',))
_entries('resale_price_reference', 'read', 'app.authz.machine_access.require_service', ('GET /api/integrations/resale/parts/price-reference',))


def validate_boundaries(resource_ids=()):
    ids = [row['id'] for row in _BOUNDARIES]
    if len(ids) != len(set(ids)) or set(ids) & set(resource_ids):
        raise ValueError('平台边界必须与员工资源互斥')
    actions = {row['id']: {a['id'] for a in row['actions']} for row in _BOUNDARIES}
    for entry, contract in BOUNDARY_ENTRY_CONTRACTS.items():
        if (entry.split(' ', 1)[0] not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'}
                or contract['boundary_id'] not in actions
                or contract['action'] not in actions[contract['boundary_id']]):
            raise ValueError('平台入口合同无效')
    return True


validate_boundaries()
del _action, _entries_list, _path
