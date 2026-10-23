"""Authority integration with actual PostgreSQL and pinned Cerbos."""

import base64
import hashlib
import hmac
import json
import time
from types import MappingProxyType
from uuid import uuid4
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.auth import _make_token
from app.authz import runtime
from app.authz.catalog import RESOURCE_DEFINITIONS, validate_catalog
from app.authz.pn_models import CatalogProofNonce, CatalogSubjectBinding
from app.authz.pn_resources import CATALOG_ACTIONS
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.models.system import SysAuditLog, SysUser
from app.pn_catalog.dto import CommandContext, ActorContext
from app.pn_catalog.errors import CatalogError
from app.services import pn_catalog_access as access
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def authority(db, monkeypatch, cerbos_url):  # noqa: F811
    s = get_settings()
    monkeypatch.setattr(s, "authorization_mode", "unified_modules")
    monkeypatch.setattr(
        s,
        "authz_enabled_resources",
        ["part_master", "part_identity", "part_content", "part_gallery"],
    )
    monkeypatch.setattr(s, "cerbos_url", cerbos_url)
    from pydantic import SecretStr

    monkeypatch.setattr(
        s, "pn_delegation_secret", SecretStr("authority-test-secret-" * 3)
    )
    monkeypatch.setattr(
        s, "pn_shop_proof_secret", SecretStr("shop-session-test-secret-" * 3)
    )
    monkeypatch.setattr(
        s,
        "pn_shop_allowed_actions",
        sorted(set(CATALOG_ACTIONS) - access.MIGRATION_ACTIONS),
    )
    monkeypatch.setattr(
        s,
        "pn_shop_allowed_fields",
        {
            r: sorted(RESOURCE_DEFINITIONS[r].field_permissions - {"legacy_evidence"})
            for r in s.authz_enabled_resources
        },
    )
    runtime.activate_configured_resources(db.get_bind())
    user = SysUser(
        username="pn-authority-user",
        password_hash="unused",
        role="readonly",
        token_version=4,
    )
    admin = SysUser(
        username="pn-authority-admin",
        password_hash="unused",
        role="admin",
        token_version=2,
    )
    db.add_all([user, admin])
    db.flush()
    for r in s.authz_enabled_resources:
        d = RESOURCE_DEFINITIONS[r]
        db.add(
            SysAuthorizationGrant(
                user_id=user.id,
                resource=r,
                actions=sorted(d.actions),
                scope="all",
                field_permissions=sorted(d.field_permissions),
                version=1,
            )
        )
    subject = uuid4()
    db.add(
        CatalogSubjectBinding(
            issuer="shop",
            subject=subject,
            user_id=user.id,
            active=True,
            version=1,
            updated_by=admin.id,
        )
    )
    db.commit()
    return user, admin, subject


def identity(user):
    return VerifiedIdentity(
        user.username, user.token_version, False, "sys_user", MappingProxyType({})
    )


def binding():
    return access.RequestBinding(
        method="PATCH",
        target="/api/pn-catalog/v1/parts/1/profile",
        body_sha256=hashlib.sha256(b'{"description":"new"}').hexdigest(),
        request_id=uuid4(),
        if_match='"1"',
    )


