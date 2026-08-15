"""坏件变卖登记 API（F5）。写操作：实名 + bad-return 管理权限 + 项目范围。"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.auth import current_identity, current_role
from app.db import get_db
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    require_action,
    require_page,
)
from app.services import maintenance_bad_salvage as salvage

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class SalvageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: int | None = None
    pn: str = Field(min_length=1, max_length=128)
    qty: Decimal = Field(gt=0, le=Decimal("999999999999"))
    revenue: Decimal = Field(ge=0, lt=Decimal("100000000000"))
    salvage_date: date
    buyer_note: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class SalvageVoid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


def _real_operator(db: Session, ident: dict) -> str:
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "坏件变卖登记必须使用实名系统账号")
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
        )
    )
    if not username or user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "坏件变卖登记必须使用实名系统账号")
    return username


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, salvage.SalvageConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, salvage.SalvageError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    raise exc


@router.get("/projects/stable/{project_id}/salvages")
def list_salvages(
    project_id: str = Path(...),
    response: Response = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """项目坏件变卖清单 + 贡献毛利（缺成本不按 0）。"""
    response.headers["Cache-Control"] = "no-store"
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "项目不存在"},
        )
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    return salvage.list_salvage(db, project_id)


@router.post(
    "/projects/stable/{project_id}/salvages", status_code=status.HTTP_201_CREATED
)
def register_salvage(
    project_id: str = Path(...),
    body: SalvageCreate = ...,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """登记一笔坏件变卖（独立事实，不伪造采购/销售单）。"""
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        payload = salvage.register_salvage(
            db,
            project_id=project_id,
            operated_by=operator,
            **body.model_dump(),
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
        raise HTTPException(status.HTTP_409_CONFLICT, "坏件变卖登记或幂等键重复") from exc
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)
        raise


@router.post("/salvages/{salvage_id}/void")
def void_salvage(
    salvage_id: str = Path(...),
    body: SalvageVoid = ...,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_bad_return_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """作废一笔坏件变卖登记（软作废，事实保留）。"""
    row = db.get(MaintenanceBadSalvage, salvage_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "变卖登记不存在")
    enforce_maintenance_project_access(db, project_id=row.project_id, ctx=ctx)
    if row.project_id != body.project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "变卖登记不属于该项目")
    operator = _real_operator(db, ident)
    try:
        payload = salvage.void_salvage(
            db, salvage_id=salvage_id, operated_by=operator, version=body.version
        )
        db.commit()
        return payload
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)
        raise
