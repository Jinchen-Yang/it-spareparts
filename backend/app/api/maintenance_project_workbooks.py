"""Production HTTP loop for stable-project four-sheet workbooks."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from pathlib import Path as FilePath
import threading

import anyio
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.maintenance import (
    _content_disposition,
    _parse_and_save_roundtrip_upload,
    _remove_roundtrip_temp,
    _require_roundtrip_permissions,
    _wait_for_roundtrip_task_terminal,
)
from app.api.maintenance_project_operations import _real_operator
from app.api.maintenance_project_scope import (
    enforce_maintenance_project_access,
    require_maintenance_project_access,
)
from app.auth import current_identity, current_role
from app.authz.runtime import guard_legacy_execution
from app.api import maintenance_project_scope as auth_transport
from app.services import maintenance_authorization as foundation
from app.services import maintenance_workflow_authorization as authorization
from app.services import authorization_artifacts as artifacts
from app.services.maintenance_project_workbook_access import authorization_view
from app.services.workbook_tasks import run_worker
_RESOURCE = 'maintenance_project_roundtrip'
from app.business_time import business_today
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookValidation
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services.maintenance_project_workbook_adapter import (
    MaintenanceProjectWorkbookAdapter,
    cleanup_project_workbook_validations,
    workbook_preview,
)
from app.services.maintenance_project_workbook_v2 import ProjectWorkbookV2Error


router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PROJECT_WORKBOOK_VALIDATE_LIMITER = threading.BoundedSemaphore(value=1)
logger = logging.getLogger(__name__)


class WorkbookApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validation_token: str = Field(min_length=1, max_length=64)
    data_version: str = Field(min_length=1, max_length=64)
    authorization_precondition: dict | None = None


def _hmac_key() -> bytes:
    key = get_settings().secret_key.encode("utf-8")
    if len(key) < 16:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "服务端工作簿签名密钥配置无效",
        )
    return key


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _http_error(exc: ProjectWorkbookV2Error) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "message": str(exc),
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "sheet": issue.sheet,
                    "row": issue.row,
                    "column": issue.column,
                }
                for issue in exc.issues
            ],
        },
    )


def _safe_export_operator(ident: dict) -> str:
    return str(ident.get("sub") or ident.get("role") or "maintenance-export")[:64]


def _run_validation_retention_cleanup(db: Session) -> None:
    """Keep request behavior available when an opportunistic cleanup fails."""

    if not isinstance(db, Session):
        return
    try:
        cleanup_project_workbook_validations(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("项目工作簿校验记录机会式清理失败", exc_info=True)


def _validate_project_workbook_in_worker(
    *,
    project_id: str,
    upload_path: str,
    original_name: str,
    ident: dict,
    ctx: UserContext,
    hmac_key: bytes,
) -> dict:
    """Run the complete validation transaction on one worker-owned Session."""

    db: Session | None = None
    try:
        db = SessionLocal()
        guard_legacy_execution(db, (_RESOURCE,))
        _run_validation_retention_cleanup(db)
        # Cleanup ends its own transaction to release validation-row locks.
        # Reacquire and recheck before any identity, input or business access.
        guard_legacy_execution(db, (_RESOURCE,))
        operator = _real_operator(db, ident)
        content = FilePath(upload_path).read_bytes()
        adapter = MaintenanceProjectWorkbookAdapter(
            db,
            user_ctx=ctx,
            operator=operator,
            as_of=business_today(),
        )
        validation, issues, validation_id = adapter.validate(
            project_id,
            content,
            hmac_key=hmac_key,
        )
        workspace = adapter.load_workspace(project_id)
        exported_at = (
            validation.metadata.get("exported_at")
            if validation is not None
            else None
        )
        payload = {
            "validation_token": validation_id,
            "project_id": project_id,
            "data_version": workspace["data_version"],
            "filename": original_name,
            "preview": workbook_preview(
                workspace,
                data_version=str(workspace["data_version"]),
                exported_at=exported_at,
            ),
            "changes": {
                "collection_append": len(validation.creates) if validation else 0
            },
            "warnings": (
                ["未检测到新增回款；确认后将记录本月已更新"]
                if validation is not None and validation.unchanged
                else []
            ),
            "errors": [issue.message for issue in issues],
            "can_apply": validation is not None,
        }
        db.commit()
        return payload
    except BaseException:
        if db is not None:
            with suppress(Exception):
                db.rollback()
        raise
    finally:
        if db is not None:
            try:
                db.close()
            except BaseException:
                with suppress(Exception):
                    logger.warning(
                        "项目工作簿校验 worker 关闭数据库会话失败",
                        exc_info=True,
                    )


async def _run_project_workbook_validation_worker(**kwargs) -> dict:
    """Wait for terminal worker state even when the caller is cancelled."""

    worker = asyncio.create_task(
        anyio.to_thread.run_sync(
            lambda: _validate_project_workbook_in_worker(**kwargs),
            abandon_on_cancel=False,
        )
    )
    cancellation = await _wait_for_roundtrip_task_terminal(worker)
    try:
        result = worker.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    if cancellation is not None:
        raise cancellation
    return result


@router.get("/projects/stable/{project_id}/workbook")
def export_project_workbook(
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    _scope: None = Depends(auth_transport.legacy_project_scope(_RESOURCE)),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> object:
    if identity is None:
        _require_roundtrip_permissions(ctx)
    adapter = MaintenanceProjectWorkbookAdapter(
        db,
        user_ctx=ctx,
        operator=_safe_export_operator(ident),
        as_of=business_today(),
        identity=identity,
        authorization_precondition=foundation.decode_precondition(authorization_precondition) if identity is not None and view != "authorization" else None,
    )
    try:
        if identity is not None and view == 'authorization':
            return authorization_view(adapter, authorization.PROJECT_WORKBOOK_DOWNLOAD, {'project_id': project_id})
        artifact = adapter.export(project_id, hmac_key=_hmac_key())
        db.commit()
    except ProjectWorkbookV2Error as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "工作簿导出记录冲突，请重试"
        ) from exc
    except Exception:
        db.rollback()
        raise
    return Response(
        content=artifact.content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(
                artifact.filename,
                ascii_fallback="maintenance_project_workbook.xlsx",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/projects/stable/{project_id}/workbook/validate")
async def validate_project_workbook_upload(
    request: Request,
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    _action: None = Depends(
        auth_transport.legacy_gate(_RESOURCE, require_action(
            "action_maintenance_roundtrip_apply",
            require_data="data_profit",
        ))
    ),
    _scope: None = Depends(auth_transport.legacy_project_scope(_RESOURCE)),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    _no_store(response)
    if identity is None:
        _require_roundtrip_permissions(ctx)
    if not _PROJECT_WORKBOOK_VALIDATE_LIMITER.acquire(blocking=False):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "已有项目工作簿正在校验，请稍后重试",
            headers={"Retry-After": "5"},
        )
    upload_path: str | None = None
    try:
        if identity is not None:
            from starlette.datastructures import UploadFile
            from app.services import import_safety
            form = await request.form()
            file = form.get('file')
            if not isinstance(file, UploadFile):
                raise HTTPException(422, '缺少工作簿 file 上传字段')
            content = await import_safety.read_limited(file, 64 * 1024 * 1024)
            precondition = foundation.decode_precondition(form.get('authorization_precondition'))
            factory = sessionmaker(bind=db.get_bind())
            db.rollback()
            if view != 'authorization':
                artifacts.begin_artifact_protocol(db.get_bind())
            def canonical_worker():
                with factory() as worker_db:
                    adapter = MaintenanceProjectWorkbookAdapter(worker_db, user_ctx=ctx,
                        operator=identity.username, as_of=business_today(), identity=identity,
                        authorization_precondition=precondition)
                    if view == 'authorization':
                        import hashlib
                        return authorization_view(adapter, authorization.PROJECT_WORKBOOK_VALIDATE,
                            {'project_id': project_id, 'file_sha256': hashlib.sha256(content).hexdigest()})
                    try:
                        validation, issues, bid = adapter.validate(project_id, content, hmac_key=_hmac_key())
                        workspace = adapter.load_workspace(project_id)
                        payload = {'validation_token': bid, 'project_id': project_id,
                            'data_version': workspace['data_version'], 'filename': file.filename or '',
                            'preview': workbook_preview(workspace, data_version=str(workspace['data_version']),
                                exported_at=validation.metadata.get('exported_at') if validation else None),
                            'changes': {'collection_append': len(validation.creates) if validation else 0},
                            'warnings': ['未检测到新增回款；确认后将记录本月已更新'] if validation and validation.unchanged else [],
                            'errors': [issue.message for issue in issues], 'can_apply': validation is not None}
                        worker_db.commit()
                        return payload
                    except Exception:
                        worker_db.rollback()
                        raise
            return await run_worker(canonical_worker)
        upload_path, original_name = await _parse_and_save_roundtrip_upload(request)
        return await _run_project_workbook_validation_worker(
            project_id=project_id,
            upload_path=upload_path,
            original_name=original_name,
            ident=ident,
            ctx=ctx,
            hmac_key=_hmac_key(),
        )
    except ProjectWorkbookV2Error as exc:
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "工作簿校验记录冲突，请重新上传",
        ) from exc
    finally:
        try:
            _remove_roundtrip_temp(upload_path)
        finally:
            _PROJECT_WORKBOOK_VALIDATE_LIMITER.release()


@router.post("/projects/stable/{project_id}/workbook/apply")
def apply_project_workbook_plan(
    body: WorkbookApplyRequest,
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    _action: None = Depends(
        auth_transport.legacy_gate(_RESOURCE, require_action(
            "action_maintenance_roundtrip_apply",
            require_data="data_profit",
        ))
    ),
    _scope: None = Depends(auth_transport.legacy_project_scope(_RESOURCE)),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
) -> dict:
    _no_store(response)
    if identity is None:
        _require_roundtrip_permissions(ctx)
    operator = identity.username if identity is not None else _real_operator(db, ident)
    if identity is None:
        _run_validation_retention_cleanup(db)
    adapter = MaintenanceProjectWorkbookAdapter(
        db,
        user_ctx=ctx,
        operator=operator,
        as_of=business_today(),
        identity=identity, authorization_precondition=body.authorization_precondition,
    )
    try:
        if identity is not None and view == 'authorization':
            return authorization_view(adapter, authorization.PROJECT_WORKBOOK_APPLY,
                {'project_id': project_id, 'validation_token': body.validation_token, 'data_version': body.data_version})
        result, state = adapter.apply_validation(
            project_id,
            body.validation_token,
            data_version=body.data_version,
        )
        payload = {
            "applied": True,
            "changed_rows": result.created,
            "data_version": state.data_version,
            "warnings": [],
        }
        db.commit()
        return payload
    except ProjectWorkbookV2Error as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "工作簿应用与现有数据冲突，未写入任何变更",
        ) from exc
    except Exception:
        db.rollback()
        raise


@router.get("/workbook-validations/{validation_id}/errors.xlsx")
def download_project_workbook_errors(
    validation_id: str = Path(..., min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    _action: None = Depends(
        auth_transport.legacy_gate(_RESOURCE, require_action(
            "action_maintenance_roundtrip_apply",
            require_data="data_profit",
        ))
    ),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> object:
    if identity is None:
        _require_roundtrip_permissions(ctx)
    if identity is not None:
        adapter = MaintenanceProjectWorkbookAdapter(db, user_ctx=ctx, operator=identity.username,
            as_of=business_today(), identity=identity,
            authorization_precondition=foundation.decode_precondition(authorization_precondition) if view != "authorization" else None)
        if view == 'authorization':
            return authorization_view(adapter, authorization.PROJECT_WORKBOOK_ERRORS, {'validation_id': validation_id})
        content = adapter.load_error_workbook(validation_id)
    else:
        project_id = db.scalar(
            select(MaintenanceProjectWorkbookValidation.project_id).where(
                MaintenanceProjectWorkbookValidation.validation_id == validation_id
            )
        )
        if project_id is not None:
            enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
        operator = _real_operator(db, ident)
        _run_validation_retention_cleanup(db)
        adapter = MaintenanceProjectWorkbookAdapter(
            db,
            user_ctx=ctx,
            operator=operator,
            as_of=business_today(),
        )
        content = adapter.load_error_workbook(validation_id)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "错误工作簿不存在或已过期")
    record_access_log(
        ctx,
        "download_workbook_errors",
        f"maintenance_workbook_validation:{validation_id}",
        {"validation_id": validation_id},
    )
    return Response(
        content=content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(
                f"maintenance_workbook_errors_{validation_id[:12]}.xlsx"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