def proof(mapped_subject, target, **changes):
    now = int(time.time())
    data = {
        "purpose": access.PROOF_PURPOSE,
        "audience": access.AUDIENCE,
        "issuer": "shop",
        "client_id": "shop",
        "subject": str(mapped_subject),
        "session_id": str(uuid4()),
        "nonce": str(uuid4()),
        "issued_at": now,
        "expires_at": now + 30,
        "binding": target.model_dump(mode="json"),
    }
    data.update(changes)
    body = json.dumps(data, separators=(",", ":")).encode()

    def b64(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    signature = hmac.new(
        get_settings().pn_shop_proof_secret.get_secret_value().encode(),
        body,
        hashlib.sha256,
    ).digest()
    return b64(body) + "." + b64(signature)


def delegated(db, authority):
    target = binding()
    result = access.exchange_delegation(db, proof(authority[2], target))
    db.commit()
    return (
        access.resolve_delegated_access(db, result["token"], target),
        result["token"],
        target,
    )


def call(db, resolved, action="update_profile"):
    return resolved.authorize(
        db, CommandContext(actor=resolved.actor, request_id=uuid4()), action, 1
    )


def test_four_closed_resources_use_actual_grant_and_pdp(db, authority):
    validate_catalog()
    resolved = access.resolve_native_access(db, identity(authority[0]))
    assert resolved.actor.principal_id == f"account:{authority[0].id}"
    for action in set(CATALOG_ACTIONS) - access.MIGRATION_ACTIONS:
        decision = call(db, resolved, action)
        assert decision.metadata["grant_version"] == 1
        assert decision.metadata["scope"] == "all"
    assert "description" in call(db, resolved).writable_fields
    assert call(db, resolved, "read").writable_fields == frozenset()


def test_named_admin_has_no_implicit_business_permission(db, authority):
    resolved = access.resolve_native_access(db, identity(authority[1]))
    with pytest.raises(CatalogError) as e:
        call(db, resolved)
    assert e.value.status_code == 403


def test_current_fields_and_client_capability_intersection(db, authority, monkeypatch):
    row = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == authority[0].id,
            SysAuthorizationGrant.resource == "part_master",
        )
    )
    row.field_permissions = ["description"]
    row.actions = [a for a in row.actions if a not in access.MIGRATION_ACTIONS]
    db.commit()
    resolved, _, _ = delegated(db, authority)
    assert call(db, resolved).writable_fields == frozenset({"description"})
    db.rollback()
    monkeypatch.setattr(get_settings(), "pn_shop_allowed_actions", ["read"])
    with pytest.raises(CatalogError) as e:
        call(db, resolved)
    assert e.value.code == "auth.client_revoked"
    resolved, _, _ = delegated(db, authority)
    with pytest.raises(CatalogError) as e:
        call(db, resolved)
    assert e.value.status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"purpose": "normal-login"},
        {"audience": "other"},
        {"subject": str(uuid4())},
        {"issued_at": 0, "expires_at": 1},
        {"role": "admin"},
        {"issuer": "untrusted"},
        {"client_id": "itsp"},
        {"issued_at": True},
    ],
)
def test_untrusted_proof_never_mints_token(db, authority, changes):
    with pytest.raises(CatalogError):
        access.exchange_delegation(db, proof(authority[2], binding(), **changes))
    assert db.scalar(select(func.count()).select_from(CatalogProofNonce)) == 0


@pytest.mark.parametrize("offset,ttl", [(1, 30), (0, 31), (-31, 30)])
def test_proof_time_boundaries(db, authority, offset, ttl):
    now = int(time.time()) + offset
    with pytest.raises(CatalogError):
        access.exchange_delegation(
            db, proof(authority[2], binding(), issued_at=now, expires_at=now + ttl)
        )


def test_proof_single_use_and_rollback(db, authority):
    signed = proof(authority[2], binding())
    access.exchange_delegation(db, signed)
    db.rollback()
    access.exchange_delegation(db, signed)
    db.commit()
    with pytest.raises(CatalogError) as e:
        access.exchange_delegation(db, signed)
    assert e.value.code == "auth.proof_replayed"


@pytest.mark.parametrize(
    "changed", ["method", "target", "body_sha256", "request_id", "if_match"]
)
def test_delegation_binds_every_request_component(db, authority, changed):
    _, token, target = delegated(db, authority)
    values = {
        "method": "DELETE",
        "target": "/api/pn-catalog/v1/parts/2/profile",
        "body_sha256": "0" * 64,
        "request_id": uuid4(),
        "if_match": '"2"',
    }
    with pytest.raises(CatalogError) as e:
        access.resolve_delegated_access(
            db, token, target.model_copy(update={changed: values[changed]})
        )
    assert e.value.code == "auth.request_binding_mismatch"


