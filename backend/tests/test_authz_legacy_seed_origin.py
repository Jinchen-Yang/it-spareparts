"""Persistent origin is distinct from an empty canonical authorization choice."""
from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api import accounts, authorization, role_templates
from app.auth import _make_token
from app.authz import catalog, management, runtime
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.models.system import SysRoleTemplate, SysUser
from app.seed_users import _upsert


ORIGIN = "authorization_legacy_seed_eligible"
DENY = {"actions": [], "scope": "assigned_customers", "field_permissions": []}


def _body(**values):
    return {"schema_version": 1, "catalog_version": catalog.CATALOG_VERSION, **values}


@pytest.fixture
def admin(db):
    user = SysUser(username="origin_operator", role="admin", password_hash="unused")
    db.add(user)
    db.commit()
    token, _ = _make_token("admin", user.username, None, authn="sys_user", token_version=0)
    return {"Authorization": "Bearer " + token}


@pytest.fixture(params=["unified_modules"])
def origin_client(request, db, monkeypatch):
    settings = get_settings()
    unified = request.param == "unified_modules"
    monkeypatch.setattr(settings, "authorization_mode", request.param)
    monkeypatch.setattr(settings, "authz_enabled_resources", [])
    monkeypatch.setattr(settings, "authz_suspended_resources", [])
    monkeypatch.setattr(settings, "authz_sales_analysis_enabled", unified)
    monkeypatch.setattr(settings, "authz_sales_analysis_suspended", unified)
    if unified:
        runtime.activate_configured_resources(db.get_bind())
    app = FastAPI()
    for router in (accounts.router, role_templates.router, authorization.router):
        app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        yield client


def _ok(response, status=201):
    assert response.status_code == status, response.text
    assert ORIGIN not in response.text
    return response.json()


def _template(db, modules):
    template = SysRoleTemplate(
        code="legacy_origin_source", name="旧来源模板", base_role="readonly",
        permissions={"page_sales_analysis": True}, authorization_bundle=deepcopy(modules),
        authorization_legacy_seed_eligible=True,
    )
    db.add(template)
    db.commit()
    return template


def test_database_and_orm_defaults_are_false_and_sql_null_is_rejected(db):
    user = SysUser(username="orm_default", role="readonly", password_hash="unused")
    template = SysRoleTemplate(code="orm_default", name="ORM", base_role="readonly", permissions={})
    db.add_all([user, template])
    db.flush()
    assert user.authorization_legacy_seed_eligible is False
    assert template.authorization_legacy_seed_eligible is False
    db.execute(text("INSERT INTO sys_user (username,role,password_hash) VALUES ('sql_default','readonly','unused')"))
    db.execute(text("INSERT INTO sys_role_template (code,name,base_role,permissions) VALUES ('sql_default','SQL','readonly','{}'::jsonb)"))
    for table, key in (("sys_user", "username"), ("sys_role_template", "code")):
        value = db.execute(text(f"SELECT {ORIGIN} FROM {table} WHERE {key}='sql_default'")).scalar_one()
        assert value is False
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                db.execute(text(f"UPDATE {table} SET {ORIGIN}=NULL WHERE {key}='sql_default'"))
    db.commit()


@pytest.mark.parametrize("kind,modules", [
    ("none", {}), ("explicit", {}), ("apply_template", {}),
    ("explicit", {"sales_analysis": DENY}), ("apply_template", {"sales_analysis": DENY}),
])
def test_canonical_account_empty_and_explicit_deny_never_inherit_origin(db, origin_client, admin, kind, modules):
    template = _template(db, modules)
    spec = {"kind": kind}
    if kind == "explicit":
        spec["modules"] = modules
    elif kind == "apply_template":
        spec.update(template_code=template.code, expected_template_version=1)
    _ok(origin_client.post("/api/accounts", headers=admin, json=_body(
        profile={"username": "canonical_created", "password": "synthetic_password"}, authorization=spec)))
    user = db.scalar(select(SysUser).where(SysUser.username == "canonical_created"))
    assert user.authorization_legacy_seed_eligible is False
    profile = user.authorization_profile
    if kind == "apply_template":
        assert profile["template_snapshot"] == {"code": template.code, "version": 1, "modules": modules}
        assert profile["resource_overrides"] == {}
    else:
        assert profile["template_snapshot"] is None
        assert profile["resource_overrides"] == modules
    grants = list(db.scalars(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == user.id)))
    assert len(grants) == len(modules)
    if grants:
        assert grants[0].actions == []
    db.refresh(template)
    assert template.authorization_legacy_seed_eligible is True
    assert template.authorization_bundle == modules


