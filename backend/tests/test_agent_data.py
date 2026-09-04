"""DSH 企业助手数据通道（/api/agent/schema|sql|call|scripts|dsn + /api/system-settings/dsh-llm-config）。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.system import SysUser

PASSWORD = "agent-data-password-1"


def _client(db, username: str, role: str = "readonly", overrides: dict | None = None) -> TestClient:
    base = permissions.effective(role, None)
    effective = permissions.effective_from_snapshot(base, overrides or {})
    db.add(SysUser(username=username, role=role, display_name=username,
                   password_hash=hash_password(PASSWORD), is_active=True,
                   template_code=role, template_version=1, template_perms=base,
                   perm_overrides=overrides or {}, permissions=effective))
    db.commit()
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = f"Bearer {r.json()['token']}"
    return c


@pytest.fixture()
def admin(db):
    return _client(db, "agent-admin", role="admin")


@pytest.fixture()
def analyst(db):
    """采购角色 + 显式授予 agent_sql；无供应商/成本之外的字段照常，客户隐藏。"""
    return _client(db, "agent-analyst", role="purchaser",
                   overrides={"action_agent_sql": True, "data_customer": False})


@pytest.fixture()
def scoped_sales(db):
    return _client(db, "agent-sales", role="sales", overrides={"action_agent_sql": True})


def test_permission_keys_registered():
    assert "action_agent_sql" in permissions.ALL_KEYS
    assert "action_agent_dsn_ro" in permissions.ALL_KEYS
    assert permissions.ROLE_TEMPLATES["admin"]["action_agent_sql"] is True
    for role in ("boss", "sales", "purchaser", "readonly", "maintenance_manager"):
        assert permissions.ROLE_TEMPLATES[role]["action_agent_sql"] is False
        assert permissions.ROLE_TEMPLATES[role]["action_agent_dsn_ro"] is False
    assert "action_agent_sql" in permissions.PERMISSION_META
    assert any("action_agent_sql" in g["keys"] for g in permissions.UI_GROUPS)


def test_schema_excludes_sensitive_tables(admin):
    r = admin.get("/api/agent/schema", params={"refresh": "true"})
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["tables"]}
    assert "dim_part" in names
    assert not ({"sys_user", "sys_access_log", "sys_role_template", "sys_dsh_script", "alembic_version"} & names)
    part = next(t for t in r.json()["tables"] if t["name"] == "dim_part")
    assert any(c["name"] == "pn_std" for c in part["columns"])


def test_schema_requires_action(db):
    c = _client(db, "agent-readonly", role="readonly")
    assert c.get("/api/agent/schema").status_code == 403


def test_sql_readonly_and_masking(admin, analyst, db):
    db.execute(text("INSERT INTO dim_part (pn_std, brand) VALUES ('AGT-TEST-1', 'BrandX') ON CONFLICT DO NOTHING"))
    db.commit()
    r = admin.post("/api/agent/sql", json={"sql": "select pn_std, brand from dim_part where pn_std = 'AGT-TEST-1'"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == ["pn_std", "brand"]
    assert body["rows"] and body["rows"][0]["pn_std"] == "AGT-TEST-1"
    assert body["truncated"] is False
    # 写语句 / 多语句 / 系统表 / 危险函数 一律拒绝
    assert admin.post("/api/agent/sql", json={"sql": "update dim_part set brand='x'"}).status_code == 400
    assert admin.post("/api/agent/sql", json={"sql": "select 1; select 2"}).status_code == 400
    assert admin.post("/api/agent/sql", json={"sql": "select * from sys_user"}).status_code == 403
    assert admin.post("/api/agent/sql", json={"sql": "select pg_sleep(1)"}).status_code == 400
    assert admin.post("/api/agent/sql", json={"sql": "select * from dim_part; -- x"}).status_code == 200
    # 字符串里的关键字不误杀
    assert admin.post("/api/agent/sql", json={"sql": "select 'update me' as note"}).status_code == 200
    # 字段脱敏：分析员无客户可见权限 → 含 customer 的列被抹
    r2 = analyst.post("/api/agent/sql", json={"sql": "select 'c1' as customer_name, 'x' as pn_std"})
    assert r2.status_code == 200, r2.text
    row = r2.json()["rows"][0]
    assert row["pn_std"] == "x"
    assert row["customer_name"] != "c1"


def test_sql_truncation(admin):
    r = admin.post("/api/agent/sql", json={"sql": "select generate_series(1, 50) as n", "max_rows": 10})
    assert r.status_code == 200
    assert r.json()["row_count"] == 10 and r.json()["truncated"] is True


def test_sql_scoped_sales_forbidden(scoped_sales):
    r = scoped_sales.post("/api/agent/sql", json={"sql": "select 1"})
    assert r.status_code == 403


def test_call_whitelist(admin):
    r = admin.post("/api/agent/call", json={"tool": "search_parts", "args": {"query": "AGT"}})
    assert r.status_code == 200, r.text
    assert "items" in r.json()
    assert admin.post("/api/agent/call", json={"tool": "rm_rf", "args": {}}).status_code == 400


def test_scripts_crud_and_run(admin, db):
    body = {"name": "echo-args", "description": "t", "content":
            "import os, json\nprint(json.dumps({'user': os.environ['ITD_USER'], 'args': json.loads(os.environ['ITD_ARGS_JSON'])}))",
            "required_action": None, "timeout_seconds": 20, "enabled": True}
    r = admin.post("/api/agent/scripts", json=body)
    assert r.status_code == 200, r.text
    assert admin.post("/api/agent/scripts", json=body).status_code == 409
    assert admin.post("/api/agent/scripts", json={**body, "name": "Bad Name"}).status_code == 400
    assert admin.post("/api/agent/scripts", json={**body, "name": "x2", "required_action": "page_parts"}).status_code == 400
    r = admin.post("/api/agent/scripts/echo-args/run", json={"args": {"k": 1}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["returncode"] == 0
    assert '"k": 1' in out["stdout"] and "agent-admin" in out["stdout"]
    # 非 admin：列表可见但无源码；不能写
    ro = _client(db, "agent-ro2", role="readonly")
    listing = ro.get("/api/agent/scripts").json()["scripts"]
    assert listing and "content" not in listing[0]
    assert ro.post("/api/agent/scripts", json={**body, "name": "nope"}).status_code == 403
    # required_action 门控：readonly 无 action_pool_manage
    admin.put("/api/agent/scripts/echo-args", json={**body, "required_action": "action_pool_manage"})
    assert ro.post("/api/agent/scripts/echo-args/run", json={"args": {}}).status_code == 403
    assert admin.post("/api/agent/scripts/echo-args/run", json={"args": {}}).status_code == 200
    # 超时
    admin.put("/api/agent/scripts/echo-args", json={**body, "content": "import time; time.sleep(30)", "timeout_seconds": 5})
    r = admin.post("/api/agent/scripts/echo-args/run", json={"args": {}})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json().get("timeout") is True
    assert admin.delete("/api/agent/scripts/echo-args").status_code == 200
    assert admin.delete("/api/agent/scripts/echo-args").status_code == 404


def test_dsn_gate(admin, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "dsh_ro_dsn", "")
    assert admin.get("/api/agent/dsn").status_code == 501
    monkeypatch.setattr(settings, "dsh_ro_dsn", "postgresql://ro:ro@db/x")
    assert admin.get("/api/agent/dsn").json()["dsn"] == "postgresql://ro:ro@db/x"
    ro = _client(db, "agent-ro3", role="readonly")
    assert ro.get("/api/agent/dsn").status_code == 403


def test_dsh_llm_config_gate(admin, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "dsh_config_token", "machine-secret")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    r = admin.get("/api/system-settings/dsh-llm-config")
    assert r.status_code == 200 and r.json()["default_model"] == "test-model"
    anon = TestClient(app)
    assert anon.get("/api/system-settings/dsh-llm-config").status_code == 401
    assert anon.get("/api/system-settings/dsh-llm-config", headers={"x-dsh-config-token": "wrong"}).status_code == 401
    assert anon.get("/api/system-settings/dsh-llm-config", headers={"x-dsh-config-token": "machine-secret"}).status_code == 200
    ro = _client(db, "agent-ro4", role="readonly")
    assert ro.get("/api/system-settings/dsh-llm-config").status_code == 403
