"""Agent 数据访问端点（DSH itdata 插件 / 企业定制 P3）。

三个端点，全部挂在 /api/agent* 下并带 require_page("page_chat") 后端准入
（结构红线 test_every_agent_route_carries_page_chat_gate）：

- POST /agent/sql    text2sql 执行口：单条只读 SELECT/WITH，走独立短连接的
                     READ ONLY 事务 + statement_timeout，结果经
                     apply_field_visibility 字段级脱敏后返回。
- GET  /agent/schema 表结构元数据（表/列/类型），不含敏感系统表，供模型写 SQL。
- POST /agent/call   转发白名单内的既有只读业务工具（tools.dispatch），复用
                     后端业务层全部行级/字段级权限过滤。

权限门：
- 三者都要求 page_chat（AI 助手准入）。
- /agent/sql 额外要求 action_agent_sql（按账号显式授权，模板默认 False；
  admin 走 require_action 短路恒放行）。
- own_customers_only=True 的账号禁用 /agent/sql：行级匿名化只在业务层实现，
  原生 SQL 会绕过它看到同事客户名 —— 这类账号一律走 /agent/call 业务工具。
"""
import re
import threading
import time
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config
from app import permissions as perms
from app.agent import tools as agent_tools
from app.auth import current_role, require_admin
from app.config import MASK_VALUE, get_settings
from app.db import Base, engine, get_db
from app.security import (
    UserContext,
    apply_field_visibility,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)

_log = logging.getLogger("agent_data")

router = APIRouter(prefix="/agent", tags=["agent-data"],
                   dependencies=[Depends(require_page("page_chat"))])

# ── /agent/sql 护栏 ──────────────────────────────────────────────────────────

# 语句必须是单条 SELECT / WITH（去掉尾部分号后不得再含分号）
_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.I)
# 任何写/DDL/会话/管理语句关键词 → 400（字符串字面量里的关键词也会拒——失败关闭）
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum"
    r"|call|do|comment|reindex|cluster|listen|notify|prepare|execute|set|reset"
    r"|begin|commit|rollback|savepoint|analyze|import|foreign)\b", re.I)
# 凭据/审计/内部表：直查会绕过下载 ACL 或泄露口令散列 → 一律 403
_SENSITIVE_TABLES = re.compile(
    r"\b(sys_user|sys_access_log|sys_security_events|sys_role_template"
    r"|sys_raw_file|sys_audit_log|alembic_version|business_file"
    r"|business_file_link|business_file_download_audit)\b", re.I)
# 系统目录/内核表不开放（schema 端点提供策展后的元数据）
_CATALOG = re.compile(r"\b(pg_\w+|information_schema)\b", re.I)
# 输出兜底：即使可查表，命中密感列名的值也置 MASK_VALUE
_SECRET_COLUMN = re.compile(r"(password|passwd|secret|token|api_key|private_key)", re.I)

_MAX_SQL_CHARS = 8000
_MAX_ROWS_DEFAULT = 100
_MAX_ROWS_CAP = 500
_STATEMENT_TIMEOUT_MS = 8000


class SqlRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=_MAX_SQL_CHARS)
    max_rows: int = Field(default=_MAX_ROWS_DEFAULT, ge=1, le=_MAX_ROWS_CAP)


def _strip_comments(sql: str) -> str:
    """去掉 -- 行注释与 /* */ 块注释，避免注释文字触发/绕过关键词检查。"""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return sql


def _guard_sql(raw_sql: str) -> str:
    """返回净化后的单条 SELECT；不合法直接抛 400（细节固定文案，不回显 SQL）。"""
    sql = raw_sql.strip()
    if len(sql) > _MAX_SQL_CHARS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SQL 过长")
    stripped = _strip_comments(sql)
    if not _SELECT_ONLY.match(stripped):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "只允许单条 SELECT / WITH 查询语句")
    body = sql.rstrip().rstrip(";")
    if ";" in _strip_comments(body):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不允许一次执行多条语句")
    probe = _strip_comments(body)
    if _FORBIDDEN_KEYWORDS.search(probe):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "检测到非查询关键词，只允许只读 SELECT")
    if _SENSITIVE_TABLES.search(probe):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "该表属于系统敏感表，不允许直查")
    if _CATALOG.search(probe):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "不允许访问系统目录，请使用 /agent/schema 获取表结构")
    return body


