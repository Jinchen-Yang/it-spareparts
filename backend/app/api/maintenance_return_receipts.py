"""统一返还收货台账 API（2026-09-11 口径）。

页面登记入口：项目必选 → 需求单可选 → PN/数量必填 → 件况可选。
读走 page_maintenance + 项目范围；写走 action_maintenance_bad_return_manage。
失败尝试（含依赖前拒绝、校验与版本冲突）独立事务落 sys_audit_log。
"""

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.maintenance_bad_returns import _real_operator
from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.auth import current_identity, verify_token_db
from app.db import SessionLocal, get_db
from app.maintenance_boss import require_maintenance_boss
from app.models.system import SysAuditLog, SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_return_receipts as receipts
from app.services.maintenance_project_assignments import can_access_project


def _record_failed_attempt(request: Request, *, action: str, status_code: int) -> None:
    """Independent durable audit; never store request bodies/tokens/error text.

    The legacy audit table uses integer entity ids, so 0 denotes an attempted
    request. Canonical UUID path identifiers live in the structured detail.
    """
    detail = {"status_code": status_code, "method": request.method}
    for field in ("project_id", "receipt_id", "batch_id"):
        value = request.path_params.get(field)
        if value:
            try:
                detail[field] = str(UUID(str(value)))
            except ValueError:
                pass
    with SessionLocal() as audit_db:
        operator = None
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token:
            try:
                ident = verify_token_db(token, audit_db)
                if ident.get("authn") == "sys_user" and not ident.get("fb"):
                    operator = audit_db.scalar(select(SysUser.username).where(
                        SysUser.username == ident.get("sub"), SysUser.is_active.is_(True)
                    ))
            except HTTPException:
                pass
        audit_db.add(SysAuditLog(
            entity_type="return_receipt_attempt", entity_id=0,
            action=f"{action}_failed"[:32], before_json=None, after_json=detail,
            reason="返还台账请求失败", operated_by=operator,
        ))
        audit_db.commit()


class ReceiptAuditRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def audited(request: Request):
            try:
                return await handler(request)
            except Exception as exc:
                code = (422 if isinstance(exc, RequestValidationError) else
                        exc.status_code if isinstance(exc, StarletteHTTPException) else 500)
                try:
                    await run_in_threadpool(
                        _record_failed_attempt, request, action=self.name, status_code=code
                    )
                except Exception:
                    logging.getLogger(__name__).error("Return receipt failure audit unavailable")
                    raise HTTPException(503, "审计服务暂不可用，请稍后重试") from None
                if code == 500:
                    raise HTTPException(500, "返还台账操作失败，请稍后重试") from None
                raise

        return audited


router = APIRouter(
    prefix="/maintenance", tags=["maintenance"], route_class=ReceiptAuditRoute,
    dependencies=[Depends(require_maintenance_boss)],
)


class ReceiptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pn: str = Field(min_length=1, max_length=128)
    qty: int = Field(gt=0, lt=10**11, strict=True)
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

    version: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=256)
    project_id: str | None = None
    wbdd_no: str | None = Field(default=None, max_length=64)
    pn: str | None = Field(default=None, min_length=1, max_length=128)
    part_id: int | None = None
    description: str | None = Field(default=None, max_length=256)
    qty: int | None = Field(default=None, gt=0, lt=10**11, strict=True)
    condition: Literal["成品", "坏品", "废品"] | None = None
    note: str | None = Field(default=None, max_length=512)
    evidence_ref: str | None = Field(default=None, max_length=128)
    occurred_at: datetime | None = None


class ReceiptVoid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, strict=True)
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


def _receipt_project(db: Session, receipt_id: str, *, target_project_id: str | None = None) -> str:
    # Lock before checking project scope: a concurrent transfer must not make
    # an authorization decision refer to the previous owner project.
    receipts.lock_receipt_context(db)
    try:
        row = receipts._load_receipt(db, receipt_id, lock=True, target_project_id=target_project_id)
    except receipts.ReturnReceiptError as exc:
        _raise_service_error(exc)
        raise
    return row.project_id


@router.get("/projects/stable/{project_id}/return-receipt-demands")
def receipt_demand_candidates(
    project_id: str = Path(min_length=1, max_length=36),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    q: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        return receipts.demand_candidates(
            db, project_id=project_id, page=page, page_size=page_size, q=q,
        )
    except receipts.ReturnReceiptError as exc:
        _raise_service_error(exc)
        raise


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
    unassigned: bool = False,
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
            unassigned=unassigned,
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
    receipts.lock_receipt_context(db)
    receipts.lock_receipt_projects(db, {project_id})
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
    except HTTPException:
        db.rollback()
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
    project_id = _receipt_project(db, receipt_id, target_project_id=body.project_id)
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    if body.project_id and body.project_id != project_id:
        enforce_maintenance_project_access(
            db, project_id=body.project_id, ctx=ctx
        )
    operator = _real_operator(db, ident)
    updates = body.model_dump(
        exclude={"version", "reason"}, exclude_unset=True
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
    except HTTPException:
        db.rollback()
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
    except HTTPException:
        db.rollback()
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
        # A transferred receipt's earlier snapshots/reasons belong to the
        # former project too. Current-project access does not grant that scope.
        access = {project_id: True}
        visible_items = []
        for item in items:
            project_ids = {item["project_id"]}
            for snapshot in (item["before_json"], item["after_json"]):
                if snapshot and snapshot.get("project_id"):
                    project_ids.add(snapshot["project_id"])
            for historical_project in project_ids - access.keys():
                access[historical_project] = can_access_project(
                    db, project_id=historical_project, user_ctx=ctx
                )
            if all(access[key] for key in project_ids):
                visible_items.append(item)
        items = visible_items
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
