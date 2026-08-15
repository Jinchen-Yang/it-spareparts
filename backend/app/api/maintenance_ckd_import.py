"""氚云发货单导入 API（C1a/F1）：preview → apply → 批次状态。"""

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from starlette.datastructures import UploadFile
from sqlalchemy.orm import Session

from app.auth import current_identity, current_role
from app.db import get_db
from app.models.maintenance_ckd_import import MaintenanceCkdImportBatch
from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.security import (
    UserContext,
    get_current_user_context,
    require_action,
    require_page,
)
from app.services import import_safety
from app.services import maintenance_ckd_import as ckd

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
MAX_PREVIEW_BYTES = ckd.MAX_PREVIEW_BYTES
_ACTION_KEY = "action_maintenance_doc_import"


def _real_operator(ident: dict) -> str:
    return str(ident.get("username") or ident.get("sub") or "unknown")


def _preflight(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_PREVIEW_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {"code": "upload_too_large", "message": "发货单超过上传安全上限"},
        )
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"code": "unsupported_media_type", "message": "只接受 multipart/form-data 的 .xlsx 文件"},
        )


@router.post("/ckd-imports/preview")
async def preview_ckd_import(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
    _preflight: None = Depends(_preflight),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request", "message": f"multipart 表单解析失败：{type(exc).__name__}"},
        )
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request", "message": "缺少 file 上传字段"},
        )
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"code": "unsupported_media_type", "message": "只接受 .xlsx 发货单"},
        )
    idempotency_key = request.headers.get("idempotency-key", "")
    if not (8 <= len(idempotency_key) <= 128):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request", "message": "Idempotency-Key 必填且长度必须在 8–128 字符之间"},
        )
    try:
        data = await import_safety.read_limited(file, MAX_PREVIEW_BYTES)
    except import_safety.UploadSafetyError as exc:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {"code": "upload_too_large", "message": str(exc)},
        )
    try:
        import_safety.validate_xlsx_zip(data, max_bytes=MAX_PREVIEW_BYTES)
    except import_safety.UploadSafetyError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_ckd", "message": str(exc)},
        )
    try:
        parsed = await import_safety.parse_in_threadpool(
            ckd.parse_ckd_workbook, data, filename
        )
        batch_id = ckd.store_preview(
            db, parsed, _real_operator(ident), idempotency_key=idempotency_key
        )
    except ckd.CkdParseError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_ckd", "message": str(exc)},
        )
    except ckd.CkdBatchError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "batch_conflict", "message": str(exc)},
        )
    return {
        "batch_id": batch_id,
        "file_hash": parsed["file_hash"],
        "head_rows": len(parsed["heads"]),
        "line_rows": parsed["line_count"],
    }


@router.post("/ckd-imports/{batch_id}/apply")
def apply_ckd_import(
    batch_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operator = _real_operator(ident)
    batch = db.get(MaintenanceCkdImportBatch, batch_id)
    if batch is not None and batch.uploaded_by != operator and ctx.role not in ("admin", "boss"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": "只能应用本人上传的发货单批次"},
        )
    # 项目范围门：范围账号的批次不得含范围外项目（403 整批零写）
    allowed = resolve_visible_project_ids(db, ctx)
    try:
        summary = ckd.apply_batch(
            db, batch_id, operator, allowed_project_ids=allowed
        )
    except ckd.CkdBatchError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "batch_conflict", "message": str(exc)},
        )
    except ckd.CkdScopeDenied as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": str(exc)},
        )
    return {"batch_id": batch_id, **summary}


@router.get("/ckd-imports/{batch_id}")
def get_ckd_import(
    batch_id: str = Path(..., min_length=36, max_length=36),
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    operator = _real_operator(ident)
    batch = db.get(MaintenanceCkdImportBatch, batch_id)
    if batch is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "发货单批次不存在"},
        )
    if batch.uploaded_by != operator and ctx.role not in ("admin", "boss"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": "无权读取该发货单批次"},
        )
    return {
        "batch_id": batch.batch_id,
        "file_hash": batch.file_hash,
        "filename": batch.filename,
        "uploaded_by": batch.uploaded_by,
        "uploaded_at": batch.uploaded_at.isoformat() if batch.uploaded_at else None,
        "head_rows": batch.head_rows,
        "line_rows": batch.line_rows,
        "issue_rows": batch.issue_rows,
        "status": batch.status,
        "applied_by": batch.applied_by,
        "applied_at": batch.applied_at.isoformat() if batch.applied_at else None,
        "report": batch.report_json,
    }
