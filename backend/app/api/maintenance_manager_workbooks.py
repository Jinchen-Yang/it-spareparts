"""Own-scope project-manager monthly workbook v3 HTTP workflow (#206)."""

from __future__ import annotations

from app.authz.runtime import guard_legacy_execution

from app.authz.service import context_resolution

import asyncio
from contextlib import suppress
from datetime import date
from pathlib import Path as FilePath
import re
import threading

import anyio
from app.services.workbook_tasks import run_worker
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.maintenance import (
    _content_disposition,
    _parse_and_save_roundtrip_upload,
    _remove_roundtrip_temp,
    _wait_for_roundtrip_task_terminal,
)
from app.api.maintenance_project_operations import _real_operator
from app.auth import current_identity, current_role
from app.api import maintenance_project_scope as auth_transport
from app.services import maintenance_authorization as foundation
from app.services import maintenance_workflow_authorization as authorization
from app.services.maintenance_manager_workbook_adapter import read_monthly_workbook, validate_monthly_workbook
_RESOURCE = 'maintenance_manager_workbook'
from app.business_time import business_today
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services.maintenance_manager_workbook_adapter import (
    MaintenanceManagerWorkbookAdapter,
    ManagerWorkbookConflict,
    ManagerWorkbookInvalid,
    ManagerWorkbookNotFound,
    ManagerWorkbookPermissionError,
)
from app.services.maintenance_manager_workbook_v3 import ManagerWorkbookV3Error


router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_VALIDATE_LIMITER = threading.BoundedSemaphore(value=1)


class ManagerWorkbookApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validation_token: str = Field(min_length=1, max_length=64)
    data_version: str = Field(min_length=1, max_length=64)
    authorization_precondition: dict | None = None


def _parse_report_month(value: str | None) -> date:
    if value is None or not value.strip():
        return business_today().replace(day=1)
    text = value.strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "报告月份必须为 YYYY-MM",
        )
    try:
        parsed = date.fromisoformat(text + "-01")
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "报告月份必须为有效的 YYYY-MM",
        ) from None
    if parsed > business_today().replace(day=1):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "不能上传未来月份的月度工作簿",
        )
    return parsed


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


def _adapter(
    db: Session,
    *,
    ident: dict,
    ctx: UserContext,
) -> MaintenanceManagerWorkbookAdapter:
    return MaintenanceManagerWorkbookAdapter(
        db,
        user_ctx=ctx,
        operator=_real_operator(db, ident),
        as_of=business_today(),
    )


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, ManagerWorkbookV3Error):
        raise HTTPException(
            exc.status_code,
            {
                "message": str(exc),
                "issues": [
                    {
                        "code": issue.code,
                        "message": issue.message,
                        "sheet": issue.sheet,
                        "row": issue.row,
                        "column": issue.column,
                        "severity": issue.severity,
                    }
                    for issue in exc.issues
                ],
            },
        ) from exc
    if isinstance(exc, ManagerWorkbookPermissionError):
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    if isinstance(exc, ManagerWorkbookNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, ManagerWorkbookConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, ManagerWorkbookInvalid):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    raise exc


def _validation_payload(validation, batch) -> dict:
    return {
        "validation_token": batch.batch_id,
        "batch_id": batch.batch_id,
        "status": batch.status,
        "report_month": batch.report_month.isoformat(),
        "data_version": batch.data_version,
        "file_sha256": batch.file_sha256,
        "changes": {
            "service_periods": len(validation.service_period_changes),
            "planned_collection_milestones": len(validation.milestone_changes),
            "total": (
                len(validation.service_period_changes)
                + len(validation.milestone_changes)
            ),
        },
        "items": list((batch.plan_json or {}).get("preview_changes") or []),
        "warnings": [
            {
                "code": issue.code,
                "message": issue.message,
                "sheet": issue.sheet,
                "row": issue.row,
                "column": issue.column,
            }
            for issue in validation.warnings
        ],
        "errors": [
            {
                "code": issue.code,
                "message": issue.message,
                "sheet": issue.sheet,
                "row": issue.row,
                "column": issue.column,
            }
            for issue in validation.errors
        ],
        "unchanged": validation.unchanged,
        "can_apply": validation.can_apply and batch.status == "valid",
        "already_applied": batch.status == "applied",
        "expires_at": batch.expires_at.isoformat(),
    }


def _validate_in_worker(
    *,
    report_month: date,
    upload_path: str,
    ident: dict,
    ctx: UserContext,
    hmac_key: bytes,
) -> dict:
    db: Session | None = None
    try:
        db = SessionLocal()
        guard_legacy_execution(db, (_RESOURCE,))
        adapter = _adapter(db, ident=ident, ctx=ctx)
        content = FilePath(upload_path).read_bytes()
        validation, batch = adapter.validate(
            report_month,
            content,
            hmac_key=hmac_key,
        )
        payload = _validation_payload(validation, batch)
        db.commit()
        return payload
    except BaseException:
        if db is not None:
            with suppress(Exception):
                db.rollback()
        raise
    finally:
        if db is not None:
            with suppress(Exception):
                db.close()


