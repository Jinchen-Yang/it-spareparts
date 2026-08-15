"""维保台账工作簿导入 API（B2）：preview → apply → 批次状态。

路由（main.py 以 api_prefix 注册）：
- ``POST /maintenance/ledger-imports/preview``（multipart .xlsx，16 MiB；Idempotency-Key 8–128）
- ``POST /maintenance/ledger-imports/{batch_id}/apply``
- ``GET  /maintenance/ledger-imports/{batch_id}``

台账是商务线唯一事实源；写端点要求 ``page_maintenance`` +
``action_maintenance_ledger_import`` + ``data_profit``（合同额属经营数据）。
preview 零 canonical 写入；apply 幂等同步 project/contract/milestone。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from starlette.datastructures import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_identity, current_role
from app.db import get_db
from app.models.maintenance_ledger import MaintenanceLedgerImportBatch
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    require_action,
    require_page,
)
from app.services import maintenance_ledger as ledger

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
MAX_PREVIEW_BYTES = ledger.MAX_PREVIEW_BYTES
_ACTION_KEY = "action_maintenance_ledger_import"


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, ledger.LedgerBatchError):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "batch_conflict", "message": str(exc)},
        )
    if isinstance(exc, ledger.LedgerParseError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_ledger", "message": str(exc)},
        )
    raise exc


def _require_data_profit(ctx: UserContext, db: Session) -> None:
    from app import permissions as _perm

    user = db.scalar(
        select(SysUser).where(
            SysUser.username == ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )
    if user is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": "无权执行此操作"},
        )
    graph = _perm.effective_for_user(user)
    if not _perm.runtime_safe(graph).get("data_profit", False):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": "台账导入要求同时具备利润数据可见权限"},
        )


def _real_operator(db: Session, ident: dict) -> str:
    value = ident.get("username") or ident.get("sub") or "unknown"
    user = db.scalar(select(SysUser).where(SysUser.username == value))
    if user is not None and user.display_name:
        return f"{user.display_name}（{value}）"
    return str(value)


def _preview_preflight(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_PREVIEW_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {"code": "upload_too_large", "message": "台账工作簿超过上传安全上限"},
        )
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"code": "unsupported_media_type", "message": "只接受 multipart/form-data 的 .xlsx 文件"},
        )


@router.post("/ledger-imports/preview")
async def preview_ledger_import(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
    _preflight: None = Depends(_preview_preflight),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    _require_data_profit(ctx, db)
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
            {"code": "unsupported_media_type", "message": "只接受 .xlsx 台账工作簿"},
        )
    idempotency_key = request.headers.get("idempotency-key", "")
    if not (8 <= len(idempotency_key) <= 128):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request", "message": "Idempotency-Key 必填且长度必须在 8–128 字符之间"},
        )
    data = await file.read()
    if len(data) > MAX_PREVIEW_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {"code": "upload_too_large", "message": "台账工作簿超过上传安全上限"},
        )
    try:
        parsed = ledger.parse_ledger_workbook(data, filename)
        batch_id = ledger.store_preview(db, parsed, _real_operator(db, ident))
    except ledger.LedgerParseError as exc:
        _raise_http(exc)
    return {
        "batch_id": batch_id,
        "file_hash": parsed["file_hash"],
        "source_kind": parsed["source_kind"],
        "contract_rows": len(parsed["contract_rows"]),
        "plan_rows": len(parsed["plan_rows"]),
        "expense_rows": len(parsed["expense_rows"]),
    }


@router.post("/ledger-imports/{batch_id}/apply")
def apply_ledger_import(
    batch_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _require_data_profit(ctx, db)
    try:
        summary = ledger.apply_batch(db, batch_id, _real_operator(db, ident))
    except ledger.LedgerBatchError as exc:
        db.rollback()
        _raise_http(exc)
    return {"batch_id": batch_id, **summary}


@router.get("/ledger-imports/{batch_id}")
def get_ledger_import(
    batch_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
) -> dict:
    batch = db.get(MaintenanceLedgerImportBatch, batch_id)
    if batch is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "台账批次不存在"},
        )
    return {
        "batch_id": batch.batch_id,
        "file_hash": batch.file_hash,
        "filename": batch.filename,
        "source_kind": batch.source_kind,
        "uploaded_by": batch.uploaded_by,
        "uploaded_at": batch.uploaded_at.isoformat() if batch.uploaded_at else None,
        "contract_rows": batch.contract_rows,
        "plan_rows": batch.plan_rows,
        "expense_rows": batch.expense_rows,
        "issue_rows": batch.issue_rows,
        "status": batch.status,
        "applied_by": batch.applied_by,
        "applied_at": batch.applied_at.isoformat() if batch.applied_at else None,
        "report": batch.report_json,
    }
