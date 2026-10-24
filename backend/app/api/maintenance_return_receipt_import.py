"""Asynchronous standard return receipt imports on the dedicated doc channel."""

from pathlib import Path
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse

from app.api.maintenance_bad_returns import _real_operator
from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.api import maintenance_project_scope as auth_transport
from app.services import maintenance_return_import_authorization as canonical
from app.services import maintenance_authorization as foundation
from app.services.maintenance_workflow_authorization import multipart_precondition
from app.services.authorization_downloads import stream_materialized_bytes, attachment_headers
from app.api.maintenance_return_receipts import ReceiptAuditRoute
from app.auth import current_identity
from app.db import get_db
from app.etl.pipeline import ArchiveError
from app.maintenance_boss import require_maintenance_boss
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import import_safety
from app.services import maintenance_return_receipt_import as service

router = APIRouter(
    prefix="/maintenance/doc-imports/return-receipts/jobs",
    route_class=ReceiptAuditRoute,
    dependencies=[
        Depends(require_maintenance_boss),
        Depends(auth_transport.legacy_page(canonical.RESOURCE,"page_maintenance")),
        Depends(auth_transport.legacy_gate(canonical.RESOURCE,require_action("action_maintenance_bad_return_manage"))),
    ],
)


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_hash: str = Field(min_length=64, max_length=64)
    preview_token: str = Field(min_length=32, max_length=128)
    confirm_changes: bool = False
    confirm_possible_duplicates: bool = Field(default=False, strict=True)
    reason: str | None = Field(default=None, max_length=256)
    authorization_precondition: dict | None = None


def _canonical_error(db,exc):
    db.rollback()
    if isinstance(exc,service.ImportError):
        raise HTTPException(exc.status,{'code':exc.code,'message':str(exc)},headers={'Cache-Control':'no-store'}) from exc
    raise HTTPException(500,{'code':'archive_failed','message':'原件核验失败，未写入业务数据'},headers={'Cache-Control':'no-store'}) from exc


def _owned(db, batch_id, operator, *, lock=False):
    batch = service._load(db, batch_id, lock=lock)
    if batch.uploaded_by != operator:
        raise service.ImportError(
            "permission_denied", "只能读取和操作本人上传的返件任务", 403
        )
    return batch


def _error(db, exc, ctx=None, batch_id=None):
    db.rollback()
    if ctx:
        record_access_log(
            ctx,
            "return_receipt_import_denied",
            "maintenance_doc_import",
            {"batch_id": batch_id, "code": getattr(exc, "code", "archive_failed")},
        )
    if isinstance(exc, service.ImportError):
        if batch_id and exc.code == "stale_preview":
            batch = service._load(db, batch_id, lock=True)
            if batch.report_json["status"] == "ready":
                service._state(
                    batch,
                    status="failed",
                    error={"code": exc.code, "message": str(exc)},
                    preview_token=None,
                )
                batch.status = "failed"
                db.commit()
        raise HTTPException(
            exc.status, {"code": exc.code, "message": str(exc)}
        ) from exc
    raise HTTPException(
        500,
        {"code": "archive_failed", "message": "原件归档或核验失败，未写入返件，可重试"},
    ) from exc


