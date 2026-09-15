"""Authenticated file data plane and human review UI; PAT cannot confirm writes."""

import hashlib
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.db import SessionLocal
from app.mcp import workflows as wf
from app.mcp.core import (
    SCOPES,
    Actor,
    McpError,
    audit,
    authenticate,
    enabled,
    event,
    get_settings,
    now,
    owned,
    record,
    refresh_actor,
    require,
    uid,
)
from app.mcp.tools import check_record_scope, invoke
from app.models.mcp import McpCredential

router = APIRouter()


def origin_check(request):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != get_settings().mcp_public_base_url.rstrip("/"):
        raise McpError("invalid_origin", "请求来源不允许", 403)


def web_actor(request: Request):
    origin_check(request)
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise McpError("unauthenticated", "请先登录", 401)
    return authenticate(value[7:], allow_jwt=True)


def jwt_actor(request: Request):
    actor = web_actor(request)
    if actor.credential_id:
        raise McpError(
            "human_login_required", "此操作需要业务网页登录，MCP 令牌不可确认", 403
        )
    return actor


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopes: list[str] = Field(min_length=1, max_length=20)
    days: int = Field(default=30, ge=1, le=30)


@router.post("/api/mcp-office/credentials")
def create_credential(
    body: CredentialInput, actor: Annotated[Actor, Depends(jwt_actor)]
):
    if not set(body.scopes) <= SCOPES:
        raise McpError("invalid_scope", "包含未知授权范围")
    value = "pfm_" + secrets.token_urlsafe(32)
    with SessionLocal() as db:
        actor = refresh_actor(db, actor)
        row = McpCredential(
            id=uid(),
            owner=actor.name,
            scopes=body.scopes,
            auth_version=actor.version,
            token_hash=hashlib.sha256(value.encode()).hexdigest(),
            expires_at=now() + timedelta(days=body.days),
        )
        db.add(row)
        event(
            db,
            actor,
            "credential_create",
            "completed",
            {"credential_id": row.id, "scopes": body.scopes},
        )
        db.commit()
        return {
            "credential_id": row.id,
            "token": value,
            "expires_at": row.expires_at.isoformat(),
        }


@router.get("/api/mcp-office/credentials")
def credentials(actor: Annotated[Actor, Depends(jwt_actor)]):
    with SessionLocal() as db:
        rows = db.scalars(
            select(McpCredential)
            .where(McpCredential.owner == actor.name)
            .order_by(McpCredential.created_at.desc())
        )
        return {
            "credentials": [
                {
                    "id": r.id,
                    "scopes": r.scopes,
                    "expires_at": r.expires_at.isoformat(),
                    "revoked": bool(r.revoked_at),
                }
                for r in rows
            ]
        }


@router.delete("/api/mcp-office/credentials/{credential_id}")
def revoke(credential_id: str, actor: Annotated[Actor, Depends(jwt_actor)]):
    with SessionLocal() as db:
        r = db.get(McpCredential, credential_id)
        if not r or r.owner != actor.name:
            raise McpError("not_found", "授权不存在", 404)
        r.revoked_at = now()
        event(db, actor, "credential_revoke", "completed", {"credential_id": r.id})
        db.commit()
    return {"revoked": True}


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=80)
    arguments: dict


@router.post("/api/mcp-office/tools")
def office_tool(body: ToolInput, actor: Annotated[Actor, Depends(web_actor)]):
    return invoke(actor, body.name, body.arguments)


