"""最小登录（§0/§15）：单管理员 + 只读用户。

不引入额外依赖：用 HMAC-SHA256 对 {role, exp} 签名生成 token。
- 管理员：username='admin' 且 password == settings.admin_password → role=admin
- 其他任意 username + 正确口令 → role=readonly
写操作（导入/库存修正/替代料/利润）要求 admin；只读查询放行任意有效 token。
"""
import base64
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    role: str
    expires_at: int


def _sign(payload: bytes) -> str:
    secret = get_settings().secret_key.encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _make_token(role: str) -> tuple[str, int]:
    exp = int(time.time()) + get_settings().token_ttl_hours * 3600
    body = json.dumps({"role": role, "exp": exp}, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{b64}.{_sign(body)}", exp


def _verify_token(token: str) -> str:
    """返回 role；非法/过期抛 401。"""
    try:
        b64, sig = token.split(".", 1)
        body = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        if not hmac.compare_digest(sig, _sign(body)):
            raise ValueError("bad signature")
        data = json.loads(body)
        if data["exp"] < time.time():
            raise ValueError("expired")
        return data["role"]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效或过期的凭证") from exc


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest) -> LoginResponse:
    if not hmac.compare_digest(req.password, get_settings().admin_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    role = "admin" if req.username == "admin" else "readonly"
    token, exp = _make_token(role)
    return LoginResponse(token=token, role=role, expires_at=exp)


def current_role(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少凭证")
    return _verify_token(creds.credentials)


def require_admin(role: str = Depends(current_role)) -> str:
    if role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return role
