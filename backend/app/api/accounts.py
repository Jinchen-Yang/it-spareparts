"""账号与权限管理（管理员专用）：建号 / 改密 / 停用 / 勾权限 / 看活动。

改密 / 停用 / 改角色或权限会递增 sys_user.token_version → 该用户**已签发的旧 token 立即失效**
（下次请求被 verify_token_db 拒，需重新登录）。即时踢线，不再等下次登录。
admin 不可改角色、不可停用、权限恒全开（不可自锁）。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app import permissions
from app.auth import current_identity, hash_password, require_admin
from app.db import get_db
from app.models.system import SysAccessLog, SysAuditLog, SysUser

router = APIRouter(prefix="/accounts", tags=["accounts"])

_ROLES = ["admin", "boss", "sales", "purchaser", "readonly"]


class CreateAccount(BaseModel):
    username: str
    password: str
    role: str = "sales"
    display_name: str | None = None
    salesperson_name: str | None = None
    permissions: dict | None = None   # 不给则用角色模板


class UpdateAccount(BaseModel):
    role: str | None = None
    display_name: str | None = None
    salesperson_name: str | None = None
    permissions: dict | None = None


class PasswordReset(BaseModel):
    password: str


class ActiveToggle(BaseModel):
    is_active: bool


def _view(u: SysUser) -> dict:
    return {
        "username": u.username, "display_name": u.display_name, "role": u.role,
        "salesperson_name": u.salesperson_name, "is_active": u.is_active,
        "last_login_at": u.last_login_at,
        "permissions": permissions.effective(u.role, u.permissions),
        "is_custom": bool(u.permissions),
    }


def _get(db: Session, username: str) -> SysUser:
    u = db.scalar(select(SysUser).where(SysUser.username == username))
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"账号不存在: {username}")
    return u


def _acct_snapshot(u: SysUser) -> dict:
    """账号审计快照——绝不含口令/hash，只记可见属性变化。"""
    return {
        "username": u.username, "role": u.role, "display_name": u.display_name,
        "salesperson_name": u.salesperson_name, "is_active": u.is_active,
        "permissions": u.permissions,
    }


def _audit(db: Session, user_id: int, action: str, before: dict | None,
           after: dict | None, operated_by: str | None, reason: str | None = None) -> None:
    """账号变更留痕 → sys_audit_log(entity_type='sys_user')。随业务事务一起 commit。"""
    db.add(SysAuditLog(entity_type="sys_user", entity_id=user_id, action=action,
                       before_json=before, after_json=after,
                       operated_by=operated_by, reason=reason))


@router.get("/_meta")
def meta(_: str = Depends(require_admin)) -> dict:
    """权限项 + 标签 + 角色模板，供前端渲染勾选框。"""
    return {
        "roles": _ROLES,
        "data_keys": list(permissions.DATA_GROUPS),
        "page_keys": permissions.PAGE_KEYS,
        "action_keys": permissions.ACTION_KEYS,
        "row_keys": permissions.ROW_KEYS,
        "labels": permissions.LABELS,
        "role_templates": {r: permissions.effective(r, None) for r in _ROLES},
    }


@router.get("")
def list_accounts(db: Session = Depends(get_db), _: str = Depends(require_admin)) -> list[dict]:
    users = db.execute(select(SysUser).order_by(SysUser.id)).scalars().all()
    return [_view(u) for u in users]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(body: CreateAccount, db: Session = Depends(get_db),
                   ident: dict = Depends(current_identity),
                   _: str = Depends(require_admin)) -> dict:
    uname = (body.username or "").strip()
    if not uname:
        raise HTTPException(400, "用户名不能为空")
    if body.role not in _ROLES:
        raise HTTPException(400, f"角色非法: {body.role}")
    if len(body.password or "") < 6:
        raise HTTPException(400, "密码至少 6 位")
    if db.scalar(select(SysUser).where(SysUser.username == uname)):
        raise HTTPException(409, f"用户名已存在: {uname}")
    custom = permissions.sanitize(body.permissions) or None
    # 拒绝非法权限组合（复审阻塞 4）：按最终生效权限（模板+自定义叠加）校验
    combo = permissions.combo_errors(permissions.effective(body.role, custom))
    if combo:
        raise HTTPException(400, "；".join(combo))
    u = SysUser(username=uname, role=body.role, display_name=body.display_name,
                salesperson_name=body.salesperson_name,
                password_hash=hash_password(body.password),
                permissions=custom)
    db.add(u)
    db.flush()   # 取 u.id 供审计
    _audit(db, u.id, "account_create", None, _acct_snapshot(u), ident["sub"])
    db.commit()
    return _view(u)


@router.put("/{username}")
def update_account(username: str, body: UpdateAccount, db: Session = Depends(get_db),
                   ident: dict = Depends(current_identity),
                   _: str = Depends(require_admin)) -> dict:
    u = _get(db, username)
    before = _acct_snapshot(u)
    if body.role is not None:
        if body.role not in _ROLES:
            raise HTTPException(400, f"角色非法: {body.role}")
        if username == "admin" and body.role != "admin":
            raise HTTPException(400, "不能改 admin 的角色")
        u.role = body.role
    if body.display_name is not None:
        u.display_name = body.display_name
    if body.salesperson_name is not None:
        u.salesperson_name = body.salesperson_name
    if body.permissions is not None:
        u.permissions = permissions.sanitize(body.permissions) or None
    # 拒绝非法权限组合（复审阻塞 4）：角色与自定义都可能变，按变更后的最终生效权限校验。
    # 400 时依赖 get_db 关闭会话丢弃未提交改动，账号保持原样。
    if body.role is not None or body.permissions is not None:
        combo = permissions.combo_errors(permissions.effective(u.role, u.permissions))
        if combo:
            raise HTTPException(400, "；".join(combo))
    # 角色/权限变更 → 吊销旧 token，迫使重新登录以即时生效（仅改显示名则不踢）
    if body.role is not None or body.permissions is not None:
        u.token_version = (u.token_version or 0) + 1
    _audit(db, u.id, "account_update", before, _acct_snapshot(u), ident["sub"])
    db.commit()
    return _view(u)


@router.put("/{username}/password")
def reset_password(username: str, body: PasswordReset, db: Session = Depends(get_db),
                   ident: dict = Depends(current_identity),
                   _: str = Depends(require_admin)) -> dict:
    if len(body.password or "") < 6:
        raise HTTPException(400, "密码至少 6 位")
    u = _get(db, username)
    u.password_hash = hash_password(body.password)
    u.token_version = (u.token_version or 0) + 1   # 改密即吊销旧 token
    # 审计只记"发生了改密"事件，绝不记口令/hash
    _audit(db, u.id, "account_reset_password", None, None, ident["sub"])
    db.commit()
    return {"username": username, "reset": True}


@router.put("/{username}/active")
def set_active(username: str, body: ActiveToggle, db: Session = Depends(get_db),
               ident: dict = Depends(current_identity),
               _: str = Depends(require_admin)) -> dict:
    if username == "admin" and not body.is_active:
        raise HTTPException(400, "不能停用 admin")
    u = _get(db, username)
    before = _acct_snapshot(u)
    u.is_active = body.is_active
    if not body.is_active:
        u.token_version = (u.token_version or 0) + 1   # 停用即吊销旧 token（立即踢线）
    _audit(db, u.id, "account_set_active", before, _acct_snapshot(u), ident["sub"])
    db.commit()
    return {"username": username, "is_active": u.is_active}


@router.get("/{username}/activity")
def activity(username: str, limit: int = 50, db: Session = Depends(get_db),
             _: str = Depends(require_admin)) -> dict:
    u = _get(db, username)
    n = min(max(limit, 1), 200)
    rows = db.execute(
        select(SysAccessLog).where(SysAccessLog.username == username)
        .order_by(desc(SysAccessLog.id)).limit(n)).scalars().all()
    total = db.scalar(select(func.count()).select_from(SysAccessLog)
                      .where(SysAccessLog.username == username))
    # 该账号被谁改过（建号/改权/改密/停用）——entity_type='sys_user' 按 user_id 取
    changes = db.execute(
        select(SysAuditLog).where(SysAuditLog.entity_type == "sys_user",
                                  SysAuditLog.entity_id == u.id)
        .order_by(desc(SysAuditLog.id)).limit(50)).scalars().all()
    return {
        "username": username, "last_login_at": u.last_login_at, "total_actions": total or 0,
        "recent": [{"action": r.action, "resource": r.resource, "ip": r.ip_address,
                    "at": r.created_at} for r in rows],
        "changes": [{"action": c.action, "by": c.operated_by, "at": c.operated_at,
                     "before": c.before_json, "after": c.after_json} for c in changes],
    }
