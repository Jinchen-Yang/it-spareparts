"""企业定制 P3 端点本机 E2E 冒烟（macOS 可跑，绕过 Linux-only 测试隔离）。

CI 上由 tests/test_agent_sql.py（run-isolation conftest）覆盖；本脚本面向
开发机：临时建库 → alembic upgrade → TestClient 全链路断言 → 删库。

用法（backend/ 下）：
    uv run python scripts/e2e_agent_sql.py
"""
import json
import os
import secrets
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

BASE_URL = os.environ.get(
    "PYTEST_DATABASE_BASE_URL",
    "postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts_test")

DB_NAME = f"spareparts_itdata_e2e_{secrets.token_hex(4)}"

from sqlalchemy import create_engine, text  # noqa: E402


def main() -> int:
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    base = create_engine(BASE_URL)
    with base.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f'CREATE DATABASE "{DB_NAME}"'))
    base.dispose()

    os.environ["DATABASE_URL"] = f"{BASE_URL.rsplit('/', 1)[0]}/{DB_NAME}"
    os.environ.setdefault("MAINTENANCE_BETA_ENABLED", "true")
    os.environ.setdefault("MAINTENANCE_BOSS_DASHBOARD_ENABLED", "true")

    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        mark = "PASS" if cond else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    try:
        cfg = AlembicConfig(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        alembic_command.upgrade(cfg, "head")

        from fastapi.testclient import TestClient  # noqa: E402
        from app.auth import hash_password  # noqa: E402
        from app.db import SessionLocal  # noqa: E402
        from app.main import app  # noqa: E402
        from app.models.system import SysUser  # noqa: E402

        c = TestClient(app)
        db = SessionLocal()

        def login(username: str, perms: dict | None = None, role: str = "sales"):
            db.add(SysUser(username=username, role=role,
                           password_hash=hash_password("pw123456"),
                           permissions=perms))
            db.commit()
            r = c.post("/api/auth/login",
                       json={"username": username, "password": "pw123456"})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['token']}"}

        # 1) 权限门：默认无 action_agent_sql → 403
        no_perm = login("sq_noperm")
        r = c.post("/api/agent/sql", headers=no_perm, json={"sql": "SELECT 1 AS x"})
        check("sql: default sales 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

        # 2) 显式授权（数据岗口径：开 agent_sql、关行级隔离）→ SELECT 通过
        ok = login("sq_ok", {"action_agent_sql": True, "own_customers_only": False})
        r = c.post("/api/agent/sql", headers=ok, json={"sql": "SELECT 1 AS x"})
        check("sql: granted SELECT 200", r.status_code == 200, r.text[:200])
        check("sql: rows correct", r.status_code == 200 and r.json()["rows"] == [{"x": 1}])

        # 3) 写语句 / 多语句 / 敏感表 / 目录 → 拒绝
        for name, sql, want in [
            ("update", "UPDATE dim_part SET part_number='x'", (400, 403)),
            ("multi", "select 1; select 2", (400, 403)),
            ("sys_user", "select * from sys_user", (400, 403)),
            ("catalog", "select * from pg_tables", (400, 403)),
        ]:
            r = c.post("/api/agent/sql", headers=ok, json={"sql": sql})
            check(f"sql reject: {name}", r.status_code in want,
                  f"{r.status_code} {r.text[:120]}")

        # 4) own_customers_only → 403
        own = login("sq_own", {"action_agent_sql": True, "own_customers_only": True})
        r = c.post("/api/agent/sql", headers=own, json={"sql": "SELECT 1"})
        check("sql: own_customers_only 403", r.status_code == 403)

        # 5) 字段级脱敏：sales 查 dim_supplier → 供应商字段置 null
        db.execute(text(
            "INSERT INTO dim_supplier (name_raw, name_normalized, supplier_code)"
            " VALUES ('深圳冒烟电子', '深圳冒烟电子', 'SUP-E2E')"))
        db.commit()
        r = c.post("/api/agent/sql", headers=ok,
                   json={"sql": "SELECT name_raw, supplier_code FROM dim_supplier"
                                " WHERE supplier_code = 'SUP-E2E'"})
        body = r.json() if r.status_code == 200 else {}
        row = body.get("rows", [{}])[0] if body.get("rows") else {}
        check("sql: masking sales", r.status_code == 200
              and row.get("name_raw") is None and row.get("supplier_code") is None,
              json.dumps(body)[:200])

        # 6) admin 可见明文
        admin = login("sq_admin", role="admin")
        r = c.post("/api/agent/sql", headers=admin,
                   json={"sql": "SELECT supplier_code FROM dim_supplier"
                                " WHERE supplier_code = 'SUP-E2E'"})
        row = r.json()["rows"][0] if r.status_code == 200 and r.json()["rows"] else {}
        check("sql: admin sees supplier_code", row.get("supplier_code") == "SUP-E2E")

        # 7) schema：不含敏感表、含业务表
        r = c.get("/api/agent/schema", headers=ok)
        names = {t["name"] for t in r.json()["tables"]} if r.status_code == 200 else set()
        check("schema: sys_user hidden", "sys_user" not in names)
        check("schema: dim_part present", "dim_part" in names)

        # 8) call：白名单外 404、白名单内业务错回灌
        r = c.post("/api/agent/call", headers=ok, json={"tool": "write_excel", "args": {}})
        check("call: non-whitelist 404", r.status_code == 404)
        r = c.post("/api/agent/call", headers=ok,
                   json={"tool": "search_parts", "args": {"query": "  "}})
        check("call: whitelist dispatch", r.status_code == 200 and "error" in r.json())

        # 9) page_chat 门
        nochat = login("sq_nochat", {"page_chat": False, "action_agent_sql": True})
        r = c.post("/api/agent/sql", headers=nochat, json={"sql": "SELECT 1"})
        check("gate: page_chat 403", r.status_code == 403)

        # 10) 结构红线：/api/agent* 全部路由带 require_page
        bad_routes = [
            route.path for route in app.routes
            if getattr(route, "path", "").startswith("/api/agent")
            and not any("require_page" in getattr(d.call, "__qualname__", "")
                        for d in route.dependant.dependencies)
        ]
        check("structure: all agent routes gated", not bad_routes, str(bad_routes))

        # ── P4：白名单脚本 + 只读 DSN ──────────────────────────────────────
        # 11) 脚本 CRUD：非 admin 被拒
        r = c.post("/api/agent/scripts", headers=ok,
                   json={"name": "x1", "content": "print(1)"})
        check("scripts: non-admin create 403", r.status_code == 403)

        # 12) admin 创建 → 列表 → 执行（无 required_action：仅 page_chat）
        r = c.post("/api/agent/scripts", headers=admin,
                   json={"name": "health_check", "description": "回显用户",
                         "content": "import os\nprint('hi', os.environ.get('ITD_USER', ''))"})
        check("scripts: admin create", r.status_code == 200, r.text[:150])
        r = c.get("/api/agent/scripts", headers=ok)
        names = {s["name"] for s in r.json()["scripts"]} if r.status_code == 200 else set()
        check("scripts: list shows enabled", "health_check" in names)
        r = c.post("/api/agent/scripts/health_check/run", headers=ok, json={"args": {}})
        body = r.json() if r.status_code == 200 else {}
        check("scripts: run by page_chat user",
              r.status_code == 200 and body.get("ok") is True and "sq_ok" in (body.get("stdout") or ""),
              r.text[:200])

        # 13) required_action 门：绑定 action_maintenance_ledger_import，无权限用户 403
        r = c.post("/api/agent/scripts", headers=admin,
                   json={"name": "write_ledger", "content": "print('ledger')",
                         "required_action": "action_maintenance_ledger_import"})
        check("scripts: admin create w/ action", r.status_code == 200)
        r = c.post("/api/agent/scripts/write_ledger/run", headers=ok, json={"args": {}})
        check("scripts: missing action 403", r.status_code == 403, r.text[:150])
        admin_run = c.post("/api/agent/scripts/write_ledger/run", headers=admin, json={"args": {}})
        check("scripts: admin short-circuit", admin_run.status_code == 200)

        # 14) 脚本超时保护
        r = c.post("/api/agent/scripts", headers=admin,
                   json={"name": "slow_script", "content": "import time; time.sleep(30)",
                         "timeout_seconds": 5})
        check("scripts: create slow", r.status_code == 200)
        r = c.post("/api/agent/scripts/slow_script/run", headers=ok, json={"args": {}})
        body = r.json() if r.status_code == 200 else {}
        check("scripts: timeout enforced",
              r.status_code == 200 and body.get("ok") is False and "超时" in (body.get("stderr") or ""),
              r.text[:200])

        # 15) DSN：无授权 → 403；已授权但部署未配置 → 501
        r = c.get("/api/agent/dsn", headers=ok)
        check("dsn: no permission 403", r.status_code == 403)
        dsn_user = login("dsn_ok", {"action_agent_dsn_ro": True, "own_customers_only": False})
        r = c.get("/api/agent/dsn", headers=dsn_user)
        check("dsn: granted but unconfigured 501", r.status_code == 501)
        no_dsn = login("dsn_noperm")
        r = c.get("/api/agent/dsn", headers=no_dsn)
        check("dsn: no permission 403", r.status_code == 403)

        db.close()
    finally:
        try:
            eng = create_engine(f"{BASE_URL.rsplit('/', 1)[0]}/{DB_NAME}")
            eng.dispose()
        except Exception:
            pass
        base = create_engine(BASE_URL)
        with base.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
        base.dispose()

    print()
    if failures:
        print(f"E2E FAILED: {len(failures)} 项未通过 → {failures}")
        return 1
    print("E2E OK: agent_data 端点全链路通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
