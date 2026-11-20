"""通过权限中心真实写入口验证仓单分析的授权、模板快照与会话撤销。"""

from datetime import date, datetime

import pytest
from app.main import app
from app.models.warehouse_analytics import WarehouseAnalyticsDocument
from fastapi.testclient import TestClient

from tests.boss_board_helpers import client_for

KEY = "page_inventory_analytics"
PASSWORD = "synthetic-warehouse-account"


@pytest.fixture
def accounts(db):
    db.add(
        WarehouseAnalyticsDocument(
            source="inbound",
            source_id="seed",
            number="fixture-1",
            status="已生效",
            business_date=date(2026, 1, 2),
            source_modified_at=datetime(2026, 1, 3),
        )
    )
    db.commit()
    return client_for(db, username="admin", role="admin")


def login(username, expected):
    client = TestClient(app)
    reply = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["permissions"][KEY] is expected
    client.headers["Authorization"] = f"Bearer {reply.json()['token']}"
    return client


def check_reads(client, status):
    for path in (
        "coverage",
        "cube",
        "rankings",
        "documents",
        "equipment",
        "testing",
        "documents/1/lines",
    ):
        response = client.get(f"/api/inventory/analytics/{path}")
        assert response.status_code == status, (path, response.text)


@pytest.mark.parametrize(("role", "initial"), [("sales", False), ("admin", True)])
def test_save_account_permissions_and_revoke_sessions(accounts, role, initial):
    username = f"warehouse-{role}-reader"
    meta = accounts.get("/api/accounts/_meta").json()
    assert KEY in meta["meta"]
    create = accounts.post(
        "/api/accounts", json={"username": username, "password": PASSWORD, "role": role}
    )
    assert create.status_code == 201, create.text
    assert create.json()["permissions"][KEY] is initial
    initial_session = login(username, initial)
    check_reads(initial_session, 200 if initial else 403)
    values = (False, True, False) if initial else (True, False)
    for enabled in values:
        reply = accounts.put(
            f"/api/accounts/{username}", json={"overrides": {KEY: enabled}}
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["permissions"][KEY] is enabled
        check_reads(initial_session, 401)
        initial_session = login(username, enabled)
        check_reads(initial_session, 200 if enabled else 403)
    assert (
        accounts.put(
            "/api/accounts/admin", json={"overrides": {KEY: False}}
        ).status_code
        == 400
    )


def test_save_template_then_sync_to_warehouse_account(accounts):
    tpl = accounts.post(
        "/api/role-templates",
        json={
            "name": "仓储验收岗位",
            "base_role": "readonly",
            "copy_from": "readonly",
        },
    )
    assert tpl.status_code == 201, tpl.text
    template = tpl.json()
    username = "warehouse-template-reader"
    create = accounts.post(
        "/api/accounts",
        json={
            "username": username,
            "password": PASSWORD,
            "template_code": template["code"],
        },
    )
    assert create.status_code == 201, create.text
    session = login(username, False)
    url = "/api/role-templates/" + template["code"]
    for enabled in (True, False):
        reply = accounts.put(
            url,
            json={
                "version": template["version"],
                "permissions": {**template["permissions"], KEY: enabled},
            },
        )
        assert reply.status_code == 200, reply.text
        template = reply.json()
        assert template["permissions"][KEY] is enabled
        # 单独保存职位模板后，已套用的账号继续使用原快照。
        check_reads(session, 403 if enabled else 200)
        sync = accounts.post(url + "/sync", json={"dry_run": True})
        assert sync.status_code == 200, sync.text
        applied = accounts.post(
            url + "/sync",
            json={"dry_run": False, "fingerprint": sync.json()["fingerprint"]},
        )
        assert applied.status_code == 200, applied.text
        check_reads(session, 401)
        session = login(username, enabled)
        check_reads(session, 200 if enabled else 403)


def test_inventory_grant_input_requires_a_json_boolean(accounts):
    subject = {"username": "strict-warehouse", "password": PASSWORD, "role": "readonly"}
    created = accounts.post("/api/accounts", json=subject)
    assert created.status_code == 201
    initial = login(subject["username"], False)
    for invalid in ("false", "true", 0, 1, None, [], {}):
        graph = {KEY: invalid}
        for field in ("overrides", "permissions"):
            assert (
                accounts.post(
                    "/api/accounts", json={**subject, field: graph}
                ).status_code
                == 422
            )
            assert (
                accounts.put(
                    "/api/accounts/strict-warehouse", json={field: graph}
                ).status_code
                == 422
            )
        assert (
            accounts.post(
                "/api/role-templates",
                json={
                    "name": "invalid",
                    "copy_from": "readonly",
                    "permissions": graph,
                },
            ).status_code
            == 422
        )
        assert (
            accounts.put(
                "/api/role-templates/readonly",
                json={
                    "version": 1,
                    "permissions": graph,
                },
            ).status_code
            == 422
        )
        assert (
            accounts.post(
                "/api/accounts/bulk",
                json={
                    "usernames": [subject["username"]],
                    "operation": "grant",
                    "keys": [KEY],
                    "permissions": graph,
                },
            ).status_code
            == 422
        )
        assert (
            accounts.post(
                "/api/role-templates/readonly/sync", json={"permissions": graph}
            ).status_code
            == 422
        )
    # Failure does not invalidate or elevate the existing session.
    check_reads(initial, 403)


def test_historical_grants_are_strict_during_read_apply_copy_and_sync(accounts, db):
    from app.models.system import SysRoleTemplate, SysUser
    from sqlalchemy import select

    tpl = accounts.post(
        "/api/role-templates",
        json={
            "name": "来源异常仓库岗位",
            "copy_from": "readonly",
            "code": "invalid-warehouse-source",
        },
    ).json()
    role_url = "/api/role-templates/" + tpl["code"]
    subject = "invalid-warehouse-account"
    assert (
        accounts.post(
            "/api/accounts",
            json={
                "username": subject,
                "password": PASSWORD,
                "template_code": tpl["code"],
            },
        ).status_code
        == 201
    )
    template = db.scalar(
        select(SysRoleTemplate).where(SysRoleTemplate.code == tpl["code"])
    )
    user = db.scalar(select(SysUser).where(SysUser.username == subject))
    url = "/api/accounts/" + subject
    template.permissions = {**template.permissions, KEY: "true"}
    for legacy, base, override in (
        ({KEY: 1}, None, None),
        (None, {KEY: "false"}, None),
        (None, {KEY: True}, {KEY: "true"}),
        (None, {KEY: True}, ["bad"]),
    ):
        user.permissions, user.template_perms, user.perm_overrides = (
            legacy,
            base,
            override,
        )
        db.commit()
        exposed = next(
            a for a in accounts.get("/api/accounts").json() if a["username"] == subject
        )
        assert (
            exposed["permissions"][KEY] is False
            and exposed["runtime_permissions"][KEY] is False
        )
        check_reads(login(subject, False), 403)
    assert (
        next(
            t
            for t in accounts.get("/api/role-templates").json()
            if t["code"] == tpl["code"]
        )["permissions"][KEY]
        is False
    )
    copied = accounts.post(
        "/api/role-templates", json={"name": "复制岗位", "copy_from": tpl["code"]}
    )
    assert copied.status_code == 201 and copied.json()["permissions"][KEY] is False
    applied = accounts.put(url, json={"template_code": tpl["code"]})
    assert applied.status_code == 200 and applied.json()["permissions"][KEY] is False
    for operation in ("apply_template", "grant", "revoke"):
        payload = {
            "usernames": [subject],
            "operation": operation,
            "client_label": "仓单授权",
        }
        if operation == "apply_template":
            payload["template_code"] = tpl["code"]
        else:
            payload["keys"] = [KEY]
        dry = accounts.post("/api/accounts/bulk", json=payload)
        assert dry.status_code == 200, dry.text
        committed = accounts.post(
            "/api/accounts/bulk",
            json={
                **payload,
                "dry_run": False,
                "fingerprint": dry.json()["fingerprint"],
            },
        )
        assert committed.status_code == 200
        check_reads(
            login(subject, operation == "grant"), 200 if operation == "grant" else 403
        )
    db.refresh(template)
    db.refresh(user)
    template.permissions = {**template.permissions, KEY: True}
    user.perm_overrides = {KEY: "false"}
    db.commit()
    plan = accounts.post(
        role_url + "/sync", json={"dry_run": True, "client_label": "仓单授权"}
    )
    assert plan.status_code == 200
    result = accounts.post(
        role_url + "/sync",
        json={"dry_run": False, "fingerprint": plan.json()["fingerprint"]},
    )
    assert result.status_code == 200
    check_reads(login(subject, False), 403)


def test_edit_inventory_grant_preserves_effective_legacy_permissions(accounts, db):
    from app.models.system import SysUser
    from sqlalchemy import select

    subject = "old-warehouse-account"
    assert (
        accounts.post(
            "/api/accounts",
            json={
                "username": subject,
                "role": "sales",
                "password": PASSWORD,
            },
        ).status_code
        == 201
    )
    user = db.scalar(select(SysUser).where(SysUser.username == subject))
    user.template_perms = None
    user.permissions = {"data_supplier": False, "page_accounts": False, KEY: False}
    user.perm_overrides = {"data_supplier": True, "page_accounts": True}
    db.commit()
    row = next(
        x for x in accounts.get("/api/accounts").json() if x["username"] == subject
    )
    assert row["overrides"] == {} and not row["is_custom"]
    initial, version = row["permissions"], user.token_version
    assert initial["data_supplier"] is False and initial["page_accounts"] is False
    # Mirrors the drawer: retain real overrides and change only the warehouse key.
    single = accounts.put(
        "/api/accounts/" + subject, json={"overrides": {**row["overrides"], KEY: True}}
    )
    assert single.status_code == 200
    assert single.json()["permissions"] == {**initial, KEY: True}
    db.refresh(user)
    assert user.token_version == version + 1
    explicit = accounts.put(
        "/api/accounts/" + subject,
        json={
            "overrides": {**single.json()["overrides"], "data_supplier": True},
        },
    )
    assert explicit.status_code == 200
    assert explicit.json()["permissions"] == {
        **initial,
        KEY: True,
        "data_supplier": True,
    }


def test_admin_template_initial_default_and_malformed_value(accounts, db):
    from app.models.system import SysRoleTemplate
    from sqlalchemy import select

    template = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == "admin"))
    original = template.permissions
    for suffix, value, expected in (
        ("old", {}, True),
        ("unclear", {KEY: "true"}, False),
        ("off", {KEY: False}, False),
    ):
        template.permissions = {
            **{k: v for k, v in original.items() if k != KEY},
            **value,
        }
        db.commit()
        created = accounts.post(
            "/api/accounts",
            json={
                "username": "admin-template-" + suffix,
                "role": "admin",
                "password": PASSWORD,
            },
        )
        assert (
            created.status_code == 201
            and created.json()["template_perms"][KEY] is expected
        )
        assert login("admin-template-" + suffix, expected).get(
            "/api/inventory/analytics/coverage"
        ).status_code == (200 if expected else 403)