@router.post("/sql", dependencies=[Depends(require_action("action_agent_sql"))])
def run_sql(
    req: SqlRequest,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    sql = _guard_sql(req.sql)
    if ctx.permissions and ctx.permissions.get("own_customers_only") \
            and ctx.role not in ("admin",):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "行级客户隔离（own_customers_only）账号不允许直查数据库，"
            "请改用业务查询工具（call_api）")
    record_access_log(ctx, "agent_sql", "sql",
                      {"sql": sql[:200], "max_rows": req.max_rows})

    fetch_ms = int(time.time() * 1000)
    wrapped = f"SELECT * FROM ({sql}) AS itdata_sub LIMIT {req.max_rows + 1}"
    try:
        # 独立短连接 + 显式事务：SET TRANSACTION READ ONLY 必须是事务第一条语句，
        # 故不复用请求级 Session（其依赖链可能已发过查询）。
        with engine.connect() as conn:
            with conn.begin():
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}")
                result = conn.execute(text(wrapped))
                keys = list(result.keys())
                rows = [dict(row._mapping) for row in result.fetchall()]
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— SQL 语法/超时等用户可见错误
        _log.info("agent sql rejected/failed: %r", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "SQL 执行失败（语法错误或超时），请检查后重试") from exc

    truncated = len(rows) > req.max_rows
    rows = rows[: req.max_rows]
    # 字段级脱敏：递归按列名掩码（FIELD_GROUPS），再兜底掩掉密感列名
    masked = apply_field_visibility({"rows": rows}, ctx)["rows"]
    perms_now = ctx.permissions or perms.effective(ctx.role, None)
    # 维度表原始名列（dim_supplier/dim_customer 同名列）不在 FIELD_GROUPS 里——
    # 业务层从不以此键外泄，但 raw SQL 直接选列名会漏。失败关闭：供应商/客户
    # 可见权限任一关闭就一并掩掉（过度掩码可接受，分级口径走 call_api 业务工具）。
    mask_dim_names = not (perms_now.get("data_supplier") and perms_now.get("data_customer"))
    for row in masked:
        for k in list(row):
            if _SECRET_COLUMN.search(k) or (mask_dim_names and k in ("name_raw", "name_normalized")):
                row[k] = MASK_VALUE
    return {
        "columns": keys,
        "rows": masked,
        "row_count": len(masked),
        "truncated": truncated,
        "elapsed_ms": int(time.time() * 1000) - fetch_ms,
    }


# ── /agent/schema 表结构元数据 ───────────────────────────────────────────────

_SCHEMA_HIDDEN = _SENSITIVE_TABLES  # 同一直查黑名单：schema 也不暴露敏感表
_SCHEMA_TTL_SECONDS = 600
_schema_cache_lock = threading.Lock()
_schema_cache: dict | None = None
_schema_cache_at = 0.0


def _build_schema() -> dict:
    import app.models  # noqa: F401 —— 触发全模型导入，Base.metadata 才完整
    tables = []
    for table in Base.metadata.sorted_tables:
        if _SCHEMA_HIDDEN.search(table.name):
            continue
        tables.append({
            "name": table.name,
            "comment": table.comment,
            "columns": [
                {
                    "name": col.name,
                    "type": str(col.type),
                    "nullable": col.nullable,
                    "comment": col.comment,
                }
                for col in table.columns
                if not _SECRET_COLUMN.search(col.name)
            ],
        })
    return {"tables": tables, "table_count": len(tables)}


