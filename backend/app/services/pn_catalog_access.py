"""PN domain adapter for the existing identity → Grant → Cerbos authority.

Transport proofs bind a verified shop session to one HTTP request. They never
carry roles or grants. Domain ownership checks (inbox/job) remain in that domain.
All locks and nonce consumption join the caller's transaction; no inner commit.
"""

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth import verify_token_db
from app.authz.catalog import CATALOG_VERSION, get_resource_definition
from app.authz.pn_models import CatalogProofNonce, CatalogSubjectBinding
from app.authz.pn_resources import CATALOG_ACTIONS
from app.authz.principal import (
    VerifiedIdentity,
    _identity_from_verified_claims,
    load_principal,
)
from app.authz.service import resource_access
from app.config import get_settings
from app.models.system import SysUser
from app.pn_catalog.authz import CatalogAccess
from app.pn_catalog.dto import ActorContext, AuthorizationDecision
from app.pn_catalog.errors import CatalogError

AUDIENCE = "itsp:pn-catalog:v1"
PROOF_PURPOSE = "pn-shop-session-proof:v1"
TOKEN_PURPOSE = "pn-authority-delegation:v1"
MIGRATION_ACTIONS = frozenset(
    {"stage_migration", "materialize_migration", "read_legacy", "migrate_content"}
)
READ_ACTIONS = frozenset(
    {
        "read",
        "read_legacy",
        "list",
        "read_content",
        "read_attributes",
        "read_inactive",
        "read_audit",
        "read_candidates",
        "manage_identity",
        "identity.read",
        "gallery.read",
        "uploads.read",
        "inbox.read",
        "inbox.shared.read",
        "job.read",
        "research.job.read",
        "enrichment.bundle",
        "enrichment.bundle.read",
    }
)


class RequestBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"]
    target: str = Field(min_length=1, max_length=4096)
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: UUID
    if_match: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def catalog_target(self):
        # No authority crossing, URL normalization or alternate API namespace.
        prefix = get_settings().api_prefix.rstrip("/") + "/pn-catalog/v1"
        path = self.target.split("?", 1)[0]
        if not path.startswith(prefix + "/") or any(
            c in self.target for c in ("\r", "\n", "#")
        ):
            raise ValueError("target must be a PN catalog path")
        return self


class ShopProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    purpose: Literal["pn-shop-session-proof:v1"]
    audience: Literal["itsp:pn-catalog:v1"]
    issuer: str = Field(min_length=1, max_length=256)
    client_id: str = Field(min_length=1, max_length=128)
    subject: UUID
    session_id: UUID
    nonce: UUID
    issued_at: int
    expires_at: int
    binding: RequestBinding


class Delegation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    purpose: Literal["pn-authority-delegation:v1"]
    audience: Literal["itsp:pn-catalog:v1"]
    issuer: str
    client_id: str
    subject: UUID
    user_id: int = Field(gt=0)
    authorization_epoch: int = Field(ge=0)
    mapping_version: int = Field(ge=1)
    client_policy_version: str
    issued_at: int
    expires_at: int
    binding: RequestBinding


def _fail(
    code="auth.invalid_delegation", message="共享资料委托凭据无效", *, status=401
):
    raise CatalogError(code, message, status_code=status)


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _keys():
    settings = get_settings()
    authority = settings.pn_delegation_secret.get_secret_value().encode()
    shop = settings.pn_shop_proof_secret.get_secret_value().encode()
    if (
        not settings.pn_shop_issuer
        or settings.pn_shop_issuer == "itsp"
        or not settings.pn_shop_client_id
        or settings.pn_shop_client_id == "itsp"
        or len(authority) < 32
        or len(shop) < 32
        or authority == shop
        or settings.secret_key.encode() in (authority, shop)
    ):
        _fail("auth.unavailable", "共享资料委托尚未安全配置", status=503)
    if (
        set(settings.pn_shop_allowed_actions) & MIGRATION_ACTIONS
        or "legacy_evidence" in settings.pn_shop_allowed_fields.get("part_master", [])
        or any(a not in CATALOG_ACTIONS for a in settings.pn_shop_allowed_actions)
    ) or any(
        r not in {"part_master", "part_identity", "part_content", "part_gallery"}
        or not set(fields) <= get_resource_definition(r).field_permissions
        for r, fields in settings.pn_shop_allowed_fields.items()
    ):
        _fail("auth.unavailable", "共享资料客户端能力配置无效", status=503)
    return authority, shop


def client_policy_version():
    authority, shop = _keys()
    s = get_settings()
    return hashlib.sha256(
        _json(
            {
                "issuer": s.pn_shop_issuer,
                "client": s.pn_shop_client_id,
                "source_namespace": s.pn_shop_source_namespace,
                "actions": sorted(set(s.pn_shop_allowed_actions)),
                "fields": {
                    k: sorted(set(v)) for k, v in s.pn_shop_allowed_fields.items()
                },
                "authority_key": hashlib.sha256(authority).hexdigest(),
                "proof_key": hashlib.sha256(shop).hexdigest(),
            }
        )
    ).hexdigest()


def _encode(model, key):
    body = _json(model.model_dump(mode="json"))
    payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
    signature = (
        base64.urlsafe_b64encode(hmac.new(key, body, hashlib.sha256).digest())
        .decode()
        .rstrip("=")
    )
    return payload + "." + signature


def _decode(token, key, schema, ttl):
    if not isinstance(token, str) or len(token) > 16384:
        _fail()
    try:
        payload, signature = token.split(".")
        body = base64.b64decode(
            payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True
        )
        expected = (
            base64.urlsafe_b64encode(hmac.new(key, body, hashlib.sha256).digest())
            .decode()
            .rstrip("=")
        )
        if not hmac.compare_digest(expected, signature):
            _fail()
        value = schema.model_validate_json(body)
        now = int(time.time())
        if (
            not 0 <= now - value.issued_at <= ttl
            or not now < value.expires_at <= value.issued_at + ttl
        ):
            _fail()
        return value
    except (ValueError, TypeError, ValidationError):
        _fail()


def _current_user(db, user_id, epoch=None):
    user = db.scalar(
        select(SysUser)
        .where(SysUser.id == user_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or not user.is_active
        or user.deleted_at is not None
        or (epoch is not None and user.token_version != epoch)
    ):
        _fail("auth.session_revoked", "当前账号或授权版本已失效")
    return user


def _current_mapping(db, issuer, subject, user_id, version=None):
    row = db.scalar(
        select(CatalogSubjectBinding)
        .where(
            CatalogSubjectBinding.issuer == issuer,
            CatalogSubjectBinding.subject == subject,
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or not row.active
        or row.user_id != user_id
        or (version is not None and row.version != version)
    ):
        _fail("auth.mapping_revoked", "跨系统账号映射不存在或已变更")
    return row


def exchange_delegation(db: Session, proof: str) -> dict:
    """Consume one ≤30s signed session proof; caller commits before returning."""
    authority, shop = _keys()
    claim = _decode(proof, shop, ShopProof, 30)
    settings = get_settings()
    if (
        claim.issuer != settings.pn_shop_issuer
        or claim.client_id != settings.pn_shop_client_id
    ):
        _fail()
    # Preliminary ID resolution is rechecked after user→mapping locks.
    mapped = db.scalar(
        select(CatalogSubjectBinding.user_id).where(
            CatalogSubjectBinding.issuer == claim.issuer,
            CatalogSubjectBinding.subject == claim.subject,
        )
    )
    if mapped is None:
        _fail("auth.mapping_required", "此商城账号尚未绑定主数据库账号", status=403)
    user = _current_user(db, mapped)
    mapping = _current_mapping(db, claim.issuer, claim.subject, user.id)
    accepted = db.scalar(
        insert(CatalogProofNonce)
        .values(
            issuer=claim.issuer,
            nonce=claim.nonce,
            expires_at=datetime.fromtimestamp(claim.expires_at, timezone.utc),
        )
        .on_conflict_do_nothing()
        .returning(CatalogProofNonce.nonce)
    )
    if accepted is None:
        _fail("auth.proof_replayed", "会话证明已经使用")
    # Bounded housekeeping; never delete a still-live nonce.
    stale = (
        select(CatalogProofNonce.issuer, CatalogProofNonce.nonce)
        .where(CatalogProofNonce.expires_at < datetime.now(timezone.utc))
        .limit(100)
        .with_for_update(skip_locked=True)
    )
    from sqlalchemy import tuple_

    db.execute(
        delete(CatalogProofNonce).where(
            tuple_(CatalogProofNonce.issuer, CatalogProofNonce.nonce).in_(stale)
        )
    )
    now = int(time.time())
    token = Delegation(
        purpose=TOKEN_PURPOSE,
        audience=AUDIENCE,
        issuer=claim.issuer,
        client_id=claim.client_id,
        subject=claim.subject,
        user_id=user.id,
        authorization_epoch=user.token_version,
        mapping_version=mapping.version,
        client_policy_version=client_policy_version(),
        issued_at=now,
        expires_at=now + 60,
        binding=claim.binding,
    )
    return {
        "token": _encode(token, authority),
        "expires_at": token.expires_at,
        "token_type": "PN-Delegation",
    }


def _translate(exc):
    code = {
        401: "auth.session_revoked",
        403: "auth.forbidden",
        503: "auth.unavailable",
    }.get(exc.status_code, "auth.unavailable")
    raise CatalogError(
        code,
        "当前账号未获得此操作权限" if exc.status_code == 403 else "当前授权不可用",
        status_code=exc.status_code,
        retryable=exc.status_code == 503,
    ) from None


def _identity(user, epoch):
    # Only called after verified transport or trusted persisted actor is rechecked.
    return VerifiedIdentity(
        user.username, epoch, False, "sys_user", MappingProxyType({})
    )


def _recheck_actor(db, actor):
    if (
        type(actor) is not ActorContext
        or actor.authorization_epoch is None
        or not actor.principal_id.startswith("account:")
    ):
        _fail("auth.session_revoked", "后台任务缺少可信账号与授权版本")
    try:
        user_id = int(actor.principal_id.removeprefix("account:"))
    except ValueError:
        _fail()
    if actor.principal_id != f"account:{user_id}" or user_id < 1:
        _fail()
    user = _current_user(db, user_id, actor.authorization_epoch)
    if actor.client_id == "itsp":
        if (
            actor.issuer != "itsp"
            or actor.subject != str(user_id)
            or actor.mapping_version is not None
        ):
            _fail()
    else:
        s = get_settings()
        if (
            actor.client_id != s.pn_shop_client_id
            or actor.issuer != s.pn_shop_issuer
            or actor.mapping_version is None
            or actor.client_policy_version != client_policy_version()
        ):
            _fail("auth.client_revoked", "客户端能力配置已变化")
        try:
            subject = UUID(actor.subject or "")
        except ValueError:
            _fail()
        _current_mapping(db, actor.issuer, subject, user_id, actor.mapping_version)
    return user


def _access(db, actor):
    _recheck_actor(db, actor)

    def check(session, context, action, part_id, *, job_resource=None):
        if context.actor != actor:
            _fail("auth.forbidden", "操作人与当前可信身份不一致", status=403)
        if action not in CATALOG_ACTIONS:
            _fail("auth.forbidden", "未注册的共享资料操作", status=403)
        if action in MIGRATION_ACTIONS and (
            actor.client_id != "itsp"
            or actor.entrypoint != "migration"
            or actor.provenance.kind != "migration"
        ):
            _fail("auth.forbidden", "历史迁移仅限本机可信迁移入口", status=403)
        user = _recheck_actor(session, actor)
        resource, registered_action = CATALOG_ACTIONS[action]
        if job_resource is not None and action in {
            "job.read",
            "job.cancel",
            "job.retry",
        }:
            resource = job_resource
        if (
            actor.client_id != "itsp"
            and action not in get_settings().pn_shop_allowed_actions
        ):
            _fail("auth.forbidden", "客户端未开放此操作", status=403)
        try:
            allowed = resource_access(
                session,
                _identity(user, actor.authorization_epoch),
                resource,
                registered_action,
                locked_user=user,
                object_id=part_id,
            )
        except HTTPException as exc:
            _translate(exc)
        fields = allowed.field_permissions
        if actor.client_id != "itsp":
            fields &= frozenset(get_settings().pn_shop_allowed_fields.get(resource, []))
        return AuthorizationDecision(
            readable_fields=fields,
            writable_fields=frozenset() if action in READ_ACTIONS else fields,
            metadata={
                "scope": allowed.scope,
                "grant_version": allowed.grant_version,
                "policy_version": CATALOG_VERSION,
                "authorization_epoch": allowed.authorization_epoch,
            },
        )

    def authorize(session, context, action, part_id):
        return check(session, context, action, part_id)

    def authorize_job(session, context, action, job):
        # B2 imports are deferred until after integration. The DB row, not a
        # caller-provided JobView, supplies the kind, owner, and identity closure.
        from app.pn_catalog.job_models import CatalogJob
        from app.pn_catalog import media
        from app.services import pn_catalog_enrichment, pn_catalog_identity

        row = session.scalar(
            select(CatalogJob)
            .where(CatalogJob.id == job.job_id)
            .execution_options(populate_existing=True)
        )
        if row is None:
            _fail("job.not_found", "任务不存在", status=404)
        if (job.kind, job.resource_type, job.resource_id, job.part_id) != (
            row.kind,
            row.resource_type,
            row.resource_id,
            row.part_id,
        ):
            _fail("auth.forbidden", "任务资源与当前数据库记录不一致", status=403)
        if row.context["actor"]["principal_id"] != actor.principal_id:
            _fail("auth.forbidden", "只能管理本人提交的任务", status=403)
        if row.kind.startswith("enrichment."):
            resource = (
                "part_master" if row.kind == "enrichment.research" else "part_gallery"
            )

            def auxiliary(s, c, a, pid):
                return check(s, c, a, pid, job_resource=resource)

            return pn_catalog_enrichment.authorize_job(
                session, context, action, job, authorize=auxiliary
            )
        if row.kind == "identity.recalculate":
            decision = check(
                session, context, action, row.part_id, job_resource="part_identity"
            )
            pn_catalog_identity._authorize_scope(
                session,
                row.payload["identity_revisions"],
                context,
                authorize,
                row.payload["operation"],
            )
            return decision
        decision = check(session, context, action, row.part_id)
        if row.kind in {
            "media.verify_upload",
            "media.complete_multipart",
            "media.abort_multipart",
            "media.thumbnail",
        }:
            media.authorize_job_resource(
                session, job, context=context, authorize=authorize
            )
        else:
            _fail("auth.forbidden", "未注册的任务类型", status=403)
        return decision

    secret = get_settings().pn_confirmation_secret.get_secret_value().encode()
    return CatalogAccess(
        actor=actor,
        authorize=authorize,
        confirmation_secret=secret if len(secret) >= 32 else None,
        authorize_job=authorize_job,
    )


def resolve_job_access(db: Session, actor: ActorContext) -> CatalogAccess:
    """Recheck original persisted epochs; never refresh a revoked job to a new epoch."""
    return _access(db, actor)


def resolve_native_access(db: Session, identity: VerifiedIdentity) -> CatalogAccess:
    try:
        principal = load_principal(db, identity)
        if principal is None:
            _fail("auth.forbidden", "共享资料需要实名账号", status=403)
        user = _current_user(db, principal.id, principal.authorization_epoch)
    except HTTPException as exc:
        _translate(exc)
    actor = ActorContext(
        principal_id=f"account:{user.id}",
        actor_kind="human",
        client_id="itsp",
        executor_id="itsp",
        entrypoint="itsp_ui",
        display_name=user.display_name,
        issuer="itsp",
        subject=str(user.id),
        authorization_epoch=user.token_version,
    )
    return _access(db, actor)


def resolve_delegated_access(
    db: Session, token: str, binding: RequestBinding
) -> CatalogAccess:
    authority, _ = _keys()
    claim = _decode(token, authority, Delegation, 60)
    if claim.binding != binding:
        _fail("auth.request_binding_mismatch", "委托与当前请求不一致")
    actor = ActorContext(
        principal_id=f"account:{claim.user_id}",
        actor_kind="human",
        client_id=claim.client_id,
        executor_id="shop",
        entrypoint="shop_ui",
        issuer=claim.issuer,
        subject=str(claim.subject),
        authorization_epoch=claim.authorization_epoch,
        mapping_version=claim.mapping_version,
        client_policy_version=claim.client_policy_version,
    )
    return _access(db, actor)


def resolve_catalog_access(request: Request, db: Session) -> CatalogAccess:
    if len(request.headers.getlist("authorization")) != 1:
        _fail("auth.session_revoked", "需要唯一的有效凭据")
    scheme, _, token = request.headers["authorization"].partition(" ")
    if scheme.lower() == "pn-delegation":
        digest = getattr(request.state, "pn_catalog_body_sha256", None)
        if digest is None:
            _fail("auth.unavailable", "请求签名校验入口尚未安装", status=503)
        try:
            request_id = UUID(
                request.headers.get("idempotency-key")
                or request.headers["x-request-id"]
            )
            target = request.scope.get("raw_path", request.url.path.encode()).decode(
                "ascii"
            )
            if query := request.scope.get("query_string", b""):
                target += "?" + query.decode("ascii")
            binding = RequestBinding(
                method=request.method,
                target=target,
                body_sha256=digest,
                request_id=request_id,
                if_match=request.headers.get("if-match"),
            )
        except (ValueError, KeyError, UnicodeDecodeError):
            _fail("auth.request_binding_mismatch", "请求签名参数不完整")
        return resolve_delegated_access(db, token, binding)
    if scheme.lower() != "bearer":
        _fail()
    try:
        identity = _identity_from_verified_claims(verify_token_db(token, db))
    except HTTPException as exc:
        _translate(exc)
    return resolve_native_access(db, identity)


def set_subject_binding(
    db: Session,
    identity: VerifiedIdentity,
    *,
    subject: UUID,
    user_id: int,
    active: bool,
    expected_version: int,
    reason: str,
) -> dict:
    """Canonical named-admin trust binding, CAS + audit in the caller transaction.

    Call without an earlier actor SHARE dependency: ordered user UPDATE locks
    must be the first user locks. No guessed or display-name based mappings.
    """
    from sqlalchemy import text
    from app.authz.principal import require_named_admin
    from app.models.system import SysAuditLog

    if (
        type(subject) is not UUID
        or type(user_id) is not int
        or user_id < 1
        or type(active) is not bool
        or type(expected_version) is not int
        or expected_version < 0
        or not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 2000
    ):
        _fail("request.validation_failed", "绑定参数或原因无效", status=422)
    issuer = get_settings().pn_shop_issuer
    try:
        actor = require_named_admin(load_principal(db, identity))
        key = int.from_bytes(
            hashlib.sha256(_json(["pn-subject", issuer, str(subject)])).digest()[:8],
            "big",
            signed=True,
        )
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        existing = db.get(
            CatalogSubjectBinding, (issuer, subject), populate_existing=True
        )
        ids = sorted({actor.id, user_id, *([existing.user_id] if existing else [])})
        users = {
            u.id: u
            for u in db.scalars(
                select(SysUser)
                .where(SysUser.id.in_(ids))
                .order_by(SysUser.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        if actor.id not in users:
            _fail()
        require_named_admin(load_principal(db, identity, locked_user=users[actor.id]))
    except HTTPException as exc:
        _translate(exc)
    target = users.get(user_id)
    if target is None or not target.is_active or target.deleted_at is not None:
        _fail("request.validation_failed", "只能绑定有效实名账号", status=422)
    existing = db.scalar(
        select(CatalogSubjectBinding)
        .where(
            CatalogSubjectBinding.issuer == issuer,
            CatalogSubjectBinding.subject == subject,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (existing.version if existing else 0) != expected_version:
        _fail("auth.mapping_conflict", "映射版本已变化", status=409)
    before = (
        None
        if existing is None
        else {
            "user_id": existing.user_id,
            "active": existing.active,
            "version": existing.version,
        }
    )
    if existing is None:
        existing = CatalogSubjectBinding(
            issuer=issuer,
            subject=subject,
            user_id=user_id,
            active=active,
            version=1,
            updated_by=actor.id,
        )
        db.add(existing)
    else:
        existing.user_id, existing.active = user_id, active
        existing.version += 1
        existing.updated_by = actor.id
        existing.updated_at = datetime.now(timezone.utc)
    after = {
        "issuer": issuer,
        "subject": str(subject),
        "user_id": user_id,
        "active": active,
        "version": existing.version,
    }
    db.add(
        SysAuditLog(
            entity_type="catalog_subject_binding",
            entity_id=user_id,
            action="set_binding",
            before_json=before,
            after_json=after,
            reason=reason.strip(),
            operated_by=f"account:{actor.id}",
        )
    )
    db.flush()
    return after


def resolve_migration_access(db: Session, identity: VerifiedIdentity) -> CatalogAccess:
    """Local verified operator; no public HTTP switch can manufacture this actor."""
    from app.pn_catalog.dto import Provenance

    native = resolve_native_access(db, identity)
    actor = native.actor.model_copy(
        update={"entrypoint": "migration", "provenance": Provenance(kind="migration")}
    )
    return _access(db, actor)


def get_part_access(db: Session, actor: ActorContext, part_id: int | None) -> dict:
    """Current grant capabilities, using the existing framework's batched PDP.

    Current resources support all-PN scope. This does not claim ownership of an
    inbox/upload/job, or replace a mutation's baseline and state checks.
    """
    from app.authz.service import my_snapshot
    from app.models.dimensions import DimPart
    from app.pn_catalog.dto import PROFILE_FIELDS

    user = _recheck_actor(db, actor)
    try:
        snapshot = my_snapshot(db, _identity(user, actor.authorization_epoch))
    except HTTPException as exc:
        _translate(exc)
    allowed, resource_fields = set(), {}
    for action, (resource, registered) in CATALOG_ACTIONS.items():
        if action in MIGRATION_ACTIONS:
            continue
        if (
            actor.client_id != "itsp"
            and action not in get_settings().pn_shop_allowed_actions
        ):
            continue
        item = snapshot["resources"][resource]
        if (
            item["runtime_state"] != "enabled"
            or item["scope_kind"] != "all"
            or item["decisions"][registered]["effect"] != "allow"
        ):
            continue
        allowed.add(action)
        fields = set(item["field_permissions"])
        if actor.client_id != "itsp":
            fields &= set(get_settings().pn_shop_allowed_fields.get(resource, []))
        resource_fields[resource] = fields - {"legacy_evidence"}
    if not allowed & {"read", "read_content", "gallery.read", "identity.read"}:
        _fail("auth.forbidden", "没有读取此型号资料的权限", status=403)
    if part_id is not None and db.scalar(select(DimPart.id).where(DimPart.id == part_id)) is None:
        _fail("catalog.part_not_found", "型号不存在", status=404)
    readable, writable = set(), set()
    master = resource_fields.get("part_master", set())
    profile = set(PROFILE_FIELDS) | {f for f in master if f.startswith("specs.")}
    if "read" in allowed:
        readable |= master & (profile | {"availability", "fill_status"})
    if "read_attributes" in allowed:
        readable |= master & {"attributes"}
    if "update_attributes" in allowed:
        writable |= master & {"attributes"}
    if "read_candidates" in allowed:
        readable |= master & {"candidates"}
    if allowed & {"update_profile", "create"}:
        writable |= master & profile
    if "set_availability" in allowed:
        writable |= master & {"availability"}
    if allowed & {"create_candidate", "adopt_candidate", "reject_candidate"}:
        writable |= master & {"candidates"}
    if "read_content" in allowed:
        readable |= resource_fields.get("part_content", set())
    if "update_content" in allowed:
        writable |= resource_fields.get("part_content", set())
    if "identity.read" in allowed:
        readable |= resource_fields.get("part_identity", set())
    if any(
        action.startswith("identity.") and action != "identity.read"
        for action in allowed
    ):
        writable |= resource_fields.get("part_identity", set())
    gallery = resource_fields.get("part_gallery", set())
    for field in ("gallery", "uploads", "inbox"):
        if field + ".read" in allowed:
            readable |= gallery & {field}
        if any(a.startswith(field + ".") and a not in READ_ACTIONS for a in allowed):
            writable |= gallery & {field}
    return {
        "part_id": part_id,
        "allowed_actions": sorted(allowed),
        "readable_fields": sorted(readable),
        "writable_fields": sorted(writable & readable),
    }