async def _run_validation_worker(**kwargs) -> dict:
    worker = asyncio.create_task(
        anyio.to_thread.run_sync(
            lambda: _validate_in_worker(**kwargs),
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

@router.get("/project-manager/workbooks/v3/status")
def manager_workbook_status(
    response: Response,
    report_month: str | None = Query(default=None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    _no_store(response)
    month = _parse_report_month(report_month)
    try:
        if identity is not None:
            return read_monthly_workbook(sessionmaker(bind=db.get_bind()), identity, month,
                ctx=ctx, as_of=business_today(), status_only=True, authorization_view=view == 'authorization',
                authorization_precondition=foundation.decode_precondition(authorization_precondition) if view != 'authorization' else None)
        payload = _adapter(db, ident=ident, ctx=ctx).status(month)
    except Exception as exc:
        _raise_http(exc)
    record_access_log(
        ctx,
        "maintenance_manager_workbook_status",
        f"maintenance_manager_workbook:{month:%Y-%m}",
    )
    return payload


@router.get("/project-manager/workbooks/v3")
def export_manager_workbook(
    report_month: str | None = Query(default=None),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> object:
    month = _parse_report_month(report_month)
    try:
        if identity is not None:
            result = read_monthly_workbook(sessionmaker(bind=db.get_bind()), identity, month,
                ctx=ctx, as_of=business_today(), hmac_key=_hmac_key(), authorization_view=view == 'authorization',
                authorization_precondition=foundation.decode_precondition(authorization_precondition) if view != 'authorization' else None)
            if view == 'authorization':
                return result
            artifact, _snapshot = result
        else:
            artifact, _snapshot = _adapter(db, ident=ident, ctx=ctx).export(month, hmac_key=_hmac_key())
    except Exception as exc:
        _raise_http(exc)
    record_access_log(
        ctx,
        "download_maintenance_manager_workbook_v3",
        f"maintenance_manager_workbook:{month:%Y-%m}",
        {"export_id": artifact.export_id, "project_count": artifact.project_count},
    )
    return Response(
        content=artifact.content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(
                artifact.filename,
                ascii_fallback=f"maintenance_manager_workbook_{month:%Y-%m}.xlsx",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/project-manager/workbooks/v3/validate")
async def validate_manager_workbook_upload(
    request: Request,
    response: Response,
    report_month: str | None = Query(default=None),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    _no_store(response)
    month = _parse_report_month(report_month)
    if not _VALIDATE_LIMITER.acquire(blocking=False):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "已有项目经理工作簿正在校验，请稍后重试",
            headers={"Retry-After": "5"},
        )
    upload_path: str | None = None
    try:
        if identity is not None:
            from starlette.datastructures import UploadFile
            from types import SimpleNamespace
            from app.services import import_safety
            form = await request.form()
            file = form.get('file')
            if not isinstance(file, UploadFile):
                raise HTTPException(422, '缺少工作簿 file 上传字段')
            content = await import_safety.read_limited(file, 64 * 1024 * 1024)
            import_safety.validate_xlsx_zip(content, max_bytes=64 * 1024 * 1024)
            precondition = foundation.decode_precondition(form.get('authorization_precondition'))
            factory = sessionmaker(bind=db.get_bind())
            db.rollback()
            result = await run_worker(lambda: validate_monthly_workbook(
                factory, identity, month, content, ctx=ctx, as_of=business_today(), hmac_key=_hmac_key(),
                authorization_view=view == 'authorization', authorization_precondition=precondition))
            if view == 'authorization':
                return result
            validation, batch_values = result
            return _validation_payload(validation, SimpleNamespace(**batch_values))
        upload_path, _original_name = await _parse_and_save_roundtrip_upload(request)
        return await _run_validation_worker(
            report_month=month,
            upload_path=upload_path,
            ident=ident,
            ctx=ctx,
            hmac_key=_hmac_key(),
        )
    except Exception as exc:
        _raise_http(exc)
    finally:
        try:
            _remove_roundtrip_temp(upload_path)
        finally:
            _VALIDATE_LIMITER.release()


@router.post("/project-manager/workbooks/v3/apply")
def apply_manager_workbook(
    body: ManagerWorkbookApplyRequest,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    _action: None = Depends(
        auth_transport.legacy_gate(_RESOURCE, require_action(
            "action_maintenance_manager_workbook_apply",
            require_data="data_profit",
        ))
    ),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
) -> dict:
    _no_store(response)
    try:
        if identity is not None:
            if view == 'authorization':
                with foundation.read_context_snapshot(sessionmaker(bind=db.get_bind()), identity,
                        entry_id=authorization.MONTHLY_APPLY, params={'validation_token': body.validation_token,
                            'data_version': body.data_version}) as (_, _, batch, accesses):
                    return context_resolution(accesses, batch.contexts[0].ref)
            adapter = MaintenanceManagerWorkbookAdapter(db, user_ctx=ctx, operator=identity.username,
                as_of=business_today(), identity=identity, authorization_precondition=body.authorization_precondition)
        else:
            adapter = _adapter(db, ident=ident, ctx=ctx)
        result = adapter.apply(body.validation_token, data_version=body.data_version)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "应用时发生并发冲突，未写入任何变更",
        ) from exc
    except Exception as exc:
        db.rollback()
        _raise_http(exc)
    record_access_log(
        ctx,
        "apply_maintenance_manager_workbook_v3",
        f"maintenance_manager_workbook_batch:{body.validation_token}",
        {"changed_rows": result.get("changed_rows", 0)},
    )
    return result
