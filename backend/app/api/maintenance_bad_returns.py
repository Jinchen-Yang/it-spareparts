"""Controlled bad-part return obligations and return-document workflow."""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import current_identity, current_role, require_admin
from app.db import get_db
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_bad_returns as returns


router = APIRouter(prefix="/maintenance", tags=["maintenance"])


ReturnClassification = Literal["required", "exempt", "pending_category"]
BadReturnStatus = Literal[
    "draft",
    "submitted",
    "in_transit",
    "warehouse_confirmed",
]


class ReturnObligationSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    q: str | None = Field(default=None, max_length=256)
    classifications: list[ReturnClassification] = Field(
        default_factory=lambda: ["required", "exempt", "pending_category"],
        min_length=1,
        max_length=3,
    )
    active_only: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class BadReturnLineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    obligation_id: str = Field(min_length=1, max_length=36)
    quantity: Decimal = Field(gt=0)


class BadReturnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    idempotency_key: str = Field(min_length=8, max_length=128)
    lines: list[BadReturnLineCreate] = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=32767)
    reason: str = Field(min_length=1, max_length=1000)


class BadReturnSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    statuses: list[BadReturnStatus] = Field(
        default_factory=lambda: [
            "draft",
            "submitted",
            "in_transit",
            "warehouse_confirmed",
        ],
        min_length=1,
        max_length=4,
    )
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class BadReturnCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class BadReturnInTransit(BadReturnCommand):
    logistics_reference: str = Field(min_length=1, max_length=128)


class BadReturnWarehouseConfirm(BadReturnCommand):
    warehouse_reference: str = Field(min_length=1, max_length=128)
    inbound_reference: str | None = Field(default=None, min_length=1, max_length=128)


class ResolveReturnCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)
    category_id: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


def _real_operator(db: Session, ident: dict) -> str:
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "坏件返还写入必须使用实名系统账号")
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
        )
    )
    if not username or user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "坏件返还写入必须使用实名系统账号")
    return username


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, returns.BadReturnPermissionError):
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    if isinstance(exc, returns.BadReturnConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, returns.BadReturnError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    raise exc


@router.post("/return-obligations/search")
def search_return_obligations(
    body: ReturnObligationSearch,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        returns.consume_pending_return_events(db, project_id=body.project_id)
        payload = returns.search_return_obligations(
            db,
            **body.model_dump(),
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
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
        "maintenance_return_obligation_search",
        "maintenance_project",
        {
            "project_id": body.project_id,
            "searched": bool(body.q and body.q.strip()),
            "classifications": list(body.classifications),
            "active_only": body.active_only,
            "page": body.page,
            "page_size": body.page_size,
            "total": payload["total"],
        },
    )
    return payload


@router.get("/projects/stable/{project_id}/return-rate")
def get_project_return_rate(
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        returns.consume_pending_return_events(db, project_id=project_id)
        payload = returns.project_return_rate(db, project_id=project_id)
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
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
        "maintenance_return_rate_read",
        "maintenance_project",
        {"project_id": project_id, "status": payload["status"]},
    )
    return payload


@router.post("/bad-returns/search")
def search_bad_returns(
    body: BadReturnSearch,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    payload = returns.search_bad_returns(db, **body.model_dump())
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
    record_access_log(
        ctx,
        "maintenance_bad_return_search",
        "maintenance_project",
        {
            "project_id": body.project_id,
            "statuses": list(body.statuses),
            "page": body.page,
            "page_size": body.page_size,
            "total": payload["total"],
        },
    )
    return payload


@router.post("/bad-returns", status_code=status.HTTP_201_CREATED)
def create_bad_return(
    body: BadReturnCreate,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        returns.consume_pending_return_events(db, project_id=body.project_id)
        payload = returns.create_bad_return(
            db,
            **body.model_dump(),
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "坏件返还单或幂等键重复") from exc
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)
        raise


def _transition(
    *,
    return_id: str,
    body: BadReturnCommand,
    action: str,
    db: Session,
    ident: dict,
    logistics_reference: str | None = None,
    warehouse_reference: str | None = None,
    inbound_reference: str | None = None,
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = returns.transition_bad_return(
            db,
            return_id=return_id,
            **body.model_dump(
                exclude={
                    "logistics_reference",
                    "warehouse_reference",
                    "inbound_reference",
                }
            ),
            action=action,
            operated_by=operator,
            logistics_reference=logistics_reference,
            warehouse_reference=warehouse_reference,
            inbound_reference=inbound_reference,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "坏件返还单或项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "坏件返还状态操作冲突") from exc
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)
        raise


@router.post("/bad-returns/{return_id}/submit")
def submit_bad_return(
    body: BadReturnCommand,
    return_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
) -> dict:
    return _transition(
        return_id=return_id,
        body=body,
        action="submit",
        db=db,
        ident=ident,
    )


@router.post("/bad-returns/{return_id}/in-transit")
def mark_bad_return_in_transit(
    body: BadReturnInTransit,
    return_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
) -> dict:
    return _transition(
        return_id=return_id,
        body=body,
        action="in_transit",
        db=db,
        ident=ident,
        logistics_reference=body.logistics_reference,
    )


@router.post("/bad-returns/{return_id}/warehouse-confirm")
def warehouse_confirm_bad_return(
    body: BadReturnWarehouseConfirm,
    return_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
) -> dict:
    return _transition(
        return_id=return_id,
        body=body,
        action="warehouse_confirm",
        db=db,
        ident=ident,
        warehouse_reference=body.warehouse_reference,
        inbound_reference=body.inbound_reference,
    )


@router.post("/return-obligations/{obligation_id}/resolve-category")
def resolve_return_category(
    body: ResolveReturnCategory,
    obligation_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _admin: str = Depends(require_admin),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = returns.resolve_obligation_category(
            db,
            obligation_id=obligation_id,
            **body.model_dump(),
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "返还义务或项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "返还义务品类处理冲突") from exc
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)
        raise
