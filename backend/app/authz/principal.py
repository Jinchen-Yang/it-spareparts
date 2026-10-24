"""Verified transport identity and live account identity are different types.

No lock is taken here. Writers pass the actor from their complete, ordered lock
set; readers can take a short shared lock before the same revalidation.
"""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions
from app.authz.errors import authz_error
from app.db import get_db
from app.models.system import SysUser

_bearer = HTTPBearer(auto_error=False)
MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    username: str | None
    token_version: int | None
    fallback: bool
    authentication_method: str | None
    legacy_claims: Mapping = field(repr=False, compare=False)
    expected_user_id: int | None = None


@dataclass(frozen=True, slots=True)
class Principal:
    id: int
    username: str
    authorization_epoch: int
    role: str
    display_name: str | None
    salesperson_name: str | None
    legacy_permissions: frozenset[str] = field(repr=False)


def _identity_from_verified_claims(claims: dict) -> VerifiedIdentity:
    """Internal HTTP adapter only; never used to trust request JSON or a ctx."""
    return VerifiedIdentity(
        claims.get("sub"), claims.get("tv"), claims.get("fb") is True,
        claims.get("authn"), MappingProxyType(deepcopy(claims)),
    )


def current_verified_identity(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> VerifiedIdentity:
    if creds is None:
        raise authz_error(401, "AUTHN_SESSION_INVALID", "缺少有效登录凭据")
    from app.auth import verify_token_db
    try:
        claims = verify_token_db(creds.credentials, db)
    except HTTPException:
        raise authz_error(401, "AUTHN_SESSION_INVALID", "登录状态已失效，请重新登录") from None
    return _identity_from_verified_claims(claims)


def load_principal(db: Session, identity: VerifiedIdentity, *, locked_user: SysUser | None = None) -> Principal | None:
    if type(identity) is not VerifiedIdentity:
        raise authz_error(401, "AUTHN_SESSION_INVALID", "需要已验证的类型化身份")
    if identity.fallback:
        return None
    if (identity.authentication_method != "sys_user" or type(identity.username) is not str
            or not identity.username or type(identity.token_version) is not int
            or not 0 <= identity.token_version <= MAX_SAFE_INTEGER):
        raise authz_error(401, "AUTHN_SESSION_INVALID", "需要有效实名账号")
    user = locked_user
    if user is None:
        user = db.scalar(select(SysUser).where(SysUser.username == identity.username)
                         .execution_options(populate_existing=True))
    if (user is None or user.username != identity.username or not user.is_active
            or user.deleted_at is not None or type(user.id) is not int
            or not 0 < user.id <= MAX_SAFE_INTEGER or user.token_version != identity.token_version
            or (identity.expected_user_id is not None and (
                type(identity.expected_user_id) is not int or user.id != identity.expected_user_id))):
        raise authz_error(401, "AUTHN_SESSION_INVALID", "实名账号或登录状态已失效")
    current = permissions.runtime_safe(permissions.effective_for_user(user))
    return Principal(user.id, user.username, user.token_version, user.role,
                     user.display_name, user.salesperson_name,
                     frozenset(key for key, value in current.items() if value is True))


def resume_task_identity(db: Session, owner_user_id: int, enqueued_epoch: int) -> VerifiedIdentity:
    """Trusted worker adapter: a rename follows the same ID; name reuse cannot."""
    if (type(owner_user_id) is not int or not 0 < owner_user_id <= MAX_SAFE_INTEGER
            or type(enqueued_epoch) is not int or not 0 <= enqueued_epoch <= MAX_SAFE_INTEGER):
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '后台任务主体无效')
    user = db.scalar(select(SysUser).where(SysUser.id == owner_user_id)
                     .execution_options(populate_existing=True))
    if user is None or not user.is_active or user.deleted_at is not None or user.token_version != enqueued_epoch:
        raise authz_error(401, 'AUTHN_SESSION_INVALID', '后台任务授权已失效')
    identity = VerifiedIdentity(user.username, enqueued_epoch, False, 'sys_user',
                                MappingProxyType({}), expected_user_id=owner_user_id)
    load_principal(db, identity, locked_user=user)
    return identity


def require_named_admin(principal: Principal | None) -> Principal:
    if type(principal) is not Principal or principal.role != "admin":
        raise authz_error(403, "AUTHZ_NAMED_ADMIN_REQUIRED", "仅有效实名管理员可管理授权")
    return principal


def actor_capabilities(principal: Principal | None, *, read_accounts=False, edit_account_profiles=False) -> dict:
    named_admin = type(principal) is Principal and principal.role == "admin"
    allowed = {
        "read_accounts": type(principal) is Principal and read_accounts is True,
        "edit_account_profiles": type(principal) is Principal and edit_account_profiles is True,
        **{key: named_admin for key in ("manage_authorization", "manage_templates", "create_accounts",
                                       "reset_account_passwords", "manage_account_lifecycle")},
    }
    return {key: {"effect": "allow" if value else "deny", "reason": None if value else {
        "code": "ACCOUNT_PROFILE_CAPABILITY_MISSING" if key in ("read_accounts", "edit_account_profiles") else "NAMED_ADMIN_REQUIRED", "message": "当前账号没有此操作权限",
    }} for key, value in allowed.items()}
