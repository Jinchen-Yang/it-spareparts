"""DSH 企业助手（itdata-dsh）数据通道：权限门下的 text2sql / 表结构 / 业务工具白名单 / 白名单脚本 / 只读 DSN。

设计（docs/DSH 企业定制实施计划 P3/P4）：
- 所有端点都以当前登录用户的 token 执行，结果按其 data_* 可见性脱敏、按 page_*/action_* 准入。
- /sql 只读：单条 SELECT/WITH，READ ONLY 事务 + statement_timeout + 行数上限；系统表黑名单；
  own_customers_only 账号整体禁用（行级过滤无法在自由 SQL 上保证）。
- /scripts 白名单脚本：管理员维护，服务端以后端自身 DB 连接执行（写库凭据不出服务器），
  required_action 绑定既有动作键；全部 sys_access_log 审计。
- /dsn：只读角色连接串（DSH_RO_DSN），仅 action_agent_dsn_ro 账号可领，不做字段脱敏。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import permissions, security
from app.agent import tools as agent_tools
from app.config import get_settings
from app.db import engine, get_db
from app.models.system import SysDshScript
from app.security import UserContext, get_current_user_context, record_access_log, require_action, require_page

router = APIRouter(prefix="/agent", tags=["agent-data"],
                   dependencies=[Depends(require_page("page_chat"))])

# 助手不可触达的表（值级敏感：口令散列/审计/凭据/原始文件/脚本源码）
BLOCKED_TABLES: frozenset[str] = frozenset({
    "sys_user", "sys_access_log", "sys_security_events", "sys_role_template", "sys_raw_file",
    "sys_audit_log", "sys_dsh_script", "alembic_version", "business_file", "business_file_chunk",
    "chat_session", "chat_message", "agent_file",
})
_BLOCKED_PREFIXES = ("business_file", "pg_", "chat_")

# call 白名单：后端 agent 工具注册表里的只读/文件类工具（写库工具不存在于该注册表）
CALL_WHITELIST: frozenset[str] = frozenset({
    "search_parts", "get_part_overview", "lookup_prices_bulk", "list_recent_purchases",
    "get_profit_ranking", "get_purchase_analysis", "get_inventory", "get_maintenance_board",
    "get_maintenance_projects", "get_maintenance_lines", "get_cancellation_stats",
    "inspect_file", "read_file_rows", "read_document", "write_excel", "write_report",
    "list_skills", "get_skill",
})

_FORBIDDEN_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|execute|lock|vacuum|"
    r"set|refresh|merge|reindex|cluster|notify|listen|unlisten|load|prepare|deallocate|declare|move|import|"
    r"do|begin|commit|rollback|savepoint|release|abort|checkpoint|discard|reset|comment|security|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|lo_export|dblink|pg_sleep)\b",
    re.I,
)
_SCRIPT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _jsonable(data: Any) -> Any:
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def _strip_sql(sql: str) -> str:
    """长度保持的 SQL 骨架：字符串字面量内容与注释替换为等长空格（引号本身保留），
    便于按位置回切原文，且关键字/表名检查不会被字面量或注释误导。"""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            out.append("'" + " " * max(0, end - i - 2) + ("'" if end - i >= 2 else ""))
            i = end
            continue
        if ch == '"':
            j = sql.find('"', i + 1)
            end = n if j == -1 else j + 1
            out.append(" " + sql[i + 1:end - 1] + " ")
            i = end
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            end = n if j == -1 else j
            out.append(" " * (end - i))
            i = end
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            end = n if j == -1 else j + 2
            out.append(" " * (end - i))
            i = end
            continue
        if ch == "$":
            # 拒绝 $$ 美元引用（可藏 DO 块）
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "SQL 不允许使用 $ 引用")
        out.append(ch)
        i += 1
    return "".join(out)


def validate_readonly_sql(sql: str) -> str:
    """校验并返回规范化后的单条只读 SQL（去掉尾部注释与分号）；不合规抛 400。"""
    if not isinstance(sql, str) or sql.strip() == "":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sql 不能为空")
    if len(sql) > 20000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SQL 过长")
    skeleton = _strip_sql(sql)
    trimmed = skeleton.rstrip()
    if trimmed.endswith(";"):
        trimmed = trimmed[:-1]
    if ";" in trimmed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只允许单条语句")
    body = sql[:len(trimmed)].strip()
    skeleton = trimmed
    first = skeleton.lstrip().split(None, 1)[0].lower() if skeleton.strip() else ""
    if first not in {"select", "with"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只允许 SELECT / WITH 只读查询")
    m = _FORBIDDEN_WORDS.search(skeleton)
    if m:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"只读通道禁止语句/函数：{m.group(1).upper()}")
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", skeleton)}
    hit = sorted(w for w in words if w in BLOCKED_TABLES or w.startswith(_BLOCKED_PREFIXES))
    if hit:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"不允许访问敏感/系统表：{', '.join(hit)}")
    if body == "":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sql 不能为空")
    return body


def _extra_hidden_columns(ctx: UserContext) -> tuple[str, ...]:
    """自由 SQL 的列名不受 FIELD_GROUPS 精确键约束：按可见组名做子串兜底遮蔽。"""
    hidden = permissions.hidden_groups(ctx.permissions if ctx.permissions is not None else permissions.template_for(ctx.role))
    needles: list[str] = []
    if "supplier_info" in hidden:
        needles += ["supplier", "source_channel", "channel"]
    if "customer_info" in hidden:
        needles += ["customer", "end_customer"]
    if "supplier_info" in hidden or "customer_info" in hidden:
        needles += ["name_raw", "name_normalized"]
    if "purchase_cost" in hidden:
        needles += ["cost", "unit_price", "price", "amount"]
    if "profit_amount" in hidden or "profit_rate" in hidden:
        needles += ["profit", "margin"]
    if "pool_price_governance" in hidden:
        needles += ["ceiling", "floor", "policy"]
    return tuple(needles)


def _mask_rows(rows: list[dict], ctx: UserContext) -> list[dict]:
    rows = security.apply_field_visibility(rows, ctx)
    needles = _extra_hidden_columns(ctx)
    if not needles:
        return rows
    from app import config as _cfg
    out = []
    for r in rows:
        out.append({k: (_cfg.MASK_VALUE if any(nd in k.lower() for nd in needles) else v) for k, v in r.items()})
    return out


# ---------- schema ----------
_schema_cache: dict[str, Any] = {"at": 0.0, "data": None}
_SCHEMA_TTL = 600


def _load_schema() -> dict:
    with engine.connect() as conn:
        tables = conn.execute(text("""
            SELECT c.relname AS name, obj_description(c.oid, 'pg_class') AS comment
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm')
            ORDER BY c.relname
        """)).mappings().all()
        cols = conn.execute(text("""
            SELECT c.table_name, c.column_name, c.data_type, c.is_nullable,
                   col_description(format('%I.%I', c.table_schema, c.table_name)::regclass::oid, c.ordinal_position) AS comment
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
            ORDER BY c.table_name, c.ordinal_position
        """)).mappings().all()
    by_table: dict[str, list[dict]] = {}
    for c in cols:
        by_table.setdefault(c["table_name"], []).append({
            "name": c["column_name"], "type": c["data_type"], "nullable": c["is_nullable"] == "YES",
            "comment": c["comment"],
        })
    out = []
    for t in tables:
        name = t["name"]
        if name in BLOCKED_TABLES or name.startswith(_BLOCKED_PREFIXES):
            continue
        out.append({"name": name, "comment": t["comment"], "columns": by_table.get(name, [])})
    return {"table_count": len(out), "tables": out}


@router.get("/schema")
def get_schema(refresh: bool = False, ctx: UserContext = Depends(get_current_user_context),
               _: None = Depends(require_action("action_agent_sql"))) -> dict:
    """业务表结构快照（不含敏感系统表；10 分钟缓存）。"""
    now = time.time()
    if refresh or _schema_cache["data"] is None or now - _schema_cache["at"] > _SCHEMA_TTL:
        _schema_cache["data"] = _load_schema()
        _schema_cache["at"] = now
    record_access_log(ctx, "agent_schema", "agent")
    return _schema_cache["data"]


# ---------- sql ----------
class SqlRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=20000)
    max_rows: int = Field(100, ge=1, le=5000)


@router.post("/sql")
def run_sql(req: SqlRequest, ctx: UserContext = Depends(get_current_user_context),
            _: None = Depends(require_action("action_agent_sql"))) -> dict:
    """text2sql 只读执行口：READ ONLY 事务 + 超时 + 行数上限 + 字段脱敏 + 审计。"""
    settings = get_settings()
    if security.is_scoped_sales(ctx):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "行级收紧账号（只看自己成交的客户）不能使用自由 SQL，请改用带行级过滤的业务查询工具")
    body = validate_readonly_sql(req.sql)
    max_rows = min(req.max_rows, max(1, settings.agent_sql_max_rows))
    wrapped = f"SELECT * FROM (\n{body}\n) AS _agent_q LIMIT {max_rows + 1}"
    record_access_log(ctx, "agent_sql", body[:500], {"max_rows": max_rows})
    started = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {int(settings.agent_sql_timeout_seconds) * 1000}"))
            result = conn.execute(text(wrapped))
            columns = list(result.keys())
            raw = [dict(zip(columns, row)) for row in result.fetchall()]
            conn.rollback()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— 数据库错误脱敏：只回简短原因
        msg = str(getattr(exc, "orig", exc)).splitlines()[0][:300]
        if "statement timeout" in msg.lower():
            raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, "查询超时，请加 WHERE 收窄范围") from exc
        if "read-only" in msg.lower():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "只读通道拒绝写操作") from exc
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"SQL 执行失败：{msg}") from exc
    truncated = len(raw) > max_rows
    rows = _mask_rows(_jsonable(raw[:max_rows]), ctx)
    return {
        "columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


# ---------- call ----------
class CallRequest(BaseModel):
    tool: str = Field(..., min_length=1, max_length=64)
    args: dict = Field(default_factory=dict)


@router.post("/call")
def call_tool(req: CallRequest, db: Session = Depends(get_db),
              ctx: UserContext = Depends(get_current_user_context)) -> dict:
    """业务工具白名单（复用后端 agent 工具集：全量业务层权限过滤，含行级客户匿名化）。"""
    if req.tool not in CALL_WHITELIST:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知或不允许的工具：{req.tool}（可用：{', '.join(sorted(CALL_WHITELIST))}）")
    if not ctx.is_authenticated and ctx.role != "admin" and security.config.ENABLE_RBAC:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    return agent_tools.dispatch(db, req.tool, req.args or {}, ctx)


# ---------- whitelist scripts ----------
class ScriptBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    content: str = Field(..., min_length=1, max_length=200_000)
    required_action: str | None = None
    timeout_seconds: int = Field(60, ge=5, le=600)
    enabled: bool = True


class ScriptRunBody(BaseModel):
    args: dict = Field(default_factory=dict)


def _script_view(s: SysDshScript, *, with_content: bool) -> dict:
    v = {
        "name": s.name, "description": s.description or "", "required_action": s.required_action,
        "timeout_seconds": s.timeout_seconds, "enabled": s.enabled,
        "created_by": s.created_by, "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_by": s.updated_by, "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
    if with_content:
        v["content"] = s.content
    return v


def _require_admin_ctx(ctx: UserContext) -> None:
    if ctx.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可维护白名单脚本")


def _validate_script(body: ScriptBody) -> None:
    if not _SCRIPT_NAME.match(body.name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "脚本名只能是小写字母/数字/_/-，且以字母或数字开头")
    if body.required_action is not None and body.required_action.strip() != "":
        if body.required_action not in permissions.ALL_KEYS or not body.required_action.startswith("action_"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"required_action 必须是已存在的 action_* 权限键：{body.required_action}")


@router.get("/scripts")
def list_scripts(db: Session = Depends(get_db), ctx: UserContext = Depends(get_current_user_context)) -> dict:
    if security.config.ENABLE_RBAC and not ctx.is_authenticated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    rows = db.scalars(select(SysDshScript).order_by(SysDshScript.name)).all()
    admin = ctx.role == "admin"
    return {"scripts": [_script_view(s, with_content=admin) for s in rows if admin or s.enabled]}


@router.post("/scripts")
def create_script(body: ScriptBody, db: Session = Depends(get_db),
                  ctx: UserContext = Depends(get_current_user_context)) -> dict:
    _require_admin_ctx(ctx)
    _validate_script(body)
    if db.scalar(select(SysDshScript).where(SysDshScript.name == body.name)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "同名脚本已存在")
    s = SysDshScript(name=body.name, description=body.description, content=body.content,
                     required_action=(body.required_action or None) or None,
                     timeout_seconds=body.timeout_seconds, enabled=body.enabled, created_by=ctx.user_id)
    db.add(s)
    db.commit()
    db.refresh(s)
    record_access_log(ctx, "agent_script_create", body.name)
    return _script_view(s, with_content=True)


@router.put("/scripts/{name}")
def update_script(name: str, body: ScriptBody, db: Session = Depends(get_db),
                  ctx: UserContext = Depends(get_current_user_context)) -> dict:
    _require_admin_ctx(ctx)
    _validate_script(body)
    s = db.scalar(select(SysDshScript).where(SysDshScript.name == name))
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "脚本不存在")
    s.description = body.description
    s.content = body.content
    s.required_action = (body.required_action or None) or None
    s.timeout_seconds = body.timeout_seconds
    s.enabled = body.enabled
    s.updated_by = ctx.user_id
    s.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(s)
    record_access_log(ctx, "agent_script_update", name)
    return _script_view(s, with_content=True)


@router.delete("/scripts/{name}")
def delete_script(name: str, db: Session = Depends(get_db),
                  ctx: UserContext = Depends(get_current_user_context)) -> dict:
    _require_admin_ctx(ctx)
    s = db.scalar(select(SysDshScript).where(SysDshScript.name == name))
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "脚本不存在")
    db.delete(s)
    db.commit()
    record_access_log(ctx, "agent_script_delete", name)
    return {"ok": True}


def _libpq_dsn(url: str) -> str:
    """SQLAlchemy URL → libpq/psycopg 直连串（脚本用 psycopg.connect(os.environ['ITD_DB_URL'])）。"""
    return re.sub(r"^postgresql\+[a-z0-9_]+://", "postgresql://", url)


@router.post("/scripts/{name}/run")
def run_script(name: str, body: ScriptRunBody, db: Session = Depends(get_db),
               ctx: UserContext = Depends(get_current_user_context)) -> dict:
    """执行白名单脚本：服务端 python，凭据只在服务端环境变量中；required_action 门控；超时终止。"""
    if security.config.ENABLE_RBAC and not ctx.is_authenticated and ctx.role != "admin":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    s = db.scalar(select(SysDshScript).where(SysDshScript.name == name))
    if s is None or not s.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "脚本不存在或已停用")
    if s.required_action:
        perms = ctx.permissions if ctx.permissions is not None else permissions.template_for(ctx.role)
        allowed = ctx.role == "admin" and s.required_action not in permissions.ACCOUNT_SCOPED_ACTION_KEYS
        if not allowed and not bool(perms.get(s.required_action)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"执行该脚本需要权限 {s.required_action}")
    settings = get_settings()
    record_access_log(ctx, "agent_script_run", name, {"args": _jsonable(body.args)[:1] if isinstance(body.args, list) else None})
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONIOENCODING": "utf-8",
        "ITD_DB_URL": _libpq_dsn(settings.database_url),
        "ITD_USER": ctx.user_id or "",
        "ITD_ROLE": ctx.role or "",
        "ITD_ARGS_JSON": json.dumps(body.args or {}, ensure_ascii=False),
    }
    with tempfile.TemporaryDirectory(prefix="itd_script_") as tmp:
        path = os.path.join(tmp, f"{name}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(s.content)
        started = time.perf_counter()
        try:
            proc = subprocess.run([settings.agent_script_python, path], env=env, cwd=tmp,
                                  capture_output=True, text=True, timeout=s.timeout_seconds)
            out = {"ok": proc.returncode == 0, "returncode": proc.returncode,
                   "stdout": proc.stdout[-200_000:], "stderr": proc.stderr[-50_000:]}
        except subprocess.TimeoutExpired as exc:
            out = {"ok": False, "returncode": None, "stdout": (exc.stdout or "")[-200_000:] if isinstance(exc.stdout, str) else "",
                   "stderr": f"脚本超过 {s.timeout_seconds}s 已终止", "timeout": True}
        except FileNotFoundError:
            raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"服务器未安装 {settings.agent_script_python}")
    out["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    out["script"] = name
    return out


# ---------- read-only DSN ----------
@router.get("/dsn")
def get_dsn(ctx: UserContext = Depends(get_current_user_context),
            _: None = Depends(require_action("action_agent_dsn_ro"))) -> dict:
    settings = get_settings()
    if not settings.dsh_ro_dsn:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "部署未配置只读 DSN（DSH_RO_DSN）")
    record_access_log(ctx, "agent_dsn_ro", "agent")
    return {"dsn": settings.dsh_ro_dsn, "user": ctx.user_id, "role": ctx.role}
