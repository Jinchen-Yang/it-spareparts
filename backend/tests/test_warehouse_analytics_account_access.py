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
