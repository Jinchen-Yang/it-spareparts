"""AI 助手 Chat API（二期）。任意有效 token 可用（销售/采购是 readonly 角色）。"""
import json
import logging
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import audit as agent_audit
from app.agent import provider, runtime, tools as agent_tools
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


def _require_agent_enabled() -> None:
    """The kill switch covers the complete /agent namespace, including file parsers."""
    if not get_settings().enable_agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI 助手未启用")


# 后端页面准入：page_chat=False 的角色连接口都进不来（前端藏菜单≠后端拦接口，PR-审计 HUB-1）。
# admin 恒放行；RBAC 关或旧 token 走角色模板回退（各角色模板默认 page_chat=True）。
router = APIRouter(prefix="/agent", tags=["agent"],
                   dependencies=[Depends(_require_agent_enabled), Depends(require_page("page_chat"))])

_NOT_CONFIGURED_MSG = (
    "AI 助手尚未配置：请在服务器 .env 设置 LLM_API_KEY（如 DeepSeek 密钥）后重启服务。"
    "在此之前可以继续使用「型号查询」页的近似搜索。"
)
_FILE_NOT_FOUND = "文件不存在或无权访问"


def _require_stable_file_owner(ctx: UserContext) -> str:
    """Return a verified named owner or fail with the non-enumerating file response."""
    if (
        ctx.authn != "sys_user"
        or not isinstance(ctx.user_id, str)
        or not ctx.user_id.strip()
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _FILE_NOT_FOUND)
    return ctx.user_id


def _deny_file_access(
    ctx: UserContext,
    action: Literal["download", "preview"],
    *,
    id_present: bool,
    id_format_valid: bool,
    cause: BaseException | None = None,
) -> NoReturn:
    """Audit a denial without copying an attacker/other-owner identifier."""
    record_access_log(
        ctx,
        f"{action}_denied",
        "agent_file",
        {
            "id_present": id_present,
            "id_format_valid": id_format_valid,
        },
    )
    raise HTTPException(status.HTTP_404_NOT_FOUND, _FILE_NOT_FOUND) from cause


def _authorize_file_access(
    ctx: UserContext,
    file_id: str,
    action: Literal["download", "preview"],
) -> str:
    """Return a canonical owner-authorized ID; only then may telemetry contain the ID."""
    id_present = isinstance(file_id, str) and bool(file_id.strip())
    try:
        canonical_id = agent_files.canonical_file_id(file_id)
    except agent_files.FileError as exc:
        _deny_file_access(
            ctx,
            action,
            id_present=id_present,
            id_format_valid=False,
            cause=exc,
        )
    try:
        owner_id = _require_stable_file_owner(ctx)
    except HTTPException as exc:
        _deny_file_access(
            ctx,
            action,
            id_present=id_present,
            id_format_valid=True,
            cause=exc,
        )
    try:
        owner = agent_files.owner_of(canonical_id)
    except agent_files.FileError as exc:
        _deny_file_access(
            ctx,
            action,
            id_present=id_present,
            id_format_valid=True,
            cause=exc,
        )
    if not owner or owner != owner_id:
        _deny_file_access(
            ctx,
            action,
            id_present=id_present,
            id_format_valid=True,
        )
    record_access_log(
        ctx,
        action,
        "agent_file",
        {"artifact_id": canonical_id},
    )
    return canonical_id


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
    if not runtime.primary_model_call_allowed():
        return {"configured": True, **runtime.model_egress_error_result([])}
    try:
        out = runtime.project_run_result(
            runtime.run(db, [m.model_dump() for m in req.messages], ctx),
            agent_tools.fresh_artifact_authorizer(ctx),
        )
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
        return f"data: {json.dumps(ev, ensure_ascii=False, allow_nan=False)}\n\n"

    configured = provider.is_configured()
    if configured and not runtime.primary_model_call_allowed():
        denied = runtime.project_error_event(runtime.model_egress_error_event([]))

        def denied_gen():
            yield _sse(denied)

        return StreamingResponse(
            denied_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def gen():
        if not configured:
            yield _sse({"type": "delta", "text": _NOT_CONFIGURED_MSG})
            yield _sse({"type": "done", "tool_calls": [], "configured": False})
            return
        try:
            projector = runtime.PublicEventProjector(
                agent_tools.fresh_artifact_authorizer(ctx),
            )
            for raw_event in runtime.run_stream(
                db,
                [m.model_dump() for m in req.messages],
                ctx,
            ):
                for ev in projector.project(raw_event):
                    yield _sse(ev)
                    if ev.get("type") == "error":
                        return
        except Exception as exc:  # noqa: BLE001 —— 流中途出错：发 error 事件而非半截断流
            _log.error("agent stream failed exception_type=%s", type(exc).__name__)
            yield _sse(runtime.project_error_event())

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
    # Multipart parsing is currently owned by FastAPI/Starlette (#222 tracks a streaming parser),
    # but the application must reject unstable/shared subjects before explicitly reading bytes,
    # auditing customer metadata or creating an ownerless artifact on disk.
    owner_id = _require_stable_file_owner(ctx)
    content = await file.read()
    record_access_log(
        ctx,
        "upload",
        "agent_file",
        agent_files.upload_audit_shape(file.filename, len(content)),
    )
    try:
        # 归属记真实身份(user_id)而非角色 → 下载/读取按人做越权校验
        return agent_files.save_upload(content, file.filename or "上传.xlsx", owner_id)
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/files/{file_id}")
def download(
    file_id: str,
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> FileResponse:
    """下载智能体生成/上传的文件；所有角色均只可访问本人文件。"""
    canonical_id = _authorize_file_access(ctx, file_id, "download")
    try:
        path, name = agent_files.get_download(canonical_id)
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _FILE_NOT_FOUND) from exc
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
    """在线预览文件内容；与 download 共用 owner-only、稳定主体边界。"""
    canonical_id = _authorize_file_access(ctx, file_id, "preview")
    try:
        return agent_files.preview(canonical_id)
    except agent_files.FileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _FILE_NOT_FOUND) from exc
