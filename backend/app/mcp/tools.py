"""Explicit tool registry. No arbitrary SQL, HTTP, filesystem path or shell tool."""

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.db import SessionLocal
from app.mcp import workflows as wf
from app.mcp.core import (
    Actor,
    McpError,
    audit,
    digest,
    event,
    get_settings,
    jsonable_encoder,
    now,
    owned,
    permissions,
    public_url,
    record,
    refresh_actor,
    require,
    text,
    timedelta,
    uid,
)
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectXsdd
from app.models.mcp import McpAuditEvent, McpRecord
from app.security import apply_field_visibility
from app.services import inventory, part_overview

REGISTRY = {
    r["name"]: r for r in json.loads(Path(__file__).with_name("tools.json").read_text())
}
PAGES = {
    "pf_search_projects": "page_maintenance",
    "pf_get_project_materials": "page_maintenance",
    "pf_get_inventory": "page_inventory",
}


def tool_allowed(actor, name):
    try:
        require(actor, REGISTRY[name]["scope"], PAGES.get(name))
        return True
    except McpError:
        return False


def check_record_scope(db, actor, r):
    payload = r.payload
    if "actor" in payload:
        original = Actor(
            actor.name,
            actor.ctx,
            set(payload["actor"]["scopes"]),
            payload["actor"]["version"],
            payload["actor"].get("credential_id"),
        )
        refresh_actor(db, original)
    args = payload.get("args", payload)
    if args.get("family"):
        wf.family_access(db, actor, args["family"], args.get("project_id"))
    if args.get("export_kind"):
        wf.project_access(
            db,
            actor,
            (args.get("project_ids") or [args.get("project_id")])[0],
            expense_data=args["export_kind"] == "expense_collection",
        )


def job_payload(r):
    data = {k: v for k, v in r.payload.items() if k in ("result", "error", "attempt")}
    return dict(job_id=r.id, status=r.state, next_poll_after_seconds=3, **data)


def dispatch(db, actor, name, args):
    if name == "pf_get_capabilities":
        families = {}
        for f in wf.FAMILIES:
            if f == "legacy_trade":
                allowed = permissions.page_permission_allowed(
                    role=actor.ctx.role,
                    permission_map=actor.ctx.permissions,
                    page_key="page_import",
                )
            else:
                allowed = permissions.page_permission_allowed(
                    role=actor.ctx.role,
                    permission_map=actor.ctx.permissions,
                    page_key="page_maintenance",
                )
                if f == "expense_collection":
                    allowed = (
                        allowed
                        and get_settings().maintenance_boss_dashboard_enabled
                        and (
                            actor.ctx.role == "admin"
                            or bool(actor.ctx.permissions.get("data_profit"))
                        )
                    )
            if allowed:
                can_preview = (
                    "mcp.import.preview" in actor.scopes
                    and "mcp.files.read" in actor.scopes
                    and (f == "legacy_trade" or "mcp.projects.read" in actor.scopes)
                )
                if f == "expense_collection" and actor.ctx.role != "admin":
                    can_preview = can_preview and bool(
                        actor.ctx.permissions.get(
                            "action_maintenance_expense_collection_upload"
                        )
                    )
                families[f] = {
                    "preview": can_preview,
                    "apply": can_preview
                    and f != "legacy_trade"
                    and get_settings().mcp_confirm_enabled,
                    "export": f != "legacy_trade"
                    and "mcp.export" in actor.scopes
                    and "mcp.projects.read" in actor.scopes,
                    "confirmation": "authenticated_web",
                }
        return {
            "actor_display": actor.name,
            "enabled_tools": [n for n in REGISTRY if tool_allowed(actor, n)],
            "families": families,
            "capability_version": "1.0",
            "limits": {"files": 10, "file_bytes": get_settings().mcp_max_file_bytes},
            "disabled_reasons": {
                "direct_mcp_write": "正式确认仅通过网页",
                "legacy_trade_apply": "首版仅预检",
            },
        }
    if name == "pf_create_upload_session":
        files = args["files"]
        if any(f["size_bytes"] > get_settings().mcp_max_file_bytes for f in files):
            raise McpError("file_too_large", "文件超出限制", 413)
        # Bounded retained sessions per employee; retries reuse the original session.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:quota_key, 0))"),
            {"quota_key": "mcp-upload-quota:" + actor.name},
        )
        existing = db.scalar(
            select(McpRecord.id).where(
                McpRecord.kind == "upload",
                McpRecord.owner == actor.name,
                McpRecord.key == args["idempotency_key"],
            )
        )
        active = db.scalar(
            select(func.count())
            .select_from(McpRecord)
            .where(
                McpRecord.kind == "upload",
                McpRecord.owner == actor.name,
                McpRecord.expires_at > now(),
            )
        )
        if not existing and active >= 10:
            raise McpError("rate_limited", "今日暂存会话已达上限", 429)
        if sum(f["size_bytes"] for f in files) > 100 * 1024 * 1024:
            raise McpError("file_too_large", "单批文件超过100MiB", 413)
        for f in files:
            if (
                Path(f["name"]).suffix.lower()
                not in (
                    ".xlsx",
                    ".pdf",
                    ".docx",
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".txt",
                    ".csv",
                )
                or "/" in f["name"]
                or "\\" in f["name"]
                or any(ord(c) < 32 for c in f["name"])
            ):
                raise McpError("invalid_filename", "文件名或类型不支持")
        r = record(
            db,
            actor,
            "upload",
            {
                "entries": [
                    {**f, "entry_id": uid(), "state": "pending"} for f in files
                ],
                "purpose": args["purpose"],
                "auth_version": actor.version,
                "actor": actor.snapshot(),
            },
            key=args["idempotency_key"],
            request=args,
        )
        return {
            "upload_session_id": r.id,
            "entries": r.payload["entries"],
            "upload_page_url": public_url("/mcp-office?upload=" + r.id),
            "expires_at": r.expires_at.isoformat(),
        }
    if name == "pf_get_upload_session":
        r = owned(db, actor, args["upload_session_id"], "upload")
        check_record_scope(db, actor, r)
        return {
            "upload_session_id": r.id,
            "entries": r.payload["entries"],
            "complete": all(e["state"] == "ready" for e in r.payload["entries"]),
        }
    if name == "pf_inspect_document":
        if args.get("include_sample"):
            raise McpError(
                "unsupported_option", "首版只返回结构和分类，不返回任意原始样本"
            )
        return wf.inspect_document(db, actor, args["file_id"])
    if name == "pf_search_projects":
        q = args["query"]
        limit = args.get("limit", 50)
        if args.get("cursor"):
            raise McpError("unsupported_option", "请缩小项目检索条件")
        scope = resolve_visible_project_ids(db, actor.ctx)
        stmt = select(MaintenanceProject).where(
            MaintenanceProject.is_active.is_(True),
            (
                MaintenanceProject.display_name.ilike("%" + q + "%")
                | MaintenanceProject.project_code.ilike("%" + q + "%")
                | MaintenanceProject.project_id.in_(
                    select(MaintenanceProjectXsdd.project_id).where(
                        MaintenanceProjectXsdd.xsdd_norm.ilike("%" + q + "%")
                    )
                )
            ),
        )
        if scope is not None:
            stmt = stmt.where(MaintenanceProject.project_id.in_(scope))
        rows = list(
            db.scalars(stmt.order_by(MaintenanceProject.project_id).limit(limit + 1))
        )
        return {
            "items": [
                {
                    "project_id": r.project_id,
                    "project_code": r.project_code,
                    "display_name": r.display_name,
                }
                for r in rows[:limit]
            ],
            "truncated": len(rows) > limit,
            "warning": "名称相同不代表同一项目；使用稳定项目编号",
        }
    if name in ("pf_preview_import", "pf_create_export"):
        if name == "pf_preview_import":
            require(actor, "mcp.files.read")
            wf.family_access(db, actor, args["family"], args.get("project_id"))
            for fid in args["file_ids"]:
                owned(db, actor, fid, "file")
        else:
            kind = args["export_kind"]
            if kind not in ("acceptance_checklist", "expense_collection"):
                raise McpError("unsupported_family", "导出模板未开放")
            wf.project_access(
                db,
                actor,
                (args.get("project_ids") or [None])[0],
                expense_data=kind == "expense_collection",
            )
        old = db.scalar(
            select(McpRecord).where(
                McpRecord.owner == actor.name,
                McpRecord.kind == "job",
                McpRecord.key == name + ":" + args["idempotency_key"],
            )
        )
        if old:
            if old.request_hash != digest(args):
                raise McpError("idempotency_conflict", "同一请求标识对应不同内容", 409)
            if old.expires_at <= now():
                raise McpError("expired_request", "原任务已过期", 409)
            return job_payload(old)
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('mcp-queue',0))")
        )
        pending = db.scalar(
            select(func.count())
            .select_from(McpRecord)
            .where(McpRecord.kind == "job", McpRecord.state.in_(["queued", "running"]))
        )
        if pending >= 20:
            raise McpError("rate_limited", "任务队列已满，请稍后重试", 429)
        r = record(
            db,
            actor,
            "job",
            {
                "args": args,
                "task": "preview" if name == "pf_preview_import" else "export",
                "actor": actor.snapshot(),
            },
            state="queued",
            key=name + ":" + args["idempotency_key"],
            request=args,
            hours=24 * 7,
        )
        return job_payload(r)
    if name == "pf_get_job":
        r = owned(db, actor, args["job_id"], "job")
        check_record_scope(db, actor, r)
        return job_payload(r)
    if name in ("pf_get_import_preview", "pf_open_review"):
        r = owned(db, actor, args["preview_id"], "preview", expired=True)
        if r.expires_at <= now() and r.state != "applied":
            raise McpError("not_found", "预览已过期，请重新生成", 404)
        check_record_scope(db, actor, r)
        if name == "pf_open_review":
            return {
                "preview_id": r.id,
                "review_url": public_url("/mcp-office?review=" + r.id),
                "requires_human_confirmation": True,
                "expires_at": r.expires_at.isoformat(),
            }
        payload = {
            k: v
            for k, v in r.payload.items()
            if k not in ("actor", "auth_version", "native_batch_id", "plan_hash")
        }
        section = args.get("section")
        if section:
            payload = {
                k: v
                for k, v in payload.items()
                if k not in ("changes", "issues") or k == section
            }
        if r.state == "applied":
            receipt = db.scalar(
                select(McpRecord).where(
                    McpRecord.kind == "receipt",
                    McpRecord.owner == actor.name,
                    McpRecord.key == r.id,
                )
            )
            payload["receipt"] = receipt.payload if receipt else None
        return dict(
            preview_id=r.id, status=r.state, preview_hash=digest(r.payload), **payload
        )
    if name == "pf_get_export_options":
        result = []
        if args.get("project_id"):
            wf.project_access(db, actor, args["project_id"])
        if permissions.page_permission_allowed(
            role=actor.ctx.role,
            permission_map=actor.ctx.permissions,
            page_key="page_maintenance",
        ):
            result.append(
                {
                    "export_kind": "acceptance_checklist",
                    "roundtrip": True,
                    "scope": "one_project",
                    "mutation_semantics": "整表替换",
                }
            )
            if get_settings().maintenance_boss_dashboard_enabled and (
                actor.ctx.role == "admin" or actor.ctx.permissions.get("data_profit")
            ):
                result.append(
                    {
                        "export_kind": "expense_collection",
                        "roundtrip": True,
                        "scope": "one_project",
                        "mutation_semantics": "依原模板操作列与版本校验",
                    }
                )
        return {
            "export_kinds": result,
            "required_parameters": ["project_ids (exactly one)"],
        }
    if name == "pf_get_download":
        r = wf.check_artifact(db, actor, args["artifact_id"])
        return {
            "artifact_id": r.id,
            "download_url": public_url("/mcp-office?download=" + r.id),
            "filename": r.payload["name"],
            "sha256": r.payload["sha256"],
            "size_bytes": r.payload["size_bytes"],
            "expires_at": r.expires_at.isoformat(),
        }
    if name == "pf_get_project_materials":
        wf.project_access(db, actor, args["project_id"])
        return {
            "acceptance": wf.checklist.project_checklist(db, args["project_id"]),
            "unknowns": ["当前仅核对已录入验收清单，不代表全部项目材料完整"],
        }
    if name == "pf_search_parts":
        if args.get("cursor"):
            raise McpError("unsupported_option", "请缩小PN检索条件")
        data = part_overview.search_parts(
            db, args["query"], 1, min(args.get("limit", 50), 100), actor.ctx
        )
        keys = {
            "id",
            "part_id",
            "pn_std",
            "pn",
            "description",
            "category_major",
            "category_minor",
            "brand",
            "part_type",
            "specs",
            "needs_review",
        }
        return {
            "items": [
                {k: v for k, v in row.items() if k in keys}
                for row in data.get("items", [])
            ],
            "total": data.get("total"),
            "as_of": now().isoformat(),
        }
    if name == "pf_get_inventory":
        data = inventory.list_inventory(
            db, args.get("warehouse"), args["query"], args.get("page", 1), 50, actor.ctx
        )
        for row in data.get("items", []):
            row.pop("unit_cost", None)
            row.pop("inventory_value", None)
        return {
            "data": apply_field_visibility(data, actor.ctx),
            "quantity_basis": "系统分仓库存快照/人工覆盖口径",
            "as_of": now().isoformat(),
        }
    if name == "pf_search_audit":
        start = date.fromisoformat(args["date_from"])
        end = date.fromisoformat(args["date_to"])
        if not 0 <= (end - start).days <= 30:
            raise McpError("invalid_range", "审计查询每次最多31天")
        if args.get("actor_id") not in (None, actor.name) and actor.ctx.role != "admin":
            raise McpError("permission_denied", "仅能查看本人审计", 403)
        stmt = select(McpAuditEvent).where(
            McpAuditEvent.owner == args.get("actor_id", actor.name),
            McpAuditEvent.created_at
            >= datetime.combine(
                start, datetime.min.time(), tzinfo=ZoneInfo("Asia/Shanghai")
            ),
            McpAuditEvent.created_at
            < datetime.combine(
                end, datetime.min.time(), tzinfo=ZoneInfo("Asia/Shanghai")
            )
            + timedelta(days=1),
        )
        if args.get("cursor"):
            raise McpError("unsupported_option", "请缩小时间范围")
        if args.get("tool_name"):
            stmt = stmt.where(McpAuditEvent.operation == args["tool_name"])
        if args.get("operation_id"):
            stmt = stmt.where(McpAuditEvent.request_id == args["operation_id"])
        if args.get("project_id"):
            stmt = stmt.where(
                McpAuditEvent.detail["project_id"].astext == args["project_id"]
            )
        limit = args.get("limit", 50)
        rows = list(
            db.scalars(
                stmt.order_by(McpAuditEvent.created_at.desc(), McpAuditEvent.id)
                .offset((args.get("page", 1) - 1) * limit)
                .limit(limit + 1)
            )
        )
        return {
            "page": args.get("page", 1),
            "has_more": len(rows) > limit,
            "timezone": "Asia/Shanghai",
            "events": [
                {
                    "event_id": r.id,
                    "request_id": r.request_id,
                    "owner": r.owner,
                    "operation": r.operation,
                    "stage": r.stage,
                    "detail": r.detail,
                    "at": r.created_at.isoformat(),
                }
                for r in rows[:limit]
            ],
        }
    raise McpError("unsupported_tool", "工具尚未开放")


