"""Stable maintenance project reads and controlled master-data writes."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.maintenance_project_scope import require_maintenance_project_access
from app.auth import current_identity, current_role
from app.business_time import business_today
from app.db import get_db
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_project
from app.services import maintenance_project_catalog as catalog

router = APIRouter(prefix="/maintenance/projects/stable", tags=["maintenance"])


class StableProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_code: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=256)
    project_manager_id: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class StableProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    display_name: str | None = Field(default=None, max_length=256)
    project_manager_id: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class StableProjectLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class StableProjectDirectorySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)
    include_inactive: bool = False
    as_of: date | None = None


def _real_operator(db: Session, ident: dict) -> str:
    # This high-risk write accepts only tokens explicitly issued from a SysUser login.
    # Missing provenance includes legacy/shared tokens and deliberately requires re-login.
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "项目主档写入必须使用实名系统账号，请先在账号中心建立账号",
        )
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
        )
    )
    if not username or user is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "项目主档写入必须使用实名系统账号，请先在账号中心建立账号",
        )
    return username


@router.post("", status_code=status.HTTP_201_CREATED)
def create_stable_project(
    body: StableProjectCreate,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
) -> dict:
    operated_by = _real_operator(db, ident)
    try:
        payload = catalog.create_project(
            db,
            project_code=body.project_code,
            display_name=body.display_name,
            project_manager_id=body.project_manager_id,
            reason=body.reason,
            operated_by=operated_by,
        )
        db.commit()
        return payload
    except catalog.MaintenanceProjectCatalogConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except catalog.MaintenanceProjectCatalogError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.patch("/{project_id}")
def patch_stable_project(
    body: StableProjectPatch,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    operated_by = _real_operator(db, ident)
    updates = body.model_dump(
        exclude_unset=True,
        exclude={"version", "reason"},
    )
    try:
        payload = catalog.update_project(
            db,
            project_id=project_id,
            version=body.version,
            updates=updates,
            reason=body.reason,
            operated_by=operated_by,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except catalog.MaintenanceProjectCatalogConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except catalog.MaintenanceProjectCatalogError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


def _set_project_active(
    *,
    project_id: str,
    body: StableProjectLifecycle,
    active: bool,
    db: Session,
    ident: dict,
) -> dict:
    operated_by = _real_operator(db, ident)
    try:
        payload = catalog.set_project_active(
            db,
            project_id=project_id,
            version=body.version,
            active=active,
            reason=body.reason,
            operated_by=operated_by,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except catalog.MaintenanceProjectCatalogConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except catalog.MaintenanceProjectCatalogError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.post("/{project_id}/archive")
def archive_stable_project(
    body: StableProjectLifecycle,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    return _set_project_active(
        project_id=project_id,
        body=body,
        active=False,
        db=db,
        ident=ident,
    )


@router.post("/{project_id}/restore")
def restore_stable_project(
    body: StableProjectLifecycle,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    return _set_project_active(
        project_id=project_id,
        body=body,
        active=True,
        db=db,
        ident=ident,
    )


def _stable_project_directory_response(
    *,
    q: str | None,
    page: int,
    page_size: int,
    include_inactive: bool,
    as_of: date | None,
    db: Session,
    ctx: UserContext,
) -> dict:
    effective_as_of = as_of or business_today()
    record_access_log(
        ctx,
        "stable_project_directory",
        "maintenance",
        {
            "searched": bool(q and q.strip()),
            "include_inactive": include_inactive,
            "as_of": str(effective_as_of),
        },
    )
    return maintenance_project.project_directory(
        db,
        q_text=q,
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
        as_of=effective_as_of,
        user_ctx=ctx,
    )


@router.get("")
def stable_project_directory(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    include_inactive: bool = Query(False),
    as_of: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if q is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "项目搜索请使用安全搜索接口",
        )
    return _stable_project_directory_response(
        q=None,
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
        as_of=as_of,
        db=db,
        ctx=ctx,
    )


@router.post("/search")
def search_stable_project_directory(
    body: StableProjectDirectorySearch,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    q = body.q.strip()
    if not q or len(q) > 128:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "项目搜索条件无效",
        )
    return _stable_project_directory_response(
        q=q,
        page=body.page,
        page_size=body.page_size,
        include_inactive=body.include_inactive,
        as_of=body.as_of,
        db=db,
        ctx=ctx,
    )


@router.get("/{project_id}")
def stable_project_overview(
    project_id: str = Path(..., min_length=1, max_length=36),
    as_of: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _scope: None = Depends(require_maintenance_project_access),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    effective_as_of = as_of or business_today()
    record_access_log(
        ctx,
        "stable_project_overview",
        "maintenance_project",
        {"project_id": project_id, "as_of": str(effective_as_of)},
    )
    payload = maintenance_project.project_overview(
        db,
        project_id,
        as_of=effective_as_of,
        user_ctx=ctx,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="维保项目不存在")
    return payload
