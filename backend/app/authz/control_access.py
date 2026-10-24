"""Ordinary account data permissions, independent of administrator commands."""
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.authz import principal, runtime, service
from app.authz.context import read_snapshot
from app.authz.errors import authz_error
from app.models.system import SysAccessLog, SysAuditLog, SysRoleTemplate, SysUser

ACCOUNT_DATA_ENTRY_CONTRACTS = {
    'GET /api/accounts': {'resource': 'account_directory', 'action': 'read', 'branch': 'default'},
    'GET /api/accounts/{username}/activity': {'resource': 'account_directory', 'action': 'read_activity', 'branch': 'default'},
    'PUT /api/accounts/{username}': {'resource': 'account_profile', 'action': 'update', 'branch': 'profile'},
}
CAPABILITY_ACTIONS = {
    'read_accounts': ('account_directory', 'read'),
    'edit_account_profiles': ('account_profile', 'update'),
}


def legacy_allowed(who, resource):
    if who is None:
        return False
    page = who.role == 'admin' or 'page_accounts' in who.legacy_permissions
    return bool(page and (resource == 'account_directory' or who.role == 'admin'
                          or 'action_account_manage' in who.legacy_permissions))


def require_account_action(db, identity, resource, action, *, read_proof=None, locked_user=None):
    if (resource, action) not in {('account_directory', 'read'), ('account_directory', 'read_activity'),
                                 ('account_profile', 'update')}:
        raise ValueError('未登记的账号资料动作')
    state = runtime.runtime_states(db)[resource]
    if state.runtime_state == 'legacy':
        if read_proof is not None:
            read_proof.validate(db)
        who = principal.load_principal(db, identity, locked_user=locked_user)
        if not legacy_allowed(who, resource):
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '当前账号没有此资料操作权限')
        return who
    return service.resource_access(db, identity, resource, action,
        read_snapshot=read_proof, locked_user=locked_user).principal


def require_capability(db, identity, capability, *, locked_user=None):
    resource, action = CAPABILITY_ACTIONS[capability]
    if locked_user is not None:
        return require_account_action(db, identity, resource, action, locked_user=locked_user)
    # Preflight ends before the writer takes its stable, sorted final lock set.
    with read_snapshot(lambda: Session(db.get_bind())) as (reader, proof):
        return require_account_action(reader, identity, resource, action, read_proof=proof)


def capabilities_from_snapshot(who, states, decisions):
    allowed = {}
    for capability, (resource, action) in CAPABILITY_ACTIONS.items():
        state = states[resource]
        decision = decisions.get(resource)
        allowed[capability] = (legacy_allowed(who, resource) if state.runtime_state == 'legacy' else
                              state.runtime_state == 'enabled' and decision is not None
                              and action in decision.allowed_actions)
    return principal.actor_capabilities(who, **allowed)


def account_rows(db):
    """Only target identity and canonical source summary enter the directory."""
    from app.authz import management
    templates = {row.code: row for row in db.execute(select(SysRoleTemplate.code, SysRoleTemplate.version))}
    columns = (SysUser.id, SysUser.username, SysUser.display_name, SysUser.role,
               SysUser.salesperson_name, SysUser.is_active, SysUser.token_version,
               SysUser.authorization_profile)
    result = []
    for row in db.execute(select(*columns).where(SysUser.deleted_at.is_(None)).order_by(SysUser.id)):
        user = SimpleNamespace(**row._mapping)
        summary = None
        try:
            summary = management.account_summaries([user], templates)[user.id]
        except HTTPException as exc:
            if exc.status_code != 409 or not isinstance(exc.detail, dict) or exc.detail.get('code') != 'AUTHZ_SOURCE_INVALID':
                raise
        result.append({**{key: getattr(user, key) for key in
            ('id', 'username', 'display_name', 'role', 'salesperson_name', 'is_active')},
            'authorization_epoch': user.token_version, 'authorization_summary': summary,
            'authorization_summary_status': 'available' if summary is not None else 'invalid'})
    return result


def activity_rows(db, username, limit):
    target = db.execute(select(SysUser.id, SysUser.last_login_at).where(
        SysUser.username == username, SysUser.deleted_at.is_(None))).first()
    if target is None:
        raise authz_error(404, 'ACCOUNT_NOT_FOUND', '账号不存在')
    recent = db.execute(select(SysAccessLog.action, SysAccessLog.ip_address, SysAccessLog.created_at)
        .where(SysAccessLog.username == username).order_by(desc(SysAccessLog.id)).limit(min(max(limit, 1), 200)))
    changes = db.execute(select(SysAuditLog.action, SysAuditLog.operated_by, SysAuditLog.operated_at)
        .where(SysAuditLog.entity_type == 'sys_user', SysAuditLog.entity_id == target.id)
        .order_by(desc(SysAuditLog.id)).limit(50))
    total = db.scalar(select(func.count()).select_from(SysAccessLog).where(SysAccessLog.username == username))
    return {'username': username, 'last_login_at': target.last_login_at, 'total_actions': total or 0,
        'recent': [{'action': r.action, 'resource': None, 'ip': r.ip_address, 'at': r.created_at} for r in recent],
        'changes': [{'action': c.action, 'by': c.operated_by, 'at': c.operated_at,
                     'reason': None, 'before': None, 'after': None} for c in changes]}