@pytest.mark.parametrize(
    "mutation", ["epoch", "inactive", "mapping", "mapping_off", "grant"]
)
def test_current_revocation_rejects_token_and_persisted_job(db, authority, mutation):
    resolved, token, target = delegated(db, authority)
    saved = resolved.actor.model_dump(mode="json")
    db.rollback()
    if mutation == "epoch":
        authority[0].token_version += 1
    elif mutation == "inactive":
        authority[0].is_active = False
    elif mutation.startswith("mapping"):
        row = db.get(CatalogSubjectBinding, ("shop", authority[2]))
        row.version += 1
        if mutation == "mapping_off":
            row.active = False
    else:
        row = db.scalar(
            select(SysAuthorizationGrant).where(
                SysAuthorizationGrant.user_id == authority[0].id,
                SysAuthorizationGrant.resource == "part_master",
            )
        )
        row.actions = []
        row.version += 1
    db.commit()
    with pytest.raises(CatalogError):
        call(db, access.resolve_delegated_access(db, token, target))
    with pytest.raises(CatalogError):
        call(db, access.resolve_job_access(db, ActorContext.model_validate(saved)))


def test_normal_login_token_cannot_be_delegation_and_reverse(db, authority):
    token, _ = _make_token(
        "admin", authority[0].username, None, token_version=4, authn="sys_user"
    )
    with pytest.raises(CatalogError):
        access.resolve_delegated_access(db, token, binding())
    _, delegation, _ = delegated(db, authority)
    from app.auth import verify_token
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        verify_token(delegation)


def test_binding_command_requires_named_admin_and_rolls_back_audit(db, authority):
    user, admin, subject = authority
    with pytest.raises(CatalogError):
        access.set_subject_binding(
            db,
            identity(user),
            subject=subject,
            user_id=user.id,
            active=False,
            expected_version=1,
            reason="revoke",
        )
    result = access.set_subject_binding(
        db,
        identity(admin),
        subject=subject,
        user_id=user.id,
        active=False,
        expected_version=1,
        reason="revoke",
    )
    assert result["version"] == 2
    assert (
        db.scalar(
            select(func.count())
            .select_from(SysAuditLog)
            .where(SysAuditLog.entity_type == "catalog_subject_binding")
        )
        == 1
    )
    db.rollback()
    assert db.get(CatalogSubjectBinding, ("shop", subject)).active
    assert (
        db.scalar(
            select(func.count())
            .select_from(SysAuditLog)
            .where(SysAuditLog.entity_type == "catalog_subject_binding")
        )
        == 0
    )
    with pytest.raises(CatalogError) as e:
        access.set_subject_binding(
            db,
            identity(admin),
            subject=subject,
            user_id=user.id,
            active=False,
            expected_version=0,
            reason="stale",
        )
    assert e.value.code == "auth.mapping_conflict"


def test_resource_suspension_never_falls_back_to_legacy(db, authority, monkeypatch):
    resolved = access.resolve_native_access(db, identity(authority[0]))
    monkeypatch.setattr(get_settings(), "authz_enabled_resources", [])
    with pytest.raises(CatalogError) as e:
        call(db, resolved)
    assert e.value.status_code == 503


def test_job_without_original_epoch_cannot_invent_current_epoch(db, authority):
    resolved = access.resolve_native_access(db, identity(authority[0]))
    with pytest.raises(CatalogError):
        access.resolve_job_access(
            db, resolved.actor.model_copy(update={"authorization_epoch": None})
        )


def test_exchange_nonce_race_only_one_transaction_wins(db, authority):
    from concurrent.futures import ThreadPoolExecutor

    signed = proof(authority[2], binding())
    engine = db.get_bind()
    db.rollback()

    def exchange():
        with Session(engine) as session:
            try:
                access.exchange_delegation(session, signed)
                session.commit()
                return "minted"
            except CatalogError as exc:
                session.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: exchange(), range(2))) == [
            "auth.proof_replayed",
            "minted",
        ]


