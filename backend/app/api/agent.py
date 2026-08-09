"""AI 助手 Chat API（二期）。任意有效 token 可用（销售/采购是 readonly 角色）。"""
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import audit as agent_audit
from app.agent import provider, runtime
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
from app.services import agent_files

_log = logging.getLogger("agent")
# 后端页面准入：page_chat=False 的角色连接口都进不来（前端藏菜单≠后端拦接口，PR-审计 HUB-1）。
# admin 恒放行；RBAC 关或旧 token 走角色模板回退（各角色模板默认 page_chat=True）。
router = APIRouter(prefix="/agent", tags=["agent"],
                   dependencies=[Depends(require_page("page_chat"))])

_NOT_CONFIGURED_MSG = (
    "AI 助手尚未配置：请在服务器 .env 设置 LLM_API_KEY（如 DeepSeek 密钥）后重启服务。"
    "在此之前可以继续使用「型号查询」页的近似搜索。"
)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=40)


@router.post("/chat")
def chat(
    req: ChatRequest,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    settings = get_settings()
    if not settings.enable_agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI 助手未启用")
    record_access_log(
        ctx,
        "chat",
        "agent",
        agent_audit.chat_request_shape(
            message_count=len(req.messages),
            last_message=req.messages[-1].content,
            endpoint="chat",
            stream=False,
        ),
    )
    if not provider.is_configured():
        return {"configured": False, "answer": _NOT_CONFIGURED_MSG, "tool_calls": []}
    try:
        out = runtime.run(db, [m.model_dump() for m in req.messages], ctx)
    except provider.LLMNotConfigured:
        return {"configured": False, "answer": _NOT_CONFIGURED_MSG, "tool_calls": []}
    except Exception as exc:  # noqa: BLE001 —— 上游 LLM 网络/配额错误：不泄露细节
        _log.error("agent chat failed exception_type=%s", type(exc).__name__)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "AI 服务调用失败，请稍后重试（详情见服务端日志）") from exc
    return {"configured": True, **out}


@router.post("/chat/stream")
def chat_stream(
    req: ChatRequest,
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """SSE 流式问答：data: {type: delta|tool|tool_done|done|error, ...}\\n\\n"""
    settings = get_settings()
    if not settings.enable_agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI 助手未启用")
    record_access_log(
        ctx,
        "chat_stream",
        "agent",
        agent_audit.chat_request_shape(
            message_count=len(req.messages),
            last_message=req.messages[-1].content,
            endpoint="chat_stream",
            stream=True,
        ),
    )

    def _sse(ev: dict) -> str:
        return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    def gen():
        if not provider.is_configured():
            yield _sse({"type": "delta", "text": _NOT_CONFIGURED_MSG})
            yield _sse({"type": "done", "tool_calls": [], "configured": False})
            return
        try:
            for ev in runtime.run_stream(db, [m.model_dump() for m in req.messages], ctx):
                yield _sse(ev)
        except Exception as exc:  # noqa: BLE001 —— 流中途出错：发 error 事件而非半截断流
            _log.error("agent stream failed exception_type=%s", type(exc).__name__)
            yield _sse({"type": "error", "message": "AI 服务调用失败，请稍后重试"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """上传 xlsx（询价单/型号清单等），返回 file_id 供对话引用。"""
    record_access_log(ctx, "upload", "agent_file", {"filename": file.filename})
    content = await file.read()
    try:
        # 归属记真实身份(user_id)而非角色 → 下载/读取按人做越权校验
        return agent_files.save_upload(content, file.filename or "上传.xlsx", ctx.user_id)
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/files/{file_id}")
def download(
    file_id: str,
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> FileResponse:
    """下载智能体生成/上传的文件（仅本人创建的；admin 例外）。"""
    record_access_log(ctx, "download", "agent_file", {"file_id": file_id})
    try:
        owner = agent_files.owner_of(file_id)        # 文件不存在 → FileError → 404
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    # 防越权下载他人上传的报价/合同(IDOR)：全量角色放行，否则需本人创建
    if ctx.role not in FULL_SCOPE_ROLES and owner != ctx.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该文件（非本人上传/生成）")
    try:
        path, name = agent_files.get_download(file_id)
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return FileResponse(
        path, filename=name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        # 含价格数据：禁止浏览器缓存（否则换人/过期 token 仍能命中缓存拿到文件）
        headers={"Cache-Control": "no-store"},
    )


@router.get("/files/{file_id}/preview")
def preview_file(
    file_id: str,
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """在线预览文件内容（仅本人创建的；admin 例外）——与 download 同一归属校验，防 IDOR。"""
    record_access_log(ctx, "preview", "agent_file", {"file_id": file_id})
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
