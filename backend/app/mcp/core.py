"""Authentication and durable primitives. No client-supplied identity is trusted."""

import hashlib
import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text

from app import config, permissions
from app.auth import verify_token_db
from app.config import get_settings
from app.db import SessionLocal
from app.models.mcp import McpAuditEvent, McpCredential, McpRecord
from app.models.system import SysUser
from app.security import UserContext

SCOPES = {
    "mcp.use",
    "mcp.files.upload",
    "mcp.files.read",
    "mcp.files.download",
    "mcp.projects.read",
    "mcp.import.preview",
    "mcp.jobs.read",
    "mcp.export",
    "mcp.parts.read",
    "mcp.inventory.read",
    "mcp.audit.read",
}


class McpError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid.uuid4())


def digest(value):
    return hashlib.sha256(
        json.dumps(
            jsonable_encoder(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


@dataclass
class Actor:
    name: str
    ctx: UserContext
    scopes: set[str]
    version: int
    credential_id: str | None = None

    def snapshot(self):
        return {
            "name": self.name,
            "version": self.version,
            "credential_id": self.credential_id,
            "scopes": sorted(self.scopes),
        }


actor_context: ContextVar[Actor] = ContextVar("mcp_actor")


def enabled():
    settings = get_settings()
    if not settings.mcp_enabled:
        raise McpError("feature_disabled", "MCP 尚未启用", 404)
    if not config.ENABLE_RBAC or not config.ENABLE_ACCESS_LOG:
        raise McpError("unsafe_configuration", "MCP 需要权限和审计开启", 503)
    from urllib.parse import urlsplit

    origin = urlsplit(settings.mcp_public_base_url)
    if (
        origin.scheme not in ("http", "https")
        or not origin.netloc
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
    ):
        raise McpError(
            "unsafe_configuration", "MCP 入口必须为无路径的 HTTP(S) 地址", 503
        )
    if settings.environment == "prod" and not settings.mcp_public_base_url.startswith(
        "https://"
    ):
        raise McpError("unsafe_configuration", "生产 MCP 必须配置 HTTPS 入口", 503)


def actor_for(db, name, *, version=None, scopes=None, credential_id=None):
    user = db.scalar(
        select(SysUser)
        .where(SysUser.username == name)
        .execution_options(populate_existing=True)
    )
    if (
        not user
        or not user.is_active
        or (version is not None and user.token_version != version)
    ):
        raise McpError("unauthenticated", "账号或授权已失效", 401)
    graph = permissions.runtime_safe(permissions.effective_for_user(user))
    return Actor(
        user.username,
        UserContext(
            user_id=user.username,
            role=user.role,
            salesperson_name=user.salesperson_name,
            permissions=graph,
            is_authenticated=True,
        ),
        set(scopes if scopes is not None else SCOPES),
        user.token_version,
        credential_id,
    )


def authenticate(token, *, allow_jwt=False):
    enabled()
    with SessionLocal() as db:
        if token.startswith("pfm_"):
            cred = db.scalar(
                select(McpCredential).where(
                    McpCredential.token_hash
                    == hashlib.sha256(token.encode()).hexdigest()
                )
            )
            if not cred or cred.revoked_at or cred.expires_at <= now():
                raise McpError("unauthenticated", "连接凭据无效或过期", 401)
            return actor_for(
                db,
                cred.owner,
                version=cred.auth_version,
                scopes=cred.scopes,
                credential_id=cred.id,
            )
        if allow_jwt:
            try:
                ident = verify_token_db(token, db)
                if ident.get("fb"):
                    raise McpError("unauthenticated", "请使用独立业务账号", 401)
                return actor_for(db, ident.get("sub"), version=ident.get("tv", 0))
            except HTTPException as exc:
                raise McpError("unauthenticated", "请登录独立业务账号", 401) from exc
        raise McpError("unauthenticated", "需要个人 MCP 连接凭据", 401)


def refresh_actor(db, actor):
    enabled()
    if actor.credential_id:
        cred = db.get(McpCredential, actor.credential_id, populate_existing=True)
        if (
            not cred
            or cred.owner != actor.name
            or cred.revoked_at
            or cred.expires_at <= now()
        ):
            raise McpError("unauthenticated", "连接授权已撤销", 401)
        scopes = actor.scopes & set(cred.scopes)
    else:
        scopes = actor.scopes
    return actor_for(
        db,
        actor.name,
        version=actor.version,
        scopes=scopes,
        credential_id=actor.credential_id,
    )


def require(actor, scope, page=None):
    if scope not in actor.scopes:
        raise McpError("permission_denied", "此连接未授权该能力", 403)
    if page and not permissions.page_permission_allowed(
        role=actor.ctx.role, permission_map=actor.ctx.permissions, page_key=page
    ):
        raise McpError("permission_denied", "无对应业务页面权限", 403)


def event(db, actor, operation, stage, detail=None, request_id=None):
    e = McpAuditEvent(
        id=uid(),
        request_id=request_id or uid(),
        owner=actor.name,
        credential_id=actor.credential_id,
        operation=operation,
        stage=stage,
        detail=jsonable_encoder(detail or {}),
    )
    db.add(e)
    db.flush()  # mandatory: failure propagates before data/side effects are released
    return e.request_id


def audit(actor, operation, stage, detail=None, request_id=None):
    try:
        with SessionLocal() as db:
            result = event(db, actor, operation, stage, detail, request_id)
            db.commit()
            return result
    except Exception as exc:
        raise McpError(
            "audit_unavailable", "审计暂不可用，本次操作未继续", 503
        ) from exc


def record(
    db, actor, kind, payload, *, state="ready", key=None, request=None, hours=24
):
    if key:
        # Serialize idempotent creation across workers, including first insert.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"mcp:{actor.name}:{kind}:{key}"},
        )
        old = db.scalar(
            select(McpRecord).where(
                McpRecord.owner == actor.name,
                McpRecord.kind == kind,
                McpRecord.key == key,
            )
        )
        if old:
            if old.request_hash != digest(request):
                raise McpError("idempotency_conflict", "同一请求标识对应不同内容", 409)
            if old.expires_at <= now():
                raise McpError(
                    "expired_request", "原请求已过期，请使用新的请求标识", 409
                )
            return old
    r = McpRecord(
        id=uid(),
        owner=actor.name,
        kind=kind,
        payload=jsonable_encoder(payload),
        state=state,
        key=key,
        request_hash=digest(request) if key else None,
        expires_at=now() + timedelta(hours=hours),
    )
    db.add(r)
    db.flush()
    return r


def owned(db, actor, id, kind, *, lock=False, expired=False):
    stmt = select(McpRecord).where(
        McpRecord.id == id, McpRecord.owner == actor.name, McpRecord.kind == kind
    )
    if lock:
        stmt = stmt.with_for_update()
    r = db.scalar(stmt.execution_options(populate_existing=True))
    if not r or (not expired and r.expires_at <= now()):
        raise McpError("not_found", "记录不存在或已过期", 404)
    return r


def public_url(path):
    return get_settings().mcp_public_base_url.rstrip("/") + path
