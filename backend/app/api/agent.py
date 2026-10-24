"""AI 助手 Chat API（二期）。任意有效 token 可用（销售/采购是 readonly 角色）。"""
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import provider, runtime, stream_protocol as wire
from app.api.agent_route import AgentRoute
from app.auth import current_role
from app.config import get_settings
from app.db import get_db
from app.security import (
    FULL_SCOPE_ROLES,
    UserContext,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import agent_files, agent_authorization as authorization, agent_file_authorization as protected_files
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.authz.provenance import AgentAccess, execution_access, unavailable

_log = logging.getLogger("agent")
# 后端页面准入：page_chat=False 的角色连接口都进不来（前端藏菜单≠后端拦接口，PR-审计 HUB-1）。
# admin 恒放行；RBAC 关或旧 token 走角色模板回退（各角色模板默认 page_chat=True）。
router = APIRouter(route_class=AgentRoute, prefix="/agent", tags=["agent"],
                   dependencies=[Depends(authorization.page_gate)])

_NOT_CONFIGURED_MSG = (
    "AI 助手尚未配置：请在服务器 .env 设置 LLM_API_KEY（如 DeepSeek 密钥）后重启服务。"
    "在此之前可以继续使用「型号查询」页的近似搜索。"
)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    model_config = {"extra": "forbid"}
    authorization_precondition: dict | None = None
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=40)


@router.post("/chat")
def chat(
    req: ChatRequest,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.CHAT, {'messages': [m.model_dump() for m in req.messages]}, view=view, precondition=authorization.precondition(authorization_precondition, req.authorization_precondition) if access.authoritative else None)
    if resolution is not None:
        return resolution
    if access.authoritative and any(m.role != "user" for m in req.messages):
        raise unavailable()
    settings = get_settings()
    if not settings.enable_agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI 助手未启用")
    record_access_log(ctx, "chat", "agent", {"q": req.messages[-1].content[:200]})
    if not provider.is_configured():
        return {"configured": False, "answer": _NOT_CONFIGURED_MSG, "tool_calls": []}
    try:
        out = runtime.run(db, [m.model_dump() for m in req.messages], ctx, access=access)
    except HTTPException:
        raise
    except provider.LLMNotConfigured:
        return {"configured": False, "answer": _NOT_CONFIGURED_MSG, "tool_calls": []}
    except Exception as exc:  # noqa: BLE001 —— 上游 LLM 网络/配额错误：不泄露细节
        _log.error("agent chat failed: %r", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "AI 服务调用失败，请稍后重试（详情见服务端日志）") from exc
    return {"configured": True, **out}


@router.post("/chat/stream")
def chat_stream(
    req: ChatRequest,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> StreamingResponse:
    """SSE 流式问答：data: {type: delta|tool|tool_done|done|error, ...}\\n\\n"""
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.STREAM, {'messages': [m.model_dump() for m in req.messages]}, view=view, precondition=authorization.precondition(authorization_precondition, req.authorization_precondition) if access.authoritative else None)
    if resolution is not None:
        return resolution
    if access.authoritative and any(m.role != "user" for m in req.messages):
        raise unavailable()
    settings = get_settings()
    if not settings.enable_agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI 助手未启用")
    record_access_log(ctx, "chat_stream", "agent", {"q": req.messages[-1].content[:200]})

    db.rollback()
    run_id = wire.new_run_id()

    def gen():
        seq = 0
        if access.authoritative:
            yield wire.encode(wire.ready(None, run_id, access.execution_wire))
        try:
            access.check()
            events = (runtime.run_stream(db, [m.model_dump() for m in req.messages], ctx, access=access)
                      if provider.is_configured() else iter((
                          {"type": "delta", "text": _NOT_CONFIGURED_MSG},
                          {"type": "done", "tool_calls": [], "configured": False})))
            terminal = False
            for ev in events:
                access.check()
                if terminal and access.authoritative:
                    return
                seq += 1
                yield wire.encode(wire.frame(ev, run_id, seq) if access.authoritative else ev)
                terminal = ev.get('type') in wire.TERMINAL
                if terminal:
                    return
            if access.authoritative and not terminal:
                seq += 1
                yield wire.encode(wire.frame(wire.error_payload('CHAT_STREAM_PROTOCOL_ERROR'), run_id, seq))
        except Exception as exc:
            _log.exception("agent stream failed")
            seq += 1
            error = wire.exception_payload(exc)
            yield wire.encode(wire.frame(error, run_id, seq) if access.authoritative else error)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/upload")