@router.put("/api/mcp-office/uploads/{session_id}/{entry_id}")
async def upload(
    session_id: str,
    entry_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(web_actor)],
):
    require(actor, "mcp.files.upload")
    rid = await run_in_threadpool(
        audit, actor, "file_upload", "requested", {"upload_session_id": session_id}
    )

    def authorize_entry():
        with SessionLocal() as db:
            current = refresh_actor(db, actor)
            session = owned(db, current, session_id, "upload")
            check_record_scope(db, current, session)
            entry = next(
                (e for e in session.payload["entries"] if e["entry_id"] == entry_id),
                None,
            )
            if not entry:
                raise McpError("not_found", "上传条目不存在", 404)
            return entry["size_bytes"]

    try:
        expected = await run_in_threadpool(authorize_entry)
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > min(expected, get_settings().mcp_max_file_bytes):
                raise McpError("file_too_large", "文件超出申报大小", 413)
            chunks.append(chunk)
    except Exception as exc:
        await run_in_threadpool(
            audit,
            actor,
            "file_upload",
            "failed",
            {"code": getattr(exc, "code", "upload_interrupted")},
            rid,
        )
        raise
    data = b"".join(chunks)

    def finish():
        with SessionLocal() as db:
            current = refresh_actor(db, actor)
            session = owned(db, current, session_id, "upload", lock=True)
            check_record_scope(db, current, session)
            if session.payload["auth_version"] != current.version:
                raise McpError("permission_denied", "上传会话授权已失效", 403)
            entries = [dict(e) for e in session.payload["entries"]]
            entry = next((e for e in entries if e["entry_id"] == entry_id), None)
            if not entry:
                raise McpError("not_found", "上传条目不存在", 404)
            sha = hashlib.sha256(data).hexdigest()
            if entry["state"] == "ready":
                if entry["sha256"] != sha:
                    raise McpError("idempotency_conflict", "该条目已上传不同内容", 409)
                event(
                    db,
                    current,
                    "file_upload",
                    "replayed",
                    {"file_id": entry["file_id"], "sha256": sha},
                    rid,
                )
                db.commit()
                return entry
            if size != entry["size_bytes"]:
                raise McpError("invalid_size", "文件大小与上传清单不符")
            if entry["name"].lower().endswith(".xlsx"):
                wf.safe_xlsx(data)
            r = record(
                db,
                current,
                "file",
                {"name": entry["name"], "size_bytes": size, "sha256": sha},
                hours=24 * 7,
            )
            wf.save_bytes(r.id, data)
            entry.update(file_id=r.id, sha256=sha, state="ready")
            session.payload = {**session.payload, "entries": entries}
            event(
                db,
                current,
                "file_upload",
                "completed",
                {"file_id": r.id, "sha256": sha, "size_bytes": size},
                rid,
            )
            db.commit()
            return entry

    try:
        return await run_in_threadpool(finish)
    except Exception as exc:
        await run_in_threadpool(
            audit,
            actor,
            "file_upload",
            "failed",
            {"code": getattr(exc, "code", "invalid_upload")},
            rid,
        )
        if isinstance(exc, wf.import_safety.UploadSafetyError):
            raise McpError("invalid_file", "工作簿格式不合法") from exc
        raise


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: str = Field(pattern="^[0-9a-f]{64}$")


@router.post("/api/mcp-office/reviews/{preview_id}/confirm")
def confirm(
    preview_id: str, body: ConfirmInput, actor: Annotated[Actor, Depends(jwt_actor)]
):
    rid = audit(actor, "confirm_import", "requested", {"preview_id": preview_id})
    try:
        with SessionLocal() as db:
            result = wf.apply_preview(db, actor, preview_id, body.preview_hash, rid)
            db.commit()
            return result
    except Exception as exc:
        audit(
            actor,
            "confirm_import",
            "failed",
            {
                "preview_id": preview_id,
                "code": getattr(exc, "code", "operation_failed"),
            },
            rid,
        )
        raise


@router.get("/api/mcp-office/artifacts/{artifact_id}/download")
def download(artifact_id: str, actor: Annotated[Actor, Depends(web_actor)]):
    with SessionLocal() as db:
        actor = refresh_actor(db, actor)
        r = wf.check_artifact(db, actor, artifact_id)
        _, data = wf.file_bytes(db, actor, artifact_id, "artifact")
        filename = r.payload["name"]
    rid = audit(
        actor,
        "download",
        "download_started",
        {
            "artifact_id": artifact_id,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )

    def stream():
        completed = False
        try:
            for offset in range(0, len(data), 64 * 1024):
                yield data[offset : offset + 64 * 1024]
            completed = True
        finally:
            audit(
                actor,
                "download",
                "download_completed" if completed else "download_aborted",
                {"artifact_id": artifact_id},
                rid,
            )

    return StreamingResponse(
        stream(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/mcp-office", response_class=HTMLResponse)
def office():
    enabled()
    return HTMLResponse(
        Path(__file__).with_name("office.html").read_text(),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/mcp-office.js")
def script():
    enabled()
    return Response(
        Path(__file__).with_name("office.js").read_text(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )
