"""POST-only warehouse import and ambiguity workbench API (#209)."""

from __future__ import annotations

import threading

import anyio
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from app import config
from app.auth import current_identity, current_role
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models.system import SysUser
from app.security import (
    FULL_SCOPE_ROLES,
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_warehouse
from app.services.maintenance_warehouse_adapters import WarehouseWorkbookError


router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_PARSE_LOCK = threading.BoundedSemaphore(value=1)
_MULTIPART_OVERHEAD = 128 * 1024
_READ_CHUNK = 1024 * 1024


class DocumentSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str | None = None
    document_type: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class AmbiguitySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str | None = None
    status: str | None = None
    ambiguity_type: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class AmbiguityResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    reason: str = Field(min_length=1)
    decision: str
    link_kind: str | None = None
    target_type: str | None = None
    target_id: str | None = None


def _real_operator(db: Session, ident: dict) -> str:
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "仓库单据落库与歧义裁决必须使用实名系统账号",
        )
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(select(SysUser).where(
        SysUser.username == username,
        SysUser.is_active.is_(True),
    ))
    if not username or user is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "仓库单据落库与歧义裁决必须使用实名系统账号",
        )
    return username


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


async def _multipart(request: Request, *, apply: bool) -> tuple[bytes, str, dict[str, str]]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "multipart/form-data":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "只接受 multipart/form-data")
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Content-Length 格式无效") from exc
        if declared_size < 0 or declared_size > limit + _MULTIPART_OVERHEAD:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "工作簿超过上传安全上限")
    consumed = 0

    async def limited_receive():
        nonlocal consumed
        message = await request.receive()
        if message["type"] == "http.request":
            consumed += len(message.get("body", b""))
            if consumed > limit + _MULTIPART_OVERHEAD:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    "工作簿超过上传安全上限",
                )
        return message

    limited_request = Request(request.scope, limited_receive)
    form = None
    try:
        try:
            form = await limited_request.form(
                max_files=1,
                max_fields=2 if apply else 0,
                max_part_size=16 * 1024,
            )
        except (MultiPartException, ValueError, UnicodeError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "multipart 请求格式无效") from exc
        items = form.multi_items()
        expected_keys = {"file", "preview_token", "reason"} if apply else {"file"}
        if len(items) != len(expected_keys) or {key for key, _value in items} != expected_keys:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "上传字段不完整或包含未允许字段",
            )
        values = dict(items)
        upload = values.pop("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "file 必须是 .xlsx 文件")
        filename = (upload.filename or "warehouse.xlsx").replace("\\", "/").rsplit("/", 1)[-1]
        if not filename.lower().endswith(".xlsx"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "仓库单据只支持 .xlsx")
        chunks: list[bytes] = []
        size = 0
        while chunk := await upload.read(_READ_CHUNK):
            size += len(chunk)
            if size > limit:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "工作簿超过上传安全上限")
            chunks.append(chunk)
        fields: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(value, str):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "文本字段格式无效")
            fields[key] = value
        return b"".join(chunks), filename[:256], fields
    finally:
        if form is not None:
            await form.close()


def _service_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WarehouseWorkbookError):
        code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if exc.code in {
            "upload_limit", "zip_bomb", "zip_member_limit", "worksheet_limit",
            "column_limit", "row_limit", "cell_limit", "text_limit",
        } else status.HTTP_422_UNPROCESSABLE_CONTENT
        return HTTPException(code, str(exc))
    if isinstance(exc, maintenance_warehouse.MaintenanceWarehouseConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, maintenance_warehouse.MaintenanceWarehouseNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, maintenance_warehouse.MaintenanceWarehouseError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    raise exc


@router.post("/warehouse-imports/preview")
async def preview_warehouse_import(
    request: Request,
    response: Response,
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
) -> dict:
    _no_store(response)
    if not _PARSE_LOCK.acquire(blocking=False):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "已有仓库工作簿正在解析，请稍后重试",
            headers={"Retry-After": "5"},
        )
    try:
        content, filename, _fields = await _multipart(request, apply=False)
        try:
            return await anyio.to_thread.run_sync(
                lambda: maintenance_warehouse.preview_import(
                    content,
                    filename=filename,
                    hmac_key=get_settings().secret_key.encode("utf-8"),
                ),
                abandon_on_cancel=False,
            )
        except Exception as exc:
            raise _service_http_error(exc) from exc
    finally:
        _PARSE_LOCK.release()


def _apply_worker(
    content: bytes,
    filename: str,
    import_id: str,
    preview_token: str,
    reason: str,
    operated_by: str,
) -> dict:
    db = SessionLocal()
    try:
        result = maintenance_warehouse.apply_import(
            db,
            content,
            filename=filename,
            import_id=import_id,
            preview_token=preview_token,
            reason=reason,
            operated_by=operated_by,
            hmac_key=get_settings().secret_key.encode("utf-8"),
        )
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/warehouse-imports/{import_id}/apply")
async def apply_warehouse_import(
    request: Request,
    response: Response,
    import_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_warehouse_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _no_store(response)
    if ctx.role not in FULL_SCOPE_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "仓库文件应用仅限全项目范围实名账号",
        )
    operated_by = _real_operator(db, ident)
    if not _PARSE_LOCK.acquire(blocking=False):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "已有仓库工作簿正在解析，请稍后重试",
            headers={"Retry-After": "5"},
        )
    try:
        content, filename, fields = await _multipart(request, apply=True)
        preview_token = fields.get("preview_token", "")
        reason = fields.get("reason", "")
        if len(preview_token) != 43 or not reason.strip() or len(reason) > 1000:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "预览签名或导入理由无效")
        try:
            return await anyio.to_thread.run_sync(
                lambda: _apply_worker(
                    content, filename, import_id, preview_token, reason, operated_by
                ),
                abandon_on_cancel=False,
            )
        except IntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "仓库单据并发写入冲突，请重试") from exc
        except Exception as exc:
            raise _service_http_error(exc) from exc
    finally:
        _PARSE_LOCK.release()


@router.post("/warehouse-documents/search")
def search_warehouse_documents(
    body: DocumentSearchRequest,
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _no_store(response)
    if body.q is not None and len(body.q) > 128:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "仓库单据搜索条件无效")
    if body.document_type is not None and body.document_type not in {
        "shipment", "return", "receipt",
    }:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "仓库单据类型无效")
    record_access_log(ctx, "maintenance_warehouse_document_search", "maintenance_warehouse", {
        "searched": bool(body.q and body.q.strip()),
        "page": body.page,
        "document_type": body.document_type,
    })
    try:
        return maintenance_warehouse.search_documents(
            db,
            q=body.q,
            document_type=body.document_type,
            page=body.page,
            page_size=body.page_size,
            user_ctx=ctx,
        )
    except maintenance_warehouse.MaintenanceWarehouseError as exc:
        raise _service_http_error(exc) from exc


@router.post("/warehouse-ambiguities/search")
def search_warehouse_ambiguities(
    body: AmbiguitySearchRequest,
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _no_store(response)
    if body.q is not None and len(body.q) > 128:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "仓库歧义搜索条件无效")
    allowed_types = {
        "unknown_version", "missing_document_id", "missing_line_id",
        "missing_stable_link", "multiple_candidates", "field_conflict",
        "unknown_enum", "controlled_attachment",
        "integration_blocker",
    }
    if body.ambiguity_type is not None and body.ambiguity_type not in allowed_types:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "仓库歧义类型无效")
    if body.status is not None and body.status not in {"open", "resolved"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "仓库歧义状态无效")
    record_access_log(ctx, "maintenance_warehouse_ambiguity_search", "maintenance_warehouse", {
        "searched": bool(body.q and body.q.strip()),
        "page": body.page,
        "status": body.status,
        "ambiguity_type": body.ambiguity_type,
    })
    try:
        return maintenance_warehouse.search_ambiguities(
            db,
            q=body.q,
            status=body.status,
            ambiguity_type=body.ambiguity_type,
            page=body.page,
            page_size=body.page_size,
            user_ctx=ctx,
        )
    except maintenance_warehouse.MaintenanceWarehouseError as exc:
        raise _service_http_error(exc) from exc


@router.post("/warehouse-ambiguities/{ambiguity_id}/resolve")
def resolve_warehouse_ambiguity(
    body: AmbiguityResolveRequest,
    response: Response,
    ambiguity_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_warehouse_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _no_store(response)
    operated_by = _real_operator(db, ident)
    if len(body.reason) > 1000 or (body.target_id is not None and len(body.target_id) > 128):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "裁决内容无效")
    try:
        result = maintenance_warehouse.resolve_ambiguity(
            db,
            ambiguity_id=ambiguity_id,
            version=body.version,
            reason=body.reason,
            operated_by=operated_by,
            decision=body.decision,
            link_kind=body.link_kind,
            target_type=body.target_type,
            target_id=body.target_id,
            user_ctx=ctx,
        )
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        if isinstance(exc, (
            maintenance_warehouse.MaintenanceWarehouseError,
            maintenance_warehouse.MaintenanceWarehouseConflict,
        )):
            raise _service_http_error(exc) from exc
        raise
