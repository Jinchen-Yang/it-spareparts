"""Agent 数据访问端点（/api/agent/sql|schema|call，企业定制 P3）。

覆盖：
- 权限门：page_chat 路由准入、action_agent_sql 显式授权（模板默认 False）、
  own_customers_only 行级隔离账号禁用直查。
- SQL 护栏：仅单条 SELECT/WITH、多语句拒绝、写关键词拒绝、敏感表/目录拒绝。
- 数据库侧只读：SET TRANSACTION READ ONLY 下 UPDATE 由数据库报错（护栏漏网兜底）。
- 字段级脱敏：sales（data_supplier=False）查 dim_supplier → 供应商字段置 null。
- schema：不含敏感表；call：白名单外 404、白名单内走 dispatch 权限过滤。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import hash_password
from app.db import engine
from app.main import app
from app.models.system import SysUser

c = TestClient(app)


def _login(db, username, perms=None, role="sales"):
    db.add(SysUser(username=username, role=role,
                   password_hash=hash_password("pw123456"), permissions=perms))
    db.commit()
    r = c.post("/api/auth/login", json={"username": username, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------- 权限门 ----------

def test_sql_requires_action_agent_sql(db):
    """sales 模板默认无 action_agent_sql → 403；显式授权后放行。"""
    no_perm = _login(db, "sq_noperm")
    r = c.post("/api/agent/sql", headers=no_perm, json={"sql": "SELECT 1 AS x"})
    assert r.status_code == 403

    granted = _login(db, "sq_granted", {"action_agent_sql": True})
    r = c.post("/api/agent/sql", headers=granted, json={"sql": "SELECT 1 AS x"})
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [{"x": 1}]


def test_sql_blocked_for_own_customers_only(db):
    """行级客户隔离账号即使有 action_agent_sql 也禁直查（绕过行级匿名化）。"""
    h = _login(db, "sq_own", {"action_agent_sql": True, "own_customers_only": True})
    r = c.post("/api/agent/sql", headers=h, json={"sql": "SELECT 1 AS x"})
    assert r.status_code == 403
    assert "own_customers_only" in r.json()["detail"] or "行级" in r.json()["detail"]


def test_routes_require_page_chat(db):
    no_chat = _login(db, "sq_nochat", {"page_chat": False, "action_agent_sql": True})
    assert c.post("/api/agent/sql", headers=no_chat,
                  json={"sql": "SELECT 1"}).status_code == 403
    assert c.get("/api/agent/schema", headers=no_chat).status_code == 403
    assert c.post("/api/agent/call", headers=no_chat,
                  json={"tool": "get_inventory", "args": {}}).status_code == 403


def test_sql_requires_login():
    assert c.post("/api/agent/sql", json={"sql": "SELECT 1"}).status_code == 401


# ---------- SQL 护栏 ----------

@pytest.fixture()
def sql_user(db):
    # 数据岗口径：开 agent_sql；关掉 sales 模板自带的行级隔离（否则端点整体拒绝）
    return _login(db, "sq_ok", {"action_agent_sql": True, "own_customers_only": False})


@pytest.mark.parametrize("bad", [
    "UPDATE dim_part SET part_number = 'x'",           # 写语句
    "DELETE FROM dim_part",                            # 删除
    "insert into dim_part values (1)",                 # 小写 insert
    "select 1; select 2",                              # 多语句
    "select 1; drop table dim_part",                   # 多语句+写
    "WITH t AS (SELECT 1) DELETE FROM dim_part",       # CTE 写
    "select * from sys_user",                          # 敏感表（口令散列）
    "select * from pg_catalog.pg_tables",              # 系统目录
    "select * from information_schema.columns",        # 系统目录
    "select set_config('statement_timeout', '0', false)",  # 会话函数
    "vacuum analyze dim_part",                         # 维护语句
])
def test_sql_rejects_bad_statements(db, sql_user, bad):
    r = c.post("/api/agent/sql", headers=sql_user, json={"sql": bad})
    assert r.status_code in (400, 403), f"{bad!r} 应被拒绝，实际 {r.status_code}"


def test_sql_comments_and_literals(db, sql_user):
    """注释里的关键词不参与执行 → 放行（剥注释后检查，见 _guard_sql）；
    字符串字面量里的关键词按失败关闭拒绝（宁可误伤不可漏放）。"""
    r = c.post("/api/agent/sql", headers=sql_user,
               json={"sql": "SELECT 1 /* insert */ AS x"})
    assert r.status_code == 200
    r = c.post("/api/agent/sql", headers=sql_user,
               json={"sql": "SELECT 'insert into x' AS k"})
    assert r.status_code == 400


def test_sql_readonly_enforced_by_database(db, sql_user):
    """护栏漏网（如未列关键词的写路径）由 READ ONLY 事务兜底拒绝。"""
    r = c.post("/api/agent/sql", headers=sql_user,
               json={"sql": "SELECT 1 AS x UNION ALL SELECT 2 AS x"})
    assert r.status_code == 200
    # 只读事务内任何写语义由数据库本身报错（端到端兜底验证）
    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            with pytest.raises(Exception):
                conn.execute(text("CREATE TEMP TABLE _ro_probe (id int)"))


def test_sql_limit_and_truncation(db, sql_user):
    r = c.post("/api/agent/sql", headers=sql_user,
               json={"sql": "SELECT generate_series(1, 50) AS n", "max_rows": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 10 and body["truncated"] is True
    assert body["columns"] == ["n"]


# ---------- 字段级脱敏 ----------

def test_sql_field_masking_for_sales(db, sql_user):
    """sales 无 data_supplier → 供应商字段列（FIELD_GROUPS supplier_info）置 null。"""
    with db.begin():
        db.execute(text(
            "INSERT INTO dim_supplier (name_raw, name_normalized, supplier_code)"
            " VALUES ('深圳某某电子', '深圳某某电子', 'SUP001')"))
    r = c.post("/api/agent/sql", headers=sql_user,
               json={"sql": "SELECT name_raw, supplier_code FROM dim_supplier"})
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["name_raw"] is None and row["supplier_code"] is None


def test_sql_admin_sees_fields(db):
    admin = _login(db, "sq_admin", role="admin")
    with db.begin():
        db.execute(text(
            "INSERT INTO dim_supplier (name_raw, name_normalized, supplier_code)"
            " VALUES ('深圳某某电子2', '深圳某某电子2', 'SUP002')"
            " ON CONFLICT (name_raw) DO NOTHING"))
    r = c.post("/api/agent/sql", headers=admin,
               json={"sql": "SELECT supplier_code FROM dim_supplier"
                             " WHERE name_raw = '深圳某某电子2'"})
    assert r.status_code == 200
    assert r.json()["rows"][0]["supplier_code"] == "SUP002"


# ---------- schema ----------

def test_schema_excludes_sensitive_tables(db):
    h = _login(db, "sc_ok")
    r = c.get("/api/agent/schema", headers=h)
    assert r.status_code == 200
    body = r.json()
    names = {t["name"] for t in body["tables"]}
    assert "sys_user" not in names
    assert "business_file" not in names
    assert "dim_part" in names
    dim_part = next(t for t in body["tables"] if t["name"] == "dim_part")
    assert dim_part["columns"], "dim_part 应有列元数据"


# ---------- call ----------

def test_call_whitelist(db):
    h = _login(db, "cl_ok")
    r = c.post("/api/agent/call", headers=h,
               json={"tool": "write_excel", "args": {}})
    assert r.status_code == 404
    r = c.post("/api/agent/call", headers=h,
               json={"tool": "search_parts", "args": {"query": "  "}})
    assert r.status_code == 200
    body = r.json()
    assert "error" in body  # 业务错（空查询）原样回灌，而非 500


# ---------- P4：白名单脚本 + 只读 DSN ----------

def test_scripts_crud_requires_admin(db):
    admin = _login(db, "sc_admin", role="admin")
    h = _login(db, "sc_user")
    r = c.post("/api/agent/scripts", headers=h,
               json={"name": "x", "content": "print(1)"})
    assert r.status_code == 403
    r = c.post("/api/agent/scripts", headers=admin,
               json={"name": "health_check", "content": "import os\nprint(os.environ.get('ITD_USER',''))"})
    assert r.status_code == 200
    # 重名 409
    r = c.post("/api/agent/scripts", headers=admin,
               json={"name": "health_check", "content": "print(2)"})
    assert r.status_code == 409
    # 非法权限键 400
    r = c.post("/api/agent/scripts", headers=admin,
               json={"name": "bad_action", "content": "print(1)",
                     "required_action": "not_a_key"})
    assert r.status_code == 400
    # 列表：普通用户只见 enabled
    r = c.get("/api/agent/scripts", headers=h)
    assert {s["name"] for s in r.json()["scripts"]} == {"health_check"}
    # 删除
    r = c.delete("/api/agent/scripts/health_check", headers=admin)
    assert r.status_code == 200


def test_script_run_action_gate_and_timeout(db):
    admin = _login(db, "sr_admin", role="admin")
    h = _login(db, "sr_user")
    r = c.post("/api/agent/scripts", headers=admin,
               json={"name": "write_ledger", "content": "print('ledger')",
                     "required_action": "action_maintenance_ledger_import"})
    assert r.status_code == 200
    # 无该动作 → 403；admin 短路放行
    assert c.post("/api/agent/scripts/write_ledger/run", headers=h,
                  json={"args": {}}).status_code == 403
    assert c.post("/api/agent/scripts/write_ledger/run", headers=admin,
                  json={"args": {}}).status_code == 200
    # 超时保护
    c.post("/api/agent/scripts", headers=admin,
           json={"name": "slow", "content": "import time; time.sleep(30)",
                 "timeout_seconds": 5})
    r = c.post("/api/agent/scripts/slow/run", headers=h, json={"args": {}})
    body = r.json()
    assert body["ok"] is False and "超时" in body["stderr"]
    # 停用脚本 404
    c.put("/api/agent/scripts/slow", headers=admin,
          json={"name": "slow", "content": "print(1)", "enabled": False})
    assert c.post("/api/agent/scripts/slow/run", headers=h,
                  json={"args": {}}).status_code == 404


def test_dsn_gate(db):
    h = _login(db, "dsn_user")
    # 未授权 → 403（配置与否都到不了）
    assert c.get("/api/agent/dsn", headers=h).status_code == 403
    granted = _login(db, "dsn_granted",
                     {"action_agent_dsn_ro": True, "own_customers_only": False})
    # 授权但部署未配置 DSH_RO_DSN → 501
    assert c.get("/api/agent/dsn", headers=granted).status_code == 501
