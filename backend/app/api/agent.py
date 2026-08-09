"""AI 助手 Chat API（二期）。任意有效 token 可用（销售/采购是 readonly 角色）。"""
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import provider, runtime
from app.auth import current_role
from app.config import get_settings
from app.db import get_db
from app.security import (
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


def _artifact_http_error(exc: agent_files.FileError, default_status: int) -> HTTPException:
    code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if isinstance(exc, agent_files.ArtifactV2Disabled)
        else default_status
    )
    return HTTPException(code, str(exc))


def _audit_artifact_access(
    ctx: UserContext,
    action: str,
    outcome: str,
    *,
    artifact_id: str | None = None,
    reason_code: str | None = None,
    size_bytes: int | None = None,
) -> None:
    """Emit content-free, stable artifact audit fields only."""
    detail: dict[str, str | int] = {"outcome": outcome}
    if artifact_id is not None:
        try:
            detail["artifact_id"] = agent_files._check_id(artifact_id)
        except agent_files.FileError:
            pass
    if reason_code is not None:
        detail["reason_code"] = reason_code
    if size_bytes is not None:
        detail["size_bytes"] = size_bytes
    record_access_log(ctx, action, "agent_file", detail)


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
    record_access_log(ctx, "chat", "agent", {"q": req.messages[-1].content[:200]})
    if not provider.is_configured():
        return {"configured": False, "answer": _NOT_CONFIGURED_MSG, "tool_calls": []}
    try:
        out = runtime.run(db, [m.model_dump() for m in req.messages], ctx)
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
) -> StreamingResponse:
    """SSE 流式问答：data: {type: delta|tool|tool_done|done|error, ...}\\n\\n"""
    settings = get_settings()
    if not settings.enable_agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI 助手未启用")
    record_access_log(ctx, "chat_stream", "agent", {"q": req.messages[-1].content[:200]})

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
            _log.error("agent stream failed: %r", exc)
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
    """上传受支持的办公文件/文本/图片，返回不可猜测的 Artifact UUID。"""
    try:
        # 身份与发布开关必须在读请求体之前失败关闭，避免未授权的大文件消耗内存。
        owner = agent_files.verified_artifact_owner(ctx)
        agent_files.require_artifact_v2_enabled()
    except agent_files.FileError as exc:
        reason = (
            "unstable_identity"
            if "实名系统账号" in str(exc)
            else agent_files.artifact_reason_code(exc)
        )
        _audit_artifact_access(ctx, "upload", "denied", reason_code=reason)
        default = (
            status.HTTP_403_FORBIDDEN
            if "实名系统账号" in str(exc)
            else status.HTTP_400_BAD_REQUEST
        )
        raise _artifact_http_error(exc, default) from exc
    content = await file.read()
    try:
        result = agent_files.save_upload(content, file.filename or "上传.xlsx", owner)
    except agent_files.FileError as exc:
        _audit_artifact_access(
            ctx,
            "upload",
            "denied",
            reason_code=agent_files.artifact_reason_code(exc),
            size_bytes=len(content),
        )
        raise _artifact_http_error(exc, status.HTTP_400_BAD_REQUEST) from exc
    # 文件名常含客户、合同或项目名；审计仅保留非内容型结构信息。
    _audit_artifact_access(
        ctx,
        "upload",
        "success",
        artifact_id=result["file_id"],
        size_bytes=len(content),
    )
    return result


@router.get("/files/{file_id}")
def download(
    file_id: str,
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> FileResponse:
    """下载智能体生成/上传的文件（普通端点严格仅本人，管理员也不例外）。"""
    try:
        agent_files.owner_of(file_id)        # 文件不存在 → FileError → 404
    except agent_files.FileError as exc:
        _audit_artifact_access(
            ctx, "download", "denied", artifact_id=file_id,
            reason_code=agent_files.artifact_reason_code(exc),
        )
        raise _artifact_http_error(exc, status.HTTP_404_NOT_FOUND) from exc
    # 防越权下载他人上传的报价/合同（IDOR）：普通端点对所有角色都要求本人创建。
    if not agent_files.access_allowed(file_id, ctx):
        _audit_artifact_access(
            ctx, "download", "denied", artifact_id=file_id, reason_code="forbidden"
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该文件（非本人上传/生成）")
    try:
        artifact = agent_files.get_download_info(file_id)
    except agent_files.FileError as exc:
        _audit_artifact_access(
            ctx, "download", "denied", artifact_id=file_id,
            reason_code=agent_files.artifact_reason_code(exc),
        )
        raise _artifact_http_error(exc, status.HTTP_404_NOT_FOUND) from exc
    _audit_artifact_access(ctx, "download", "success", artifact_id=file_id)
    return FileResponse(
        artifact.path,
        filename=artifact.filename,
        media_type=artifact.media_type,
        # 含价格数据：禁止浏览器缓存（否则换人/过期 token 仍能命中缓存拿到文件）
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{artifact.sha256}"',
            "Content-Length": str(artifact.size_bytes),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/files/{file_id}/preview")
def preview_file(
    file_id: str,
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """在线预览文件内容（所有角色仅本人）——与 download 同一归属校验，防 IDOR。"""
    try:
        agent_files.owner_of(file_id)
    except agent_files.FileError as exc:
        _audit_artifact_access(
            ctx, "preview", "denied", artifact_id=file_id,
            reason_code=agent_files.artifact_reason_code(exc),
        )
        raise _artifact_http_error(exc, status.HTTP_404_NOT_FOUND) from exc
    if not agent_files.access_allowed(file_id, ctx):
        _audit_artifact_access(
            ctx, "preview", "denied", artifact_id=file_id, reason_code="forbidden"
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该文件（非本人上传/生成）")
    try:
        result = agent_files.preview(file_id)
    except agent_files.FileError as exc:
        _audit_artifact_access(
            ctx, "preview", "denied", artifact_id=file_id,
            reason_code=agent_files.artifact_reason_code(exc),
        )
        raise _artifact_http_error(exc, status.HTTP_404_NOT_FOUND) from exc
    _audit_artifact_access(ctx, "preview", "success", artifact_id=file_id)
    return result