@router.post("", status_code=202)
async def upload(
    request: Request,
    background: BackgroundTasks,
    response: Response,
    db: Session = Depends(get_db),
    ident=Depends(current_identity),
    verified=Depends(auth_transport.identity_for(canonical.RESOURCE)),
    view: Literal["authorization"] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
):
    response.headers["Cache-Control"] = "no-store"
    operator = _real_operator(db, ident) if verified is None else None
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 128:
        raise HTTPException(
            422,
            {"code": "invalid_request", "message": "Idempotency-Key 必填，长度 8–128"},
        )
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > service.MAX_PREVIEW_BYTES:
        raise HTTPException(
            413, {"code": "upload_too_large", "message": "文件超过上传上限"}
        )
    if (
        not request.headers.get("content-type", "")
        .lower()
        .startswith("multipart/form-data")
    ):
        raise HTTPException(
            415,
            {
                "code": "invalid_request",
                "message": "只接受 multipart/form-data 的 xlsx 文件",
            },
        )
    try:
        form = await request.form()
        if verified is not None:
            authorization_precondition=multipart_precondition(authorization_precondition,form.get('authorization_precondition'))
        file = form.get("file")
        if not isinstance(file, UploadFile) or not (
            file.filename or ""
        ).lower().endswith(".xlsx"):
            raise HTTPException(
                415, {"code": "invalid_doc", "message": "请选择标准入库单 xlsx 文件"}
            )
        data = await import_safety.read_limited(file, service.MAX_PREVIEW_BYTES)
        # Cheap envelope validation precedes durable enqueue; full CRC and
        # streaming row validation run in the asynchronous worker.
        import_safety.validate_xlsx_zip(data, max_bytes=service.MAX_PREVIEW_BYTES)
        if verified is not None:
            outcome = await run_in_threadpool(canonical.submit,db,verified,data=data,filename=file.filename,key=key,
                authorization_view=view=='authorization',precondition=foundation.decode_precondition(authorization_precondition))
            if view=='authorization':
                response.status_code=200
                return outcome
            result,created,generation=outcome
            if created:background.add_task(service.run_job,result['batch_id'],generation)
            return result
        batch, created = await run_in_threadpool(
            service.enqueue, db, data, file.filename, operator, key
        )
    except import_safety.UploadSafetyError as exc:
        raise HTTPException(422, {"code": "invalid_doc", "message": str(exc)}) from exc
    except (service.ImportError, ArchiveError) as exc:
        if verified is not None:_canonical_error(db,exc)
        _error(db, exc, ctx)
    if created:
        background.add_task(
            service.run_job, batch.batch_id, batch.report_json["generation"]
        )
    return {"batch_id": batch.batch_id, "status": batch.report_json["status"]}


