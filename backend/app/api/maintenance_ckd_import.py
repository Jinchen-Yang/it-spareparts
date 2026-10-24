from typing import Literal
"""氚云发货单导入 API（C1a/F1）：preview → apply → 批次状态。"""

from fastapi import Body, Query, APIRouter, Depends, HTTPException, Path, Request, Response, status
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

from app.api import maintenance_project_scope as auth_transport
from app.services import maintenance_import_authorization as canonical
from app.services import maintenance_authorization as foundation
from app.services.maintenance_workflow_authorization import multipart_precondition

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
    identity=Depends(auth_transport.identity_for('maintenance_shipment_import')),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_shipment_import", "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_shipment_import", require_action(_ACTION_KEY, require_data="data_purchase_cost"))),
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
    parsed_precondition=multipart_precondition(authorization_precondition,form.get("authorization_precondition")) if identity is not None else None
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
        if identity is not None and view == 'authorization':
            with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
                    entry_id=canonical.CKD_PREVIEW,
                    params={'input_sha256': parsed['file_hash'], 'doc_type': parsed.get('doc_type', 'ckd_shipment')}) as (_, _, context, accesses):
                return canonical.service.context_resolution(accesses, context.contexts[0].ref)
        batch_id = ckd.store_preview(
            db, parsed, _real_operator(ident), idempotency_key=idempotency_key, identity=identity,
            authorization_precondition=parsed_precondition
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
    response: Response = None,
    identity=Depends(auth_transport.identity_for('maintenance_shipment_import', 'maintenance_shipment_import_targets')),
    view: Literal['authorization'] | None = Query(None),
    command: dict | None = Body(None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(("maintenance_shipment_import", "maintenance_shipment_import_targets"), "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate(("maintenance_shipment_import", "maintenance_shipment_import_targets"), require_action(_ACTION_KEY, require_data="data_purchase_cost"))),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if identity is not None:
        response.headers['Cache-Control'] = 'no-store'
        if command is not None and set(command) - {'authorization_precondition'}:
            raise canonical.authz_error(422, 'AUTHZ_INVALID_REQUEST', '导入应用仅接受授权前置条件')
        try:
            return canonical.apply_batch(db, identity, kind='shipment_import_batch', batch_id=batch_id,
                authorization_view=view == 'authorization', precondition=(command or {}).get('authorization_precondition'))
        except ckd.CkdBatchError as exc:
            raise HTTPException(409, {'code': 'batch_conflict', 'message': str(exc)}, headers={'Cache-Control': 'no-store'}) from exc
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
    identity=Depends(auth_transport.identity_for('maintenance_shipment_import')),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_shipment_import", "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_shipment_import", require_action(_ACTION_KEY, require_data="data_purchase_cost"))),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if identity is not None:
        return canonical.read_batch(db, identity, kind='shipment_import_batch', batch_id=batch_id,
            authorization_view=view == 'authorization',
            precondition=foundation.decode_precondition(authorization_precondition))
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
