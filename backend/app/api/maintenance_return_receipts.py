"""统一返还收货台账 API（2026-09-11 口径）。

页面登记入口：项目必选 → 需求单可选 → PN/数量必填 → 件况可选。
读走 page_maintenance + 项目范围；写走 action_maintenance_bad_return_manage。
失败尝试（权限拒绝 403 / 校验 422 / 版本冲突 409）同样 record_access_log 留痕。
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.api.maintenance_bad_returns import _real_operator
from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.auth import current_identity
from app.db import get_db
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_return_receipts as receipts

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class ReceiptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pn: str = Field(min_length=1, max_length=128)
    qty: int = Field(gt=0)
    wbdd_no: str | None = Field(default=None, max_length=64)
    part_id: int | None = None
    description: str | None = Field(default=None, max_length=256)
    condition: Literal["成品", "坏品", "废品"] | None = None
    note: str | None = Field(default=None, max_length=512)
    evidence_ref: str | None = Field(default=None, max_length=128)
    occurred_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


class ReceiptUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=256)
    project_id: str | None = None
    wbdd_no: str | None = Field(default=None, max_length=64)
    pn: str | None = Field(default=None, min_length=1, max_length=128)
    part_id: int | None = None
    description: str | None = Field(default=None, max_length=256)
    qty: int | None = Field(default=None, gt=0)
    condition: Literal["成品", "坏品", "废品"] | None = None
    note: str | None = Field(default=None, max_length=512)
    evidence_ref: str | None = Field(default=None, max_length=128)
    occurred_at: datetime | None = None


class ReceiptVoid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=256)


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, receipts.ReturnReceiptNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, receipts.ReturnReceiptConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, receipts.ReturnReceiptValidation):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if isinstance(exc, receipts.ReturnReceiptError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) == "55P03":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该项目正在被其他操作处理，请稍后重试",
        ) from exc
    raise exc


def _receipt_project(db: Session, receipt_id: str) -> str:
    row = db.get(MaintenanceRkdReturnLine, receipt_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "返还记录不存在")
    return row.project_id


@router.get(
    "/projects/stable/{project_id}/return-receipt-summary",
)
def receipt_summary(
    project_id: str = Path(min_length=1),
    db: Session = Depends(get_db),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        payload = receipts.receipt_summary(db, project_id=project_id)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_service_error(exc)
        raise
    record_access_log(
        ctx,
        "maintenance_return_receipt_summary",
        "maintenance_project",
        {"project_id": project_id, "project_total_qty": payload["project_total_qty"]},
    )
    return payload


@router.get("/projects/stable/{project_id}/return-receipts")
def search_receipts(
    project_id: str = Path(min_length=1),
    page: int = 1,
    page_size: int = 50,
    line_status: str = "active",
    q: str | None = None,
    source_order_id: str | None = None,
    source: str | None = None,
    db: Session = Depends(get_db),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        payload = receipts.search_receipts(
            db,
            project_id=project_id,
            page=max(page, 1),
            page_size=min(max(page_size, 1), 100),
            line_status=line_status,
            q=q,
            source_order_id=source_order_id,
            source=source,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _raise_service_error(exc)
        raise
    record_access_log(
        ctx,
        "maintenance_return_receipt_search",
        "maintenance_project",
        {"project_id": project_id, "total": payload["total"], "page": page},
    )
    return payload


@router.post(
    "/projects/stable/{project_id}/return-receipts",
    status_code=status.HTTP_201_CREATED,
)
def create_receipt(
    body: ReceiptCreate,
    project_id: str = Path(min_length=1),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        payload = receipts.register_receipt(
            db,
            project_id=project_id,
            pn=body.pn,
            qty=body.qty,
            wbdd_no=body.wbdd_no,
            part_id=body.part_id,
            description=body.description,
            condition=body.condition,
            note=body.note,
            evidence_ref=body.evidence_ref,
            occurred_at=body.occurred_at,
            idempotency_key=body.idempotency_key,
            operated_by=operator,
        )
        db.commit()
    except HTTPException as exc:
        db.rollback()
        record_access_log(
            ctx,
            "maintenance_return_receipt_create_failed",
            "maintenance_project",
            {"project_id": project_id, "pn": body.pn, "qty": body.qty,
             "outcome": f"http_{exc.status_code}", "detail": exc.detail},
        )
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "返还登记写入冲突，请重试"
        ) from exc
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)
        raise
    record_access_log(
        ctx,
        "maintenance_return_receipt_create",
        "maintenance_project",
        {"project_id": project_id, "receipt_id": payload["receipt_id"],
         "qty": payload["qty"], "replayed": payload["replayed"]},
    )
    return payload


@router.patch("/return-receipts/{receipt_id}")
def update_receipt(
    body: ReceiptUpdate,
    receipt_id: str = Path(min_length=1),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    project_id = _receipt_project(db, receipt_id)
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    if body.project_id and body.project_id != project_id:
        enforce_maintenance_project_access(
            db, project_id=body.project_id, ctx=ctx
        )
    operator = _real_operator(db, ident)
    updates = body.model_dump(
        exclude={"version", "reason"}, exclude_none=True
    )
    try:
        payload = receipts.update_receipt(
            db,
            receipt_id=receipt_id,
            expected_version=body.version,
            updates=updates,
            reason=body.reason,
            operated_by=operator,
        )
        db.commit()
    except HTTPException as exc:
        db.rollback()
        record_access_log(
            ctx,
            "maintenance_return_receipt_update_failed",
            "maintenance_return_receipt",
            {"receipt_id": receipt_id, "outcome": f"http_{exc.status_code}",
             "detail": exc.detail},
        )
        raise
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)
        raise
    record_access_log(
        ctx,
        "maintenance_return_receipt_update",
        "maintenance_return_receipt",
        {"receipt_id": receipt_id, "version": payload["version"]},
    )
    return payload


@router.post("/return-receipts/{receipt_id}/void")
def void_receipt(
    body: ReceiptVoid,
    receipt_id: str = Path(min_length=1),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    project_id = _receipt_project(db, receipt_id)
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        payload = receipts.void_receipt(
            db,
            receipt_id=receipt_id,
            expected_version=body.version,
            reason=body.reason,
            operated_by=operator,
        )
        db.commit()
    except HTTPException as exc:
        db.rollback()
        record_access_log(
            ctx,
            "maintenance_return_receipt_void_failed",
            "maintenance_return_receipt",
            {"receipt_id": receipt_id, "outcome": f"http_{exc.status_code}",
             "detail": exc.detail},
        )
        raise
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)
        raise
    record_access_log(
        ctx,
        "maintenance_return_receipt_void",
        "maintenance_return_receipt",
        {"receipt_id": receipt_id, "reason": body.reason},
    )
    return payload


@router.get("/return-receipts/{receipt_id}/audit")
def receipt_audit(
    receipt_id: str = Path(min_length=1),
    db: Session = Depends(get_db),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    project_id = _receipt_project(db, receipt_id)
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        items = receipts.receipt_audit(db, receipt_id=receipt_id)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_service_error(exc)
        raise
    record_access_log(
        ctx,
        "maintenance_return_receipt_audit",
        "maintenance_return_receipt",
        {"receipt_id": receipt_id, "entries": len(items)},
    )
    return {"receipt_id": receipt_id, "items": items}