@pytest.mark.parametrize("copy,modules", [
    (False, {}), (True, {}), (True, {"sales_analysis": DENY}),
])
def test_canonical_template_creation_and_copy_preserve_bundle_but_not_origin(db, origin_client, admin, copy, modules):
    source = _template(db, modules)
    spec = ({"kind": "copy_template", "code": source.code, "expected_version": 1}
            if copy else {"kind": "explicit", "modules": modules})
    _ok(origin_client.post("/api/role-templates", headers=admin, json=_body(
        metadata={"code": "canonical_copy", "name": "明确配置"}, authorization=spec)))
    created = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == "canonical_copy"))
    assert created.authorization_legacy_seed_eligible is False
    assert created.authorization_bundle == modules
    db.refresh(source)
    assert source.authorization_legacy_seed_eligible is True
    assert source.authorization_bundle == modules and source.version == 1


@pytest.mark.parametrize("origin_client", ["legacy_compat"], indirect=True)
@pytest.mark.parametrize("role", ["readonly", "admin"])
def test_legacy_account_creation_records_legacy_origin(db, origin_client, admin, role):
    _ok(origin_client.post("/api/accounts", headers=admin, json={
        "username": "legacy_created", "password": "synthetic_password", "role": role,
    }))
    user = db.scalar(select(SysUser).where(SysUser.username == "legacy_created"))
    assert user.authorization_legacy_seed_eligible is True
    assert user.authorization_profile == management.empty_profile()
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 0


@pytest.mark.parametrize("origin_client", ["legacy_compat"], indirect=True)
@pytest.mark.parametrize("copy", [False, True])
def test_legacy_template_creation_and_copy_record_legacy_origin(db, origin_client, admin, copy):
    source = SysRoleTemplate(code="canonical_source", name="来源", base_role="readonly", permissions={})
    db.add(source)
    db.commit()
    body = {"code": "legacy_created", "name": "旧协议新模板", "base_role": "readonly"}
    if copy:
        body["copy_from"] = source.code
    _ok(origin_client.post("/api/role-templates", headers=admin, json=body))
    created = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == "legacy_created"))
    assert created.authorization_legacy_seed_eligible is True
    assert created.authorization_bundle == {}
    db.refresh(source)
    assert source.authorization_legacy_seed_eligible is False


def test_legacy_bootstrap_marks_only_created_accounts_and_is_idempotent(db):
    profile = {**management.empty_profile(), "resource_overrides": {"sales_analysis": DENY}}
    existing = SysUser(username="existing_canonical", role="readonly", password_hash="untouched",
                       authorization_profile=deepcopy(profile))
    db.add(existing)
    db.commit()
    assert _upsert(db, "existing_canonical", "admin", "must not replace", "must not replace", "synthetic_password") == "exists"
    assert _upsert(db, "legacy_seeded", "sales", "旧初始化", "合成销售", "synthetic_password") == "created"
    db.commit()
    assert _upsert(db, "legacy_seeded", "readonly", "must not replace", None, "different_password") == "exists"
    db.commit()
    db.refresh(existing)
    assert existing.authorization_legacy_seed_eligible is False
    assert existing.authorization_profile == profile and existing.role == "readonly"
    assert existing.password_hash == "untouched" and existing.token_version == 0
    created = db.scalar(select(SysUser).where(SysUser.username == "legacy_seeded"))
    assert created.authorization_legacy_seed_eligible is True and created.role == "sales"
    assert created.authorization_profile == management.empty_profile()
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 0


@pytest.mark.parametrize("eligible", [False, True])
def test_canonical_template_and_profile_updates_preserve_origin_and_saved_snapshot(db, origin_client, admin, eligible):
    template = _template(db, {"sales_analysis": DENY})
    template.authorization_legacy_seed_eligible = eligible
    saved = {"schema_version": 1, "template_snapshot": {"code": template.code, "version": 1, "modules": {}}, "resource_overrides": {}}
    user = SysUser(username="saved_snapshot", role="readonly", password_hash="unused",
                   authorization_profile=deepcopy(saved), authorization_legacy_seed_eligible=eligible)
    db.add(user)
    db.commit()
    _ok(origin_client.put(f"/api/role-templates/{template.code}", headers=admin, json=_body(
        expected_version=1, metadata={"name": "模板新名"})), 200)
    _ok(origin_client.put(f"/api/accounts/{user.username}", headers=admin, json=_body(
        profile={"display_name": "新显示名"})), 200)
    db.refresh(template)
    db.refresh(user)
    assert template.authorization_legacy_seed_eligible is eligible
    assert user.authorization_legacy_seed_eligible is eligible
    assert template.authorization_bundle == {"sales_analysis": DENY}
    assert user.authorization_profile == saved and user.token_version == 0
