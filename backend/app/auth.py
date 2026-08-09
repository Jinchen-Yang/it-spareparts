"""登录与身份（§0/§15 → 三期 RBAC）。

token 用 HMAC-SHA256 对 {role, sub, name, exp} 签名（无额外依赖）。
登录优先查 sys_user（每用户独立口令，pbkdf2 散列）；为兼容既有部署，
sys_user 里没有的 username 回退老逻辑（admin/admin_password → admin，其余 → readonly）。

**身份只来自服务端校验的 token，绝不信对话内容自报身份**（注入防御的根基）。
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions, security
from app.config import get_settings
from app.db import get_db
from app.models.system import SysAuditLog, SysUser

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

_PBKDF2_ITERS = 200_000
# 登录暴力破解防护：连续失败 _LOGIN_MAX_FAILS 次锁定 _LOGIN_LOCK_MINUTES 分钟
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_MINUTES = 15
_MIN_PASSWORD_LEN = 6   # 与账号管理建号/重置口径一致（accounts.py）


# ---------- 口令散列（pbkdf2，stdlib，无额外依赖） ----------
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


# 时序抹平：未知用户也跑一次等量 pbkdf2，避免"命中用户名慢/未命中快"暴露有效账号（用户名枚举旁路）
_DUMMY_PW_HASH = hash_password("__login_timing_equalizer__")


# ---------- token ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    role: str
    name: str | None = None
    expires_at: int
    permissions: dict | None = None   # 该用户最终权限，前端据此控菜单


def _sign(payload: bytes) -> str:
    sig = hmac.new(get_settings().secret_key.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _make_token(role: str, sub: str, name: str | None,
                fallback: bool = False, perms: dict | None = None,
                token_version: int = 0, authn: str | None = None) -> tuple[str, int]:
    exp = int(time.time()) + get_settings().token_ttl_hours * 3600
    payload: dict = {"role": role, "sub": sub, "name": name, "exp": exp, "tv": token_version}
    if perms is not None:
        payload["perms"] = perms
    if authn is not None:
        payload["authn"] = authn
    if fallback:
        # 共享口令回退登录：sub 是用户自报的任意字符串，不是实名身份。
        # 依赖 sub 做归属的功能（如对话会话）必须拒绝此类 token。
        payload["fb"] = True
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{b64}.{_sign(body)}", exp


def verify_token(token: str) -> dict:
    """返回 payload {role, sub, name, exp}；非法/过期抛 401。"""
    try:
        b64, sig = token.split(".", 1)
        body = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        if not hmac.compare_digest(sig, _sign(body)):
            raise ValueError("bad signature")
        data = json.loads(body)
        if data["exp"] < time.time():
            raise ValueError("expired")
        return data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效或过期的凭证") from exc


# 兼容旧调用：返回 role 字符串
def _verify_token(token: str) -> str:
    return verify_token(token)["role"]


def verify_token_db(token: str, db: Session) -> dict:
    """verify_token + 数据库侧吊销校验：实名 token 的 tv 必须等于用户当前 token_version，
    且账号仍 active。改密/停用/改权限会递增 token_version → 旧 token 立即失效（即时踢线）。

    共享口令回退 token(fb=True，无对应实名用户)只验签名/过期，不做吊销校验。
    部署前签发的旧 token 无 tv 字段 → 视作 tv=0，与初值 0 匹配，不会被误踢（平滑升级）。
    """
    data = verify_token(token)
    # 已签发 token 也可能携带历史非法数据权限组合。签名验证后立即失败关闭，
    # 防止它在下次登录前继续通过直接读取 ctx.permissions 的调用方。
    token_has_perms = isinstance(data.get("perms"), dict)
    if token_has_perms:
        data["perms"] = permissions.runtime_safe(data["perms"])
    if data.get("fb"):
        return data
    sub = data.get("sub")
    if sub:
        user = db.scalar(select(SysUser).where(SysUser.username == sub))
        if user is not None:
            if not user.is_active:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已停用，请重新登录")
            if int(data.get("tv", 0)) != int(user.token_version or 0):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录状态已失效，请重新登录")
            if not token_has_perms:
                # 部署前旧 JWT 没有 perms。不能按 token 里的 role 模板回退：账号可能
                # 已套自定义职位模板/个别调整。每次验签从 DB 的当前有效图生成，并再做
                # 运行时收紧，既保持旧 token 平滑可用，也不绕过账号真实权限。
                data["perms"] = permissions.runtime_safe(
                    permissions.effective_for_user(user)
                )
    return data


def _client_ip(request: Request) -> str | None:
    """登录源 IP：经 nginx 反代时取 X-Forwarded-For 首个，否则直连 IP。"""
    xff = request.headers.get("x-forwarded-for", "")
    return (xff.split(",")[0].strip() if xff else None) or (request.client.host if request.client else None)


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)) -> LoginResponse:
    now = datetime.now(timezone.utc)
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")

    def _ev(action: str, role: str | None = None, detail: dict | None = None) -> None:
        # 登录事件审计（成功/失败/锁定/停用拦截）→ sys_access_log，带源 IP，供暴力破解排查
        security.record_security_event(req.username, role, action, "auth", detail, ip, ua)

    user = db.scalar(select(SysUser).where(SysUser.username == req.username))
    if user is not None:
        # 已存在的账号一律在此处理——停用也绝不跌入下面的共享口令回退（否则停用账号可凭
        # ADMIN_PASSWORD 复活登录，且 fb token 绕过 #15 的吊销/停用校验 → 永久有效）。
        if not user.is_active:
            verify_password(req.password, _DUMMY_PW_HASH)  # 时序抹平：与正常校验等量 pbkdf2
            _ev("login_blocked", user.role, {"reason": "inactive"})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已停用，请联系管理员")
        # 锁定中：连续失败已达阈值，未到解锁时间则直接拒绝（不消耗 pbkdf2）
        if user.locked_until is not None and user.locked_until > now:
            mins = int((user.locked_until - now).total_seconds() // 60) + 1
            _ev("login_locked", user.role, {"locked_until": user.locked_until.isoformat()})
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"登录失败次数过多，账号已锁定，请 {mins} 分钟后再试")
        if not verify_password(req.password, user.password_hash):
            user.failed_attempts = (user.failed_attempts or 0) + 1
            just_locked = user.failed_attempts >= _LOGIN_MAX_FAILS
            if just_locked:
                user.locked_until = now + timedelta(minutes=_LOGIN_LOCK_MINUTES)
            db.commit()
            _ev("login_failed", user.role, {"failed_attempts": user.failed_attempts})
            if just_locked:
                _ev("login_locked", user.role,
                    {"reason": "too_many_failures", "minutes": _LOGIN_LOCK_MINUTES})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
        # 成功：清零失败计数与锁定。权限中心 v2：有效权限=模板快照⊕个别调整
        perms = permissions.runtime_safe(permissions.effective_for_user(user))
        user.failed_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        db.commit()
        _ev("login_success", user.role)
        token, exp = _make_token(user.role, user.username, user.salesperson_name,
                                 perms=perms, token_version=user.token_version or 0,
                                 authn="sys_user")
        return LoginResponse(token=token, role=user.role,
                             name=user.display_name or user.salesperson_name,
                             expires_at=exp, permissions=perms)

    # 回退：兼容既有部署的共享口令登录（sys_user 无此账号时）。
    # admin 的 sub 固定为 'admin'（无冒充空间）；其余用户名是自报的。
    # authn 记录登录来源，供高风险实名门禁失败关闭。非 admin 仍标记 fb=True，
    # 延续按 sub 归属功能对自报用户名的既有保护。
    # 先跑一次等量 pbkdf2 抹平时序：避免"已存在用户名走慢路径、不存在走快路径"暴露有效账号。
    verify_password(req.password, _DUMMY_PW_HASH)
    if not hmac.compare_digest(req.password, get_settings().admin_password):
        _ev("login_failed", None, {"path": "shared_password"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    role = "admin" if req.username == "admin" else "readonly"
    perms = permissions.runtime_safe(permissions.effective(role, None))
    # Shared credentials are not a real SysUser identity and therefore never
    # advertise the high-risk project-master write capability to the client.
    perms["action_maintenance_project_manage"] = False
    perms["action_maintenance_demand_delete"] = False
    _ev("login_success", role, {"path": "shared_password"})
    token, exp = _make_token(
        role,
        req.username,
        None,
        fallback=(role != "admin"),
        perms=perms,
        authn="shared",
    )
    return LoginResponse(token=token, role=role, name=req.username, expires_at=exp, permissions=perms)


# ---------- 依赖 ----------
def current_identity(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                     db: Session = Depends(get_db)) -> dict:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少凭证")
    return verify_token_db(creds.credentials, db)


# ---------- 自助改密 ----------
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordResponse(BaseModel):
    token: str          # 换新密码后即时签发的新 token（当前会话不掉线）
    expires_at: int


class PreauthChangePasswordRequest(BaseModel):
    """登录页（未登录/登出后）改密：靠 用户名+当前密码 自证身份，无需 token。"""
    username: str
    current_password: str
    new_password: str


def _guard_password_attempt(db: Session, user: SysUser, current_pw: str, now: datetime,
                            ev, fail_status: int, fail_msg: str) -> None:
    """当前密码校验闸门（认证/登录页两条改密路径共用同一套锁定逻辑，防拷贝漂移）：
    锁定中 → 429；密码错 → 累加 failed_attempts（达阈值锁定）并抛 fail_status/fail_msg。
    与登录共用 failed_attempts/locked_until（同账号认证失败统一冷却）。成功则静默返回。"""
    if user.locked_until is not None and user.locked_until > now:
        mins = int((user.locked_until - now).total_seconds() // 60) + 1
        ev("change_password_locked", user.role, {"locked_until": user.locked_until.isoformat()})
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"密码尝试次数过多，请 {mins} 分钟后再试")
    if not verify_password(current_pw, user.password_hash):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        just_locked = user.failed_attempts >= _LOGIN_MAX_FAILS
        if just_locked:
            user.locked_until = now + timedelta(minutes=_LOGIN_LOCK_MINUTES)
        db.commit()
        ev("change_password_failed", user.role, {"failed_attempts": user.failed_attempts})
        if just_locked:
            ev("change_password_locked", user.role,
               {"reason": "too_many_failures", "minutes": _LOGIN_LOCK_MINUTES})
        raise HTTPException(fail_status, fail_msg)


def _set_new_password(db: Session, user: SysUser, new_pw: str, operated_by: str) -> None:
    """校验并应用新密码（调用方须先过 _guard_password_attempt 验证当前密码）：
    长度 → 不同于旧 → 写 hash + 清失败计数/锁定 + 递增 token_version（踢其他会话）+ 审计。
    审计只记「发生改密」，绝不落明文/hash。"""
    if len(new_pw or "") < _MIN_PASSWORD_LEN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"新密码至少 {_MIN_PASSWORD_LEN} 位")
    if verify_password(new_pw, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与当前密码相同")
    user.password_hash = hash_password(new_pw)
    user.failed_attempts = 0
    user.locked_until = None
    user.token_version = (user.token_version or 0) + 1
    db.add(SysAuditLog(entity_type="sys_user", entity_id=user.id,
                       action="account_change_password", before_json=None, after_json=None,
                       operated_by=operated_by, reason="用户自助修改密码"))
    db.commit()


@router.post("/change-password", response_model=ChangePasswordResponse)
def change_password(req: ChangePasswordRequest, request: Request,
                    ident: dict = Depends(current_identity),
                    db: Session = Depends(get_db)) -> ChangePasswordResponse:
    """登录用户自助改密：验当前密码 → 设新密码 → 递增 token_version 踢其他会话 →
    签发新 token 返回（本次会话不掉线，其余设备下次请求即 401 重登）。

    共享口令回退登录（fb token / 无对应 sys_user 行，如 ADMIN_PASSWORD 登录的 admin）
    没有可改的账号行 → 明确拒绝，引导改环境变量。审计只记「发生改密」，绝不落明文/hash。
    """
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    sub = ident.get("sub")

    def _ev(action: str, role: str | None, detail: dict | None = None) -> None:
        security.record_security_event(sub, role, action, "auth", detail, ip, ua)

    if ident.get("fb") or not sub:
        _ev("change_password_rejected", ident.get("role"), {"reason": "shared_password"})
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "当前为共享口令登录，无法自助改密；请用独立账号登录或联系管理员")
    user = db.scalar(select(SysUser).where(SysUser.username == sub))
    if user is None:
        # 无 fb 标记但也无实名行：ADMIN_PASSWORD 登录的 admin（sub='admin' 但未建号）
        _ev("change_password_rejected", ident.get("role"), {"reason": "no_account_row"})
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "当前账号通过共享口令登录，无法自助改密；请联系管理员改用独立账号")
    # current_identity 已校验 is_active/tv，此处仅防御性
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已停用，请重新登录")
    # 当前密码尝试防爆破：与登录共用失败计数/锁定（安全审查 CP-1）。虽需先持有效会话才能
    # 到这里，但零成本复用可挡住会话被盗后的当前密码爆破。
    now = datetime.now(timezone.utc)
    _guard_password_attempt(db, user, req.current_password, now, _ev,
                            status.HTTP_400_BAD_REQUEST, "当前密码不正确")
    _set_new_password(db, user, req.new_password, operated_by=sub)
    _ev("change_password", user.role)

    perms = permissions.runtime_safe(permissions.effective_for_user(user))
    token, exp = _make_token(user.role, user.username, user.salesperson_name,
                             perms=perms, token_version=user.token_version,
                             authn="sys_user")
    return ChangePasswordResponse(token=token, expires_at=exp)


@router.post("/change-password-unauth")
def change_password_unauth(req: PreauthChangePasswordRequest, request: Request,
                           db: Session = Depends(get_db)) -> dict:
    """登录页改密（未登录）：靠 用户名+当前密码 自证身份，无 token。改后不自动登录——
    递增 token_version 使所有旧会话失效，用户用新密码重新登录。

    安全同登录门：未知用户/停用/仅共享口令账号(无 sys_user 行) → 跑一次等量 pbkdf2 抹平
    时序、返回不泄露账号存在性的统一错；当前密码错累加同一失败计数（与登录共用锁定）。
    """
    now = datetime.now(timezone.utc)
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")

    def _ev(action: str, role: str | None, detail: dict | None = None) -> None:
        security.record_security_event(req.username, role, action, "auth", detail, ip, ua)

    user = db.scalar(select(SysUser).where(SysUser.username == req.username))
    if user is None:
        # 未知用户名 / 仅靠 ADMIN_PASSWORD 登录的账号（无实名行）：时序抹平 + 不泄露存在性
        verify_password(req.current_password, _DUMMY_PW_HASH)
        _ev("change_password_failed", None, {"path": "preauth", "reason": "unknown"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或当前密码错误")
    if not user.is_active:
        verify_password(req.current_password, _DUMMY_PW_HASH)  # 时序抹平
        _ev("change_password_blocked", user.role, {"path": "preauth", "reason": "inactive"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已停用，请联系管理员")
    # 未知用户与"用户存在但密码错"返回同一 401 文案，避免用户名枚举
    _guard_password_attempt(db, user, req.current_password, now, _ev,
                            status.HTTP_401_UNAUTHORIZED, "用户名或当前密码错误")
    _set_new_password(db, user, req.new_password, operated_by=user.username)
    _ev("change_password", user.role, {"path": "preauth"})
    return {"changed": True}


def current_role(ident: dict = Depends(current_identity)) -> str:
    return ident["role"]


def require_admin(role: str = Depends(current_role)) -> str:
    if role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return role


def require_roles(*roles: str):
    """端点级角色门：admin 恒通过，另允许 roles 中任一角色，其余 403。
    用于把某操作开放给特定业务角色（如替代料维护开放给采购）。"""
    allowed = {"admin", *roles}

    def _dep(role: str = Depends(current_role)) -> str:
        if role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "无此操作权限")
        return role

    return _dep
