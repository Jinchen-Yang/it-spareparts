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

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions
from app.config import get_settings
from app.db import get_db
from app.models.system import SysUser

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

_PBKDF2_ITERS = 200_000
# 登录暴力破解防护：连续失败 _LOGIN_MAX_FAILS 次锁定 _LOGIN_LOCK_MINUTES 分钟
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_MINUTES = 15


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
                token_version: int = 0) -> tuple[str, int]:
    exp = int(time.time()) + get_settings().token_ttl_hours * 3600
    payload: dict = {"role": role, "sub": sub, "name": name, "exp": exp, "tv": token_version}
    if perms is not None:
        payload["perms"] = perms
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
    if data.get("fb"):
        return data
    sub = data.get("sub")
    if sub:
        row = db.execute(
            select(SysUser.token_version, SysUser.is_active).where(SysUser.username == sub)
        ).first()
        if row is not None:
            tv, is_active = row
            if not is_active:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已停用，请重新登录")
            if int(data.get("tv", 0)) != int(tv or 0):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录状态已失效，请重新登录")
    return data


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    now = datetime.now(timezone.utc)
    user = db.scalar(select(SysUser).where(SysUser.username == req.username))
    if user is not None:
        # 已存在的账号一律在此处理——停用也绝不跌入下面的共享口令回退（否则停用账号可凭
        # ADMIN_PASSWORD 复活登录，且 fb token 绕过 #15 的吊销/停用校验 → 永久有效）。
        if not user.is_active:
            verify_password(req.password, _DUMMY_PW_HASH)  # 时序抹平：与正常校验等量 pbkdf2
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已停用，请联系管理员")
        # 锁定中：连续失败已达阈值，未到解锁时间则直接拒绝（不消耗 pbkdf2）
        if user.locked_until is not None and user.locked_until > now:
            mins = int((user.locked_until - now).total_seconds() // 60) + 1
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"登录失败次数过多，账号已锁定，请 {mins} 分钟后再试")
        if not verify_password(req.password, user.password_hash):
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= _LOGIN_MAX_FAILS:
                user.locked_until = now + timedelta(minutes=_LOGIN_LOCK_MINUTES)
            db.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
        # 成功：清零失败计数与锁定
        perms = permissions.effective(user.role, user.permissions)
        user.failed_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        db.commit()
        token, exp = _make_token(user.role, user.username, user.salesperson_name,
                                 perms=perms, token_version=user.token_version or 0)
        return LoginResponse(token=token, role=user.role,
                             name=user.display_name or user.salesperson_name,
                             expires_at=exp, permissions=perms)

    # 回退：兼容既有部署的共享口令登录（sys_user 无此账号时）。
    # admin 的 sub 固定为 'admin'（无冒充空间）；其余用户名是自报的，
    # token 标记 fb=True，按 sub 归属的功能（对话会话）会拒绝。
    # 先跑一次等量 pbkdf2 抹平时序：避免"已存在用户名走慢路径、不存在走快路径"暴露有效账号。
    verify_password(req.password, _DUMMY_PW_HASH)
    if not hmac.compare_digest(req.password, get_settings().admin_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    role = "admin" if req.username == "admin" else "readonly"
    perms = permissions.effective(role, None)
    token, exp = _make_token(role, req.username, None, fallback=(role != "admin"), perms=perms)
    return LoginResponse(token=token, role=role, name=req.username, expires_at=exp, permissions=perms)


# ---------- 依赖 ----------
def current_identity(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                     db: Session = Depends(get_db)) -> dict:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少凭证")
    return verify_token_db(creds.credentials, db)


def current_role(ident: dict = Depends(current_identity)) -> str:
    return ident["role"]


def require_admin(role: str = Depends(current_role)) -> str:
    if role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return role
