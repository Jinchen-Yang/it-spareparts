from typing import Literal
"""氚云三单（RKD/返库/BXD）导入 API（C1b）。"""

from fastapi import Body, Query, APIRouter, Depends, HTTPException, Path, Request, Response, status
from starlette.datastructures import UploadFile
from sqlalchemy.orm import Session

from app.auth import current_identity, current_role
from app.db import get_db
from app.models.maintenance_doc_import import MaintenanceDocImportBatch
from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.maintenance_boss import require_maintenance_boss
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import import_safety
from app.services import maintenance_doc_import as docs

from app.api import maintenance_project_scope as auth_transport
from app.services import maintenance_import_authorization as canonical
from app.services import maintenance_authorization as foundation
from app.services.maintenance_workflow_authorization import multipart_precondition

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
MAX_PREVIEW_BYTES = docs.MAX_PREVIEW_BYTES
_ACTION_KEY = "action_maintenance_doc_import"
_DOC_LABELS = {
    "rkd_inbound": "入库单",
    "return_order": "退货返库单",
    "bxd_expense": "报销单",
}


def _real_operator(ident: dict) -> str:
    return str(ident.get("username") or ident.get("sub") or "unknown")


def _preflight(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_PREVIEW_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {"code": "upload_too_large", "message": "单据超过上传安全上限"},
        )
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"code": "unsupported_media_type", "message": "只接受 multipart/form-data 的 .xlsx 文件"},
        )


@router.post("/doc-imports/{doc_type}/preview")
async def preview_doc_import(
    doc_type: str = Path(...),
    request: Request = None,
    response: Response = None,
    identity=Depends(auth_transport.identity_for('maintenance_document_import')),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_document_import", "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_document_import", require_action(_ACTION_KEY, require_data="data_purchase_cost"))),
    _preflight: None = Depends(_preflight),
    _ctx=Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if doc_type not in _DOC_LABELS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_doc_type", "message": "单据类型必须是 rkd_inbound/return_order/bxd_expense"},
        )
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
            {"code": "unsupported_media_type", "message": "只接受 .xlsx 单据"},
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
            {"code": "invalid_doc", "message": str(exc)},
        )
    try:
        parsed = await import_safety.parse_in_threadpool(
            docs.parse_doc_workbook, doc_type, data, filename
        )
        if identity is not None and view == 'authorization':
            with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
                    entry_id=canonical.DOC_PREVIEW,
                    params={'input_sha256': parsed['file_hash'], 'doc_type': parsed.get('doc_type', 'ckd_shipment')}) as (_, _, context, accesses):
                return canonical.service.context_resolution(accesses, context.contexts[0].ref)
        batch_id = docs.store_preview(
            db, parsed, _real_operator(ident), idempotency_key=idempotency_key, identity=identity,
            authorization_precondition=parsed_precondition
        )
    except docs.DocParseError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_doc", "message": str(exc)},
        )
    except docs.DocBatchError as exc:
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


@router.post("/doc-imports/{batch_id}/apply")
def apply_doc_import(
    batch_id: str = Path(..., min_length=36, max_length=36),
    response: Response = None,
    identity=Depends(auth_transport.identity_for('maintenance_document_import', 'maintenance_document_import_targets')),
    view: Literal['authorization'] | None = Query(None),
    command: dict | None = Body(None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(("maintenance_document_import", "maintenance_document_import_targets"), "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate(("maintenance_document_import", "maintenance_document_import_targets"), require_action(_ACTION_KEY, require_data="data_purchase_cost"))),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if identity is not None:
        response.headers['Cache-Control'] = 'no-store'
        if command is not None and set(command) - {'authorization_precondition'}:
            raise canonical.authz_error(422, 'AUTHZ_INVALID_REQUEST', '导入应用仅接受授权前置条件')
        try:
            return canonical.apply_batch(db, identity, kind='document_import_batch', batch_id=batch_id,
                authorization_view=view == 'authorization', precondition=(command or {}).get('authorization_precondition'))
        except docs.DocBatchError as exc:
            raise HTTPException(409, {'code': 'batch_conflict', 'message': str(exc)}, headers={'Cache-Control': 'no-store'}) from exc
    operator = _real_operator(ident)
    batch = db.get(MaintenanceDocImportBatch, batch_id)
    if batch is not None and batch.uploaded_by != operator and ctx.role not in ("admin", "boss"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": "只能应用本人上传的单据批次"},
        )
    allowed = resolve_visible_project_ids(db, ctx)
    try:
        summary = docs.apply_batch(
            db, batch_id, operator, allowed_project_ids=allowed
        )
    except docs.DocBatchError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "batch_conflict", "message": str(exc)},
        )
    except docs.DocScopeDenied as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": str(exc)},
        )
    return {"batch_id": batch_id, **summary}


@router.get("/doc-imports/{batch_id}")
def get_doc_import(
    batch_id: str = Path(..., min_length=36, max_length=36),
    response: Response = None,
    identity=Depends(auth_transport.identity_for('maintenance_document_import')),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_document_import", "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_document_import", require_action(_ACTION_KEY, require_data="data_purchase_cost"))),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if identity is not None:
        return canonical.read_batch(db, identity, kind='document_import_batch', batch_id=batch_id,
            authorization_view=view == 'authorization',
            precondition=foundation.decode_precondition(authorization_precondition))
    operator = _real_operator(ident)
    batch = db.get(MaintenanceDocImportBatch, batch_id)
    if batch is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "单据批次不存在"},
        )
    if batch.uploaded_by != operator and ctx.role not in ("admin", "boss"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": "无权读取该单据批次"},
        )
    if (batch.report_json or {}).get("protocol") == "return_receipts_v1":
        raise HTTPException(409, {"code": "dedicated_protocol", "message": "返件任务请使用专用任务查询接口"})
    return {
        "batch_id": batch.batch_id,
        "doc_type": batch.doc_type,
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


@router.post("/doc-imports/relink-projects")
def relink_doc_import_projects(
    response: Response = None,
    # M4-3 是本次发布新增的写端点：必须同受展示板总闸约束，否则「回滚=关 flag」
    # 收不回它（铁律 7）。既有三单导入端点不受影响。
    _flag: None = Depends(require_maintenance_boss),
    identity=Depends(auth_transport.identity_for('maintenance_document_relink')),
    view: Literal['authorization'] | None = Query(None),
    command: dict | None = Body(None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_document_relink", "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_document_relink", require_action(_ACTION_KEY, require_data="data_purchase_cost"))),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """已应用单据头行的项目重解析（plan v1.3 M4-3：上传顺序无关）。

    先传 RKD/返库、后建 WBDD 归属的场景下，把停在 NULL 的 project_id 补齐；
    幂等，不覆盖既有归属。非 admin/boss 只在自身可见项目范围内补。
    """
    response.headers["Cache-Control"] = "no-store"
    if identity is not None:
        if command is not None and set(command) - {'authorization_precondition'}:
            raise canonical.authz_error(422,'AUTHZ_INVALID_REQUEST','重关联不接受客户端目标列表')
        return canonical.relink(db,identity,authorization_view=view == 'authorization',
                                precondition=(command or {}).get('authorization_precondition'))
    allowed = (None if ctx.role in ("admin", "boss")
               else set(resolve_visible_project_ids(db, ctx) or set()))
    result = docs.relink_projects(db, allowed_project_ids=allowed)
    _log_relink(ctx, result)
    return result


def _log_relink(ctx: UserContext, result: dict) -> None:
    record_access_log(ctx, "relink_projects", "maintenance_doc_import", dict(result))
