"""Own-scope project-manager monthly workbook v3 HTTP workflow (#206)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date
from pathlib import Path as FilePath
import re
import threading

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.maintenance import (
    _content_disposition,
    _parse_and_save_roundtrip_upload,
    _remove_roundtrip_temp,
    _wait_for_roundtrip_task_terminal,
)
from app.api.maintenance_project_operations import _real_operator
from app.auth import current_identity, current_role
from app.business_time import business_today
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
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


def _parse_report_month(value: str | None) -> date:
    if value is None or not value.strip():
        return business_today().replace(day=1)
    text = value.strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "报告月份必须为 YYYY-MM",
        )
    parsed = date.fromisoformat(text + "-01")
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
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _no_store(response)
    month = _parse_report_month(report_month)
    try:
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
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> Response:
    month = _parse_report_month(report_month)
    try:
        artifact, _snapshot = _adapter(db, ident=ident, ctx=ctx).export(
            month,
            hmac_key=_hmac_key(),
        )
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
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
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
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _no_store(response)
    try:
        result = _adapter(db, ident=ident, ctx=ctx).apply(
            body.validation_token,
            data_version=body.data_version,
        )
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