@router.get("/schema")
def get_schema(
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    global _schema_cache, _schema_cache_at
    record_access_log(ctx, "agent_schema", "schema", None)
    now = time.time()
    with _schema_cache_lock:
        if _schema_cache is None or now - _schema_cache_at > _SCHEMA_TTL_SECONDS:
            _schema_cache = _build_schema()
            _schema_cache_at = now
        return _schema_cache


# ── /agent/call 白名单业务工具转发 ───────────────────────────────────────────
# 只放只读查询工具：这些工具在业务层内做行级过滤（own_customers_only 匿名化）
# 与字段脱敏，是行级隔离账号的数据通道。文件写/导入/技能类工具不放。

_CALLABLE_TOOLS = frozenset({
    "search_parts", "get_part_overview", "lookup_prices_bulk",
    "list_recent_purchases", "get_profit_ranking", "get_purchase_analysis",
    "get_inventory", "get_maintenance_board", "get_maintenance_projects",
    "get_maintenance_lines", "get_cancellation_stats",
})


class CallRequest(BaseModel):
    tool: str = Field(..., min_length=1, max_length=64)
    args: dict = Field(default_factory=dict)


@router.post("/call")
def call_tool(
    req: CallRequest,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if req.tool not in _CALLABLE_TOOLS:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"工具不在白名单内：{req.tool}")
    record_access_log(ctx, "agent_call", req.tool,
                      {"args": str(req.args)[:300]})
    return agent_tools.dispatch(db, req.tool, req.args, ctx)


# ── P4：白名单脚本 + 只读 DSN ────────────────────────────────────────────────
# 写库边界（已确认决策）：agent 临时脚本只拿只读 DSN；任何 UPDATE/INSERT/覆盖
# 只能走管理员维护的白名单脚本（服务端子进程执行，required_action 动作级准入，
# 每次执行写审计）。脚本内容在服务端执行，写库凭据不离开服务器。

_SCRIPT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SCRIPT_OUTPUT_CAP = 20000


class ScriptIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=256)
    content: str = Field(..., min_length=1)
    required_action: str | None = Field(default=None, max_length=64)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    enabled: bool = True


class ScriptRunRequest(BaseModel):
    args: dict = Field(default_factory=dict)


def _script_public(script, include_content: bool = False) -> dict:
    out = {
        "name": script.name,
        "description": script.description or "",
        "required_action": script.required_action,
        "timeout_seconds": script.timeout_seconds,
        "enabled": script.enabled,
    }
    if include_content:
        out["content"] = script.content
    return out


@router.get("/scripts")
def list_scripts(
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """白名单脚本清单。普通用户只见 enabled；admin 额外看到全部含内容。"""
    from app.models.system import SysDshScript

    record_access_log(ctx, "agent_scripts_list", "script", None)
    q = db.query(SysDshScript).order_by(SysDshScript.name)
    admin = role == "admin"
    scripts = [_script_public(s, include_content=admin) for s in q
               if admin or s.enabled]
    return {"scripts": scripts}


@router.post("/scripts", dependencies=[Depends(require_admin)])
def create_script(
    req: ScriptIn,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    from app.models.system import SysDshScript

    if not _SCRIPT_NAME_RE.match(req.name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "脚本名只允许小写字母/数字/_/-，最长 64 位")
    if req.required_action and req.required_action not in perms.ALL_KEYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"required_action 必须是合法权限键：{req.required_action}")
    if db.query(SysDshScript).filter_by(name=req.name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"脚本已存在：{req.name}")
    s = SysDshScript(name=req.name, description=req.description, content=req.content,
                     required_action=req.required_action,
                     timeout_seconds=req.timeout_seconds, enabled=req.enabled,
                     created_by=ctx.user_id)
    db.add(s)
    db.commit()
    record_access_log(ctx, "agent_script_create", "script", {"name": req.name})
    return _script_public(s, include_content=True)


@router.put("/scripts/{name}", dependencies=[Depends(require_admin)])
def update_script(
    name: str,
    req: ScriptIn,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    from app.models.system import SysDshScript

    s = db.query(SysDshScript).filter_by(name=name).first()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"脚本不存在：{name}")
    if req.required_action and req.required_action not in perms.ALL_KEYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"required_action 必须是合法权限键：{req.required_action}")
    s.description = req.description
    s.content = req.content
    s.required_action = req.required_action
    s.timeout_seconds = req.timeout_seconds
    s.enabled = req.enabled
    db.commit()
    record_access_log(ctx, "agent_script_update", "script", {"name": name})
    return _script_public(s, include_content=True)