async def upload(
    form_precondition: str | None = Form(None, alias="authorization_precondition"),
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    """上传 xlsx（询价单/型号清单等），返回 file_id 供对话引用。"""
    record_access_log(ctx, "upload", "agent_file", {"filename": file.filename})
    content = await file.read()
    try:
        # 归属记真实身份(user_id)而非角色 → 下载/读取按人做越权校验
        access = AgentAccess(db.get_bind(), identity)
        from app.services.maintenance_workflow_authorization import multipart_precondition
        precondition = multipart_precondition(authorization_precondition, form_precondition) if access.authoritative else None
        resolution = authorization.prepare(access, authorization.UPLOAD,
            {'filename': file.filename or '上传.xlsx', 'content': __import__('hashlib').sha256(content).hexdigest()},
            view=view, precondition=precondition)
        if resolution is not None:
            return resolution
        with execution_access(access):
            return agent_files.save_upload(content, file.filename or "上传.xlsx", ctx.user_id)
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/files/{file_id}")
def download(
    file_id: str,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> StreamingResponse:
    """下载智能体生成/上传的文件；新权威只允许来源仍有效的本人产物。"""
    record_access_log(ctx, "download", "agent_file", {"file_id": file_id})
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.DOWNLOAD, {'file_id': file_id}, view=view, precondition=authorization_precondition)
    if resolution is not None:
        return resolution
    if access.authoritative:
        with execution_access(access):
            meta, content = protected_files.materialize(access, file_id, download=True)
        db.rollback()
        access.check()
        def verified_chunks():
            for offset in range(0, len(content), 64 * 1024):
                access.check()
                yield content[offset:offset + 64 * 1024]
        from urllib.parse import quote
        return StreamingResponse(verified_chunks(), media_type='application/octet-stream',
            headers={'Cache-Control': 'no-store',
                     'Content-Disposition': "attachment; filename*=utf-8''" + quote(meta['filename'])})
    try:
        with execution_access(access):
            if not access.authoritative:
                owner = agent_files.owner_of(file_id)
                if ctx.role not in FULL_SCOPE_ROLES and owner != ctx.user_id:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该文件（非本人上传/生成）")
            path, name = agent_files.get_download(file_id)
    except agent_files.FileError as exc:
        raise HTTPException(404, "文件不存在或不可访问") from exc
    db.rollback()

    def chunks():
        with path.open("rb") as source:
            while True:
                access.check()
                chunk = source.read(64 * 1024)
                if not chunk:
                    return
                access.check()
                yield chunk

    from urllib.parse import quote
    return StreamingResponse(chunks(), media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "Content-Disposition": "attachment; filename*=utf-8''" + quote(name)})


@router.get("/files/{file_id}/preview")
def preview_file(
    file_id: str,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    """在线预览文件内容（仅本人创建的；admin 例外）——与 download 同一归属校验，防 IDOR。"""
    record_access_log(ctx, "preview", "agent_file", {"file_id": file_id})
    access = AgentAccess(db.get_bind(), identity)
    resolution = authorization.prepare(access, authorization.PREVIEW, {'file_id': file_id}, view=view, precondition=authorization_precondition)
    if resolution is not None:
        return resolution
    if access.authoritative:
        try:
            with execution_access(access):
                result = agent_files.preview(file_id)
                return {**result, 'authorization_context': access.execution_wire}
        except agent_files.FileError as exc:
            raise HTTPException(404, "文件不存在或不可访问") from exc
    try:
        owner = agent_files.owner_of(file_id)
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if ctx.role not in FULL_SCOPE_ROLES and owner != ctx.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该文件（非本人上传/生成）")
    try:
        return agent_files.preview(file_id)
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