def test_facade_list_and_write_receipt_recheck_real_authority(db, authority):
    from app.models.dimensions import DimPart
    from app.pn_catalog import api
    from app.pn_catalog.dto import PatchProfile

    part = DimPart(pn_std="AUTHORITY-LIST-PN", description="before")
    db.add(part)
    db.commit()
    resolved = access.resolve_native_access(db, identity(authority[0]))
    ctx = CommandContext(actor=resolved.actor, request_id=uuid4())
    command = PatchProfile(
        expected_identity_revision=0,
        field_versions={"description": 0},
        changes={"description": "after"},
    )
    result = api.patch_profile(
        db, part.id, command, context=ctx, authorize=resolved.authorize
    )
    db.commit()
    listed = api.list_parts(db, context=ctx, authorize=resolved.authorize)
    assert listed["data"][0]["part_id"] == part.id
    replay = api.patch_profile(
        db, part.id, command, context=ctx, authorize=resolved.authorize
    )
    assert replay["meta"]["audit_id"] == result["meta"]["audit_id"]
    assert replay["meta"]["replayed"]
    db.rollback()
    row = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == authority[0].id,
            SysAuthorizationGrant.resource == "part_master",
        )
    )
    row.actions = ["read"]
    row.version += 1
    db.commit()
    with pytest.raises(CatalogError):
        api.get_receipt(db, ctx.request_id, context=ctx, authorize=resolved.authorize)


def test_revocation_waits_for_authorized_transaction_then_blocks_new_job(db, authority):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    resolved = access.resolve_native_access(db, identity(authority[0]))
    call(db, resolved)
    engine = db.get_bind()
    user_id = authority[0].id
    started = Event()
    completed = Event()

    def revoke():
        with Session(engine) as session:
            started.set()
            user = session.scalar(
                select(SysUser).where(SysUser.id == user_id).with_for_update()
            )
            user.token_version += 1
            session.commit()
            completed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(revoke)
        assert started.wait(2)
        assert not completed.wait(0.15)
        db.commit()
        future.result(timeout=5)
    with pytest.raises(CatalogError):
        access.resolve_job_access(db, resolved.actor)


def test_mapping_delete_cannot_reset_version_and_revive_old_token(db, authority):
    from sqlalchemy import delete
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        db.execute(
            delete(CatalogSubjectBinding).where(
                CatalogSubjectBinding.subject == authority[2]
            )
        )
    db.rollback()
    assert db.get(CatalogSubjectBinding, ("shop", authority[2])).version == 1


def test_legacy_evidence_requires_explicit_grant_and_local_migration_actor(
    db, authority
):
    from app.authz.catalog import validate_grant

    with pytest.raises(ValueError, match="legacy_evidence"):
        validate_grant("part_master", ["read", "read_legacy"], "all", ["description"])
    native = access.resolve_native_access(db, identity(authority[0]))
    for action in access.MIGRATION_ACTIONS:
        with pytest.raises(CatalogError) as exc:
            call(db, native, action)
        assert exc.value.status_code == 403
    local = access.resolve_migration_access(db, identity(authority[0]))
    assert local.actor.authorization_epoch == native.actor.authorization_epoch
    assert local.actor.principal_id == native.actor.principal_id
    for action in ["stage_migration", "materialize_migration", "read_legacy"]:
        decision = call(db, local, action)
        assert "legacy_evidence" in decision.readable_fields
        assert "*" not in decision.readable_fields
        assert decision.metadata["scope"] == "all"
    assert not call(db, local, "read_legacy").writable_fields
    row = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == authority[0].id,
            SysAuthorizationGrant.resource == "part_master",
        )
    )
    row.actions = ["read", "edit"]
    row.field_permissions = ["description"]
    row.version += 1
    db.commit()
    with pytest.raises(CatalogError):
        call(db, local, "read_legacy")


def test_shop_cannot_open_legacy_evidence_even_if_cap_misconfigured(
    db, authority, monkeypatch
):
    monkeypatch.setattr(
        get_settings(), "pn_shop_allowed_actions", ["read", "read_legacy"]
    )
    with pytest.raises(CatalogError) as exc:
        access.exchange_delegation(db, proof(authority[2], binding()))
    assert exc.value.status_code == 503