@router.get("/{batch_id}")
def get_job(
    batch_id: str,
    response: Response,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    ident=Depends(current_identity),
    verified=Depends(auth_transport.identity_for(canonical.RESOURCE)),
    view: Literal["authorization"] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        if verified is not None:
            return canonical.read(db,verified,entry=canonical.READ,batch_id=batch_id,offset=offset,limit=limit,
                authorization_view=view=='authorization',precondition=foundation.decode_precondition(authorization_precondition))
        batch = _owned(db, batch_id, _real_operator(db, ident))
        return service.public_job(
            db,
            batch,
            allowed_project_ids=resolve_visible_project_ids(db, ctx),
            offset=offset,
            limit=limit,
        )
    except service.ImportError as exc:
        if verified is not None:_canonical_error(db,exc)
        _error(db, exc, ctx, batch_id)


@router.post("/{batch_id}/cancel")
def cancel(
    batch_id: str,
    response: Response,
    db: Session = Depends(get_db),
    ident=Depends(current_identity),
    verified=Depends(auth_transport.identity_for(canonical.RESOURCE)),
    view: Literal["authorization"] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
):
    response.headers['Cache-Control']='no-store'
    try:
        if verified is not None:
            return canonical.command(db,verified,entry=canonical.CANCEL,batch_id=batch_id,
                authorization_view=view=='authorization',precondition=foundation.decode_precondition(authorization_precondition))
        _owned(db, batch_id, _real_operator(db, ident))
        batch = service.control(db, batch_id, "cancel")
        return {"batch_id": batch.batch_id, "status": batch.report_json["status"]}
    except service.ImportError as exc:
        if verified is not None:_canonical_error(db,exc)
        _error(db, exc, ctx, batch_id)


@router.get("/{batch_id}/original")
def original(
    batch_id: str,
    response: Response,
    db: Session = Depends(get_db),
    ident=Depends(current_identity),
    verified=Depends(auth_transport.identity_for(canonical.RESOURCE)),
    view: Literal["authorization"] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
):
    response.headers['Cache-Control']='no-store'
    try:
        if verified is not None:
            result=canonical.read(db,verified,entry=canonical.RAW,batch_id=batch_id,
                authorization_view=view=='authorization',precondition=foundation.decode_precondition(authorization_precondition))
            if view=='authorization':return result
            data,filename,lineage=result
            return stream_materialized_bytes(data,db.get_bind(),verified,lineage,
                media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers=attachment_headers(filename))
        batch = _owned(db, batch_id, _real_operator(db, ident))
        service.public_job(
            db, batch, allowed_project_ids=resolve_visible_project_ids(db, ctx), limit=1
        )
        service.verify_archive(batch.report_json["storage_path"], batch.file_hash)
        return FileResponse(
            batch.report_json["storage_path"],
            filename=Path(batch.filename).name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Cache-Control": "no-store"},
        )
    except (service.ImportError, ArchiveError) as exc:
        if verified is not None:_canonical_error(db,exc)
        _error(db, exc, ctx, batch_id)


@router.post("/{batch_id}/retry", status_code=202)
def retry(
    batch_id: str,
    response: Response,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    ident=Depends(current_identity),
    verified=Depends(auth_transport.identity_for(canonical.RESOURCE)),
    view: Literal["authorization"] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
):
    response.headers['Cache-Control']='no-store'
    try:
        if verified is not None:
            result=canonical.command(db,verified,entry=canonical.RETRY,batch_id=batch_id,
                authorization_view=view=='authorization',precondition=foundation.decode_precondition(authorization_precondition))
            if view=='authorization':
                response.status_code=200
                return result
            background.add_task(service.run_job,batch_id,result['generation'])
            return result
        _owned(db, batch_id, _real_operator(db, ident))
        batch = service.control(db, batch_id, "retry")
        background.add_task(
            service.run_job, batch.batch_id, batch.report_json["generation"]
        )
        return {"batch_id": batch.batch_id, "status": batch.report_json["status"]}
    except service.ImportError as exc:
        if verified is not None:_canonical_error(db,exc)
        _error(db, exc, ctx, batch_id)


@router.post("/{batch_id}/apply")
def apply(
    batch_id: str,
    body: ApplyRequest,
    response: Response,
    db: Session = Depends(get_db),
    ident=Depends(current_identity),
    verified=Depends(auth_transport.identity_for(canonical.RESOURCE)),
    view: Literal["authorization"] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    ctx: UserContext = Depends(get_current_user_context),
):
    response.headers['Cache-Control']='no-store'
    try:
        if verified is not None:
            return canonical.command(db,verified,entry=canonical.APPLY,batch_id=batch_id,
                command=body.model_dump(exclude={'authorization_precondition'}),authorization_view=view=='authorization',
                precondition=body.authorization_precondition or foundation.decode_precondition(authorization_precondition))
        operator = _real_operator(db, ident)
        service.ledger.lock_receipt_context(db)
        _owned(db, batch_id, operator)
        return service.apply(
            db,
            batch_id,
            operator,
            **body.model_dump(exclude={"authorization_precondition"}),
            scope_provider=lambda: resolve_visible_project_ids(db, ctx),
        )
    except (service.ImportError, ArchiveError) as exc:
        if verified is not None:_canonical_error(db,exc)
        _error(db, exc, ctx, batch_id)
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        batch = service._load(db, batch_id, lock=True)
        if batch.report_json["status"] == "ready":
            service._state(
                batch,
                status="failed",
                preview_token=None,
                error={
                    "code": "apply_failed",
                    "message": "应用失败，整批已回滚，请重新预览",
                },
            )
            batch.status = "failed"
            db.commit()
        raise HTTPException(
            500, {"code": "apply_failed", "message": "应用失败，请重新查询任务状态"}
        ) from None
