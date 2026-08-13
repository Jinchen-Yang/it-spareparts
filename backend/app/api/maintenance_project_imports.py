"""Tritium project table import: preview and apply (admin-only)."""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.auth import current_identity, require_admin
from app.db import get_db
from app.security import record_access_log, UserContext, get_current_user_context
from app.services import maintenance_project_imports as imports

router = APIRouter(prefix="/maintenance/project-imports", tags=["maintenance"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_READ_CHUNK = 64 * 1024


def _real_operator(db: Session, ident: dict) -> str:
    """High-risk project-creating writes reject shared/fallback tokens,
    mirroring the demand-deletion operator gate."""
    from app.models.system import SysUser
    from sqlalchemy import select
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "请使用实名系统账号")
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(SysUser.username == username, SysUser.is_active.is_(True))
    )
    if not username or user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "请使用实名系统账号")
    return username


async def _read_capped(file: UploadFile) -> bytes:
    """Stream-read with a hard cap so oversized uploads never buffer fully."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "文件大小不能超过 10 MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/preview")
async def preview_project_import(
    file: UploadFile,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _admin: str = Depends(require_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operated_by = _real_operator(db, ident)
    content = await _read_capped(file)
    result = imports.preview_import(
        db, content, file.filename or "unknown.xls", operated_by
    )
    record_access_log(
        ctx,
        "tritium_import_preview",
        "maintenance",
        {"filename": (file.filename or "")[:256], "status": result["status"]},
    )
    return result


@router.get("/{import_id}")
def get_project_import(
    import_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
) -> dict:
    batch = db.get(imports.MaintenanceProjectImportBatch, import_id)
    if not batch:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "导入批次不存在")
    return {
        "import_id": batch.id,
        "filename": batch.filename,
        "file_hash": batch.file_hash,
        "status": batch.status,
        "preview": batch.preview_json,
        "applied_at": batch.applied_at.isoformat() if batch.applied_at else None,
        "operated_by": batch.operated_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
    }


@router.post("/{import_id}/apply")
def apply_project_import(
    import_id: int,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _admin: str = Depends(require_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operated_by = _real_operator(db, ident)
    try:
        result = imports.apply_import(db, import_id, operated_by)
    except imports.ImportApplyConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    record_access_log(
        ctx,
        "tritium_import_apply",
        "maintenance",
        {"import_id": import_id, **result},
    )
    return result
