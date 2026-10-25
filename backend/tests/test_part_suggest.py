"""实时联想接口：鉴权、参数边界、字段净化与访问审计。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, permissions
from app.api import parts
from app.auth import _make_token, current_role
from app.db import get_db
from app.main import app
from app.models.dimensions import DimPart
from app.models.system import SysAccessLog
from app.security import UserContext, get_current_user_context


def _headers(role="sales", overrides=None):
    token, _ = _make_token(
        role, "联想测试", "联想测试", perms=permissions.effective(role, overrides), authn="sys_user",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(db):
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def test_suggest_requires_login(client):
    assert client.get("/api/parts/suggest", params={"q": "A"}).status_code == 401
    assert client.get("/api/parts/suggest").status_code == 401


@pytest.mark.parametrize("role", ["sales", "purchaser", "readonly", "boss", "admin"])
def test_suggest_any_logged_in_role_can_read(client, db, role):
    part = DimPart(pn_std="SUGGEST-READ-427", description="联想接口测试")
    db.add(part)
    db.commit()
    response = client.get("/api/parts/suggest", params={"q": part.pn_std}, headers=_headers(role))
    assert response.status_code == 200
    assert response.json()[0]["part_id"] == part.id


@pytest.mark.parametrize("limit", [0, -1, 31])
def test_suggest_limit_is_bounded(client, limit):
    response = client.get("/api/parts/suggest", params={"q": "A", "limit": limit}, headers=_headers())
    assert response.status_code == 422


def test_suggest_default_and_explicit_limit_are_forwarded(client, monkeypatch):
    calls = []

    def search(db, q, *, limit=10, filters=None):
        calls.append((q, limit, filters))
        return []

    monkeypatch.setattr(parts.part_search, "search", search)
    assert client.get("/api/parts/suggest", params={"q": "  MODEL  "}, headers=_headers()).json() == []
    assert client.get("/api/parts/suggest", params={"q": "MODEL", "limit": 30}, headers=_headers()).json() == []
    assert calls == [("MODEL", 10, None), ("MODEL", 30, None)]


@pytest.mark.parametrize("q", [None, "", "  \t\n", "　"])
def test_empty_suggest_does_not_query_database(monkeypatch, q):
    class NoDatabase:
        def execute(self, *args, **kwargs):
            pytest.fail("空联想不应执行检索 SQL")

        def scalar(self, *args, **kwargs):
            pytest.fail("空联想不应读取数据库")

    def unexpected_search(*args, **kwargs):
        pytest.fail("空联想不应调用检索服务")

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: NoDatabase()
    app.dependency_overrides[current_role] = lambda: "sales"
    app.dependency_overrides[get_current_user_context] = lambda: UserContext("test", "sales")
    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", False)
    monkeypatch.setattr(parts.part_search, "search", unexpected_search)
    try:
        with TestClient(app) as test_client:
            response = test_client.get("/api/parts/suggest", params={} if q is None else {"q": q})
        assert response.status_code == 200
        assert response.json() == []
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_suggest_applies_real_recursive_field_visibility(client, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    # 注入敏感字段，证明联想响应确实经过既有净化器，未来增加角标也不会绕过它。
    monkeypatch.setattr(parts.part_search, "search", lambda *args, **kwargs: [{
        "part_id": 1, "pn_std": "SAFE-PN", "unit_cost": "123.45",
        "supplier": "私密供应商", "nested": {"unit_price": "66.00"},
    }])
    response = client.get(
        "/api/parts/suggest", params={"q": "SAFE"},
        headers=_headers("sales", {"data_purchase_cost": False, "data_profit": False}),
    )
    assert response.status_code == 200
    item = response.json()[0]
    assert item["pn_std"] == "SAFE-PN"
    assert item["unit_cost"] == config.MASK_VALUE
    assert item["supplier"] == config.MASK_VALUE
    assert item["nested"]["unit_price"] == config.MASK_VALUE


def test_suggest_records_access_audit(client, db, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", True)
    monkeypatch.setattr(parts.part_search, "search", lambda *args, **kwargs: [])
    response = client.get("/api/parts/suggest", params={"q": "  AUDIT  ", "limit": 7}, headers=_headers())
    assert response.status_code == 200
    row = db.scalar(select(SysAccessLog).where(SysAccessLog.action == "suggest"))
    assert row is not None
    assert row.resource == "parts"
    assert row.username == "联想测试"
    assert row.detail == {"q": "AUDIT", "limit": 7}