def invoke(actor, name, args):
    if name not in REGISTRY:
        raise McpError("unknown_tool", "未知工具")
    import jsonschema

    try:
        jsonschema.validate(
            args,
            REGISTRY[name]["inputSchema"],
            format_checker=jsonschema.FormatChecker(),
        )
    except jsonschema.ValidationError as exc:
        raise McpError("invalid_arguments", "工具参数不符合格式") from exc
    request_detail = {
        k: v
        for k, v in args.items()
        if k
        in (
            "family",
            "project_id",
            "file_id",
            "preview_id",
            "job_id",
            "artifact_id",
            "export_kind",
            "date_from",
            "date_to",
        )
    }
    request_detail["arguments_hash"] = digest(args)
    rid = audit(actor, name, "requested", request_detail)
    try:
        with SessionLocal() as db:
            db.execute(text("SET LOCAL statement_timeout = '25s'"))
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            actor = refresh_actor(db, actor)
            require(actor, REGISTRY[name]["scope"], PAGES.get(name))
            result = dispatch(db, actor, name, args)
            rows = (
                result.get("items")
                or result.get("events")
                or (result.get("data") or {}).get("items", [])
            )
            refs = [
                {
                    k: row[k]
                    for k in (
                        "id",
                        "part_id",
                        "project_id",
                        "file_id",
                        "preview_id",
                        "event_id",
                    )
                    if row.get(k) is not None
                }
                for row in rows[:200]
                if isinstance(row, dict)
            ]
            event(
                db,
                actor,
                name,
                "completed",
                {
                    "result_hash": digest(result),
                    "schema_version": "1.0",
                    "result_refs": [ref for ref in refs if ref],
                    **{
                        k: v
                        for k, v in args.items()
                        if k
                        in (
                            "family",
                            "project_id",
                            "project_ids",
                            "file_id",
                            "file_ids",
                            "preview_id",
                            "job_id",
                            "artifact_id",
                            "export_kind",
                        )
                    },
                    **{
                        k: v
                        for k, v in result.items()
                        if k
                        in (
                            "upload_session_id",
                            "job_id",
                            "artifact_id",
                            "file_id",
                            "preview_id",
                            "project_id",
                        )
                    },
                },
                rid,
            )
            db.commit()
        return {
            "schema_version": "1.0",
            "request_id": rid,
            "data": jsonable_encoder(result),
        }
    except Exception as exc:
        code = exc.code if isinstance(exc, McpError) else "operation_failed"
        audit(actor, name, "failed", {"code": code}, rid)
        raise