@router.delete("/scripts/{name}", dependencies=[Depends(require_admin)])
def delete_script(
    name: str,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    from app.models.system import SysDshScript

    s = db.query(SysDshScript).filter_by(name=name).first()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"脚本不存在：{name}")
    db.delete(s)
    db.commit()
    record_access_log(ctx, "agent_script_delete", "script", {"name": name})
    return {"ok": True}


def _require_script_action(ctx: UserContext, action_key: str | None) -> None:
    """脚本的 required_action 准入：admin 短路；无键=仅需 page_chat（router 已挡）。"""
    if not action_key:
        return
    if not config.ENABLE_RBAC or ctx.role == "admin":
        return
    perms_now = ctx.permissions or perms.effective(ctx.role, None)
    if not perms_now.get(action_key):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"缺少执行该脚本所需的权限：{action_key}")


@router.post("/scripts/{name}/run")
def run_script(
    name: str,
    req: ScriptRunRequest,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """执行白名单脚本：服务端子进程，写库凭据不离开服务器，执行全审计。"""
    import os
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from app.models.system import SysDshScript

    s = db.query(SysDshScript).filter_by(name=name).first()
    if s is None or not s.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"脚本不存在或已停用：{name}")
    _require_script_action(ctx, s.required_action)
    record_access_log(ctx, "agent_script_run", "script",
                      {"name": name, "args": str(req.args)[:300]})

    started = time.time()
    script_args = req.args if isinstance(req.args, dict) else {}
    env = dict(os.environ)
    env["ITD_DB_URL"] = get_settings().database_url
    env["ITD_USER"] = ctx.user_id or ""
    env["ITD_ROLE"] = ctx.role or ""
    env["ITD_ARGS_JSON"] = _json_safe(script_args)
    try:
        with tempfile.TemporaryDirectory(prefix="itd_script_") as tmp:
            path = Path(tmp) / "script.py"
            path.write_text(s.content, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True, timeout=s.timeout_seconds, env=env,
            )
            stdout = proc.stdout.decode("utf-8", errors="replace")[:_SCRIPT_OUTPUT_CAP]
            stderr = proc.stderr.decode("utf-8", errors="replace")[:_SCRIPT_OUTPUT_CAP]
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "elapsed_ms": int((time.time() - started) * 1000),
            }
    except subprocess.TimeoutExpired:
        record_access_log(ctx, "agent_script_timeout", "script", {"name": name})
        return {"ok": False, "returncode": None, "stdout": "",
                "stderr": f"脚本超时（>{s.timeout_seconds}s），已终止", "elapsed_ms": -1}
    except Exception as exc:  # noqa: BLE001
        _log.error("script run failed: %r", exc)
        return {"ok": False, "returncode": None, "stdout": "",
                "stderr": "服务端执行失败（见服务器日志）", "elapsed_ms": -1}


def _json_safe(value) -> str:
    import json as _json
    try:
        return _json.dumps(value, ensure_ascii=False)[:4000]
    except Exception:
        return "{}"


@router.get("/dsn", dependencies=[Depends(require_action("action_agent_dsn_ro"))])
def get_readonly_dsn(
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """领取只读 DSN 供本地临时脚本使用（PG 只读角色，不经字段脱敏）。"""
    if ctx.permissions and ctx.permissions.get("own_customers_only") \
            and ctx.role not in ("admin",):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "行级客户隔离账号不允许领取数据库连接")
    dsn = get_settings().dsh_ro_dsn.strip()
    if not dsn:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED,
                            "部署未配置只读 DSN（env DSH_RO_DSN）")
    record_access_log(ctx, "agent_dsn_ro", "dsn", None)
    return {"dsn": dsn}
